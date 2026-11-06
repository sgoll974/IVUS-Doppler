"""Validate a timed DDSP pulsed-wave Doppler UDP stream on port 8081."""

import argparse
import socket
import struct
import time
import zlib


HEADER = struct.Struct("!IBBHIQHHHHHHII")
DOPPLER_METADATA = struct.Struct("!7I")
MAGIC = 0x44445350
VERSION = 1
STREAM_DOPPLER_PW8 = 2
FLAG_LOOP_START = 0x0004
FLAG_BEAT_UPDATE = 0x0008
MAX_PAYLOAD = 1400


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--wait-timeout", type=float, default=30.0)
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.5)

    wait_deadline = time.monotonic() + args.wait_timeout
    measurement_start = None
    previous_frame_id = None
    valid_samples = 0
    missing_samples = 0
    crc_failures = 0
    malformed_packets = 0
    loop_starts = 0
    beat_updates = 0
    latest = None
    source_ip = None

    print(f"Listening for DDSP Doppler on UDP {args.port}...")
    while True:
        now = time.monotonic()
        if measurement_start is None and now >= wait_deadline:
            raise SystemExit("Timed out waiting for the Doppler sender")
        if measurement_start is not None and now - measurement_start >= args.duration:
            break

        try:
            datagram, source = sock.recvfrom(HEADER.size + MAX_PAYLOAD)
        except socket.timeout:
            continue
        if len(datagram) < HEADER.size:
            malformed_packets += 1
            continue

        fields = HEADER.unpack(datagram[: HEADER.size])
        (
            magic,
            version,
            stream_type,
            flags,
            frame_id,
            timestamp_us,
            packet_index,
            packet_count,
            payload_bytes,
            width,
            height,
            reserved,
            area_milli,
            expected_crc,
        ) = fields
        payload = datagram[HEADER.size :]

        if (magic, version, stream_type) != (
            MAGIC,
            VERSION,
            STREAM_DOPPLER_PW8,
        ):
            continue
        if measurement_start is None:
            measurement_start = time.monotonic()
            source_ip = source[0]

        if (
            packet_index != 0
            or packet_count != 1
            or height != 1
            or payload_bytes != len(payload)
            or payload_bytes != DOPPLER_METADATA.size + width
        ):
            malformed_packets += 1
            continue

        calculated_crc = zlib.crc32(payload) & 0xFFFFFFFF
        if calculated_crc != expected_crc:
            crc_failures += 1
            continue

        if previous_frame_id is not None and frame_id > previous_frame_id + 1:
            missing_samples += frame_id - previous_frame_id - 1
        previous_frame_id = frame_id

        metadata = DOPPLER_METADATA.unpack(payload[: DOPPLER_METADATA.size])
        (
            sample_index,
            time_offset_us,
            heart_rate_milli_bpm,
            lvot_diameter_um,
            vti_milli_cm,
            stroke_volume_microliters,
            cardiac_output_milli_lpm,
        ) = metadata
        spectrum = payload[DOPPLER_METADATA.size :]
        if len(spectrum) != width:
            malformed_packets += 1
            continue

        valid_samples += 1
        if flags & FLAG_LOOP_START:
            loop_starts += 1
        if flags & FLAG_BEAT_UPDATE:
            beat_updates += 1
            print(
                f"Beat update: VTI={vti_milli_cm / 1000:.3f} cm, "
                f"SV={stroke_volume_microliters / 1000:.3f} mL, "
                f"CO={cardiac_output_milli_lpm / 1000:.3f} L/min"
            )
        latest = {
            "sample_index": sample_index,
            "time_offset_s": time_offset_us / 1_000_000,
            "heart_rate_bpm": heart_rate_milli_bpm / 1000,
            "lvot_diameter_cm": lvot_diameter_um / 10000,
            "vti_cm": vti_milli_cm / 1000,
            "stroke_volume_ml": stroke_volume_microliters / 1000,
            "cardiac_output_lpm": cardiac_output_milli_lpm / 1000,
            "area_mm2": area_milli / 1000,
            "timestamp_us": timestamp_us,
        }

    elapsed = time.monotonic() - measurement_start
    print("\nDoppler stream verification")
    print(f"Source: {source_ip}")
    print(f"Measured duration: {elapsed:.3f} s")
    print(f"Valid spectral samples: {valid_samples}")
    print(f"Estimated sample rate: {valid_samples / elapsed:.3f} samples/s")
    print(f"Missing samples: {missing_samples}")
    print(f"CRC failures: {crc_failures}")
    print(f"Malformed packets: {malformed_packets}")
    print(f"Loop starts: {loop_starts}")
    print(f"Beat updates: {beat_updates}")
    if latest:
        print(f"Latest HR: {latest['heart_rate_bpm']:.3f} bpm")
        print(f"Latest LVOT diameter: {latest['lvot_diameter_cm']:.3f} cm")
        print(f"Latest VTI: {latest['vti_cm']:.3f} cm")
        print(f"Latest stroke volume: {latest['stroke_volume_ml']:.3f} mL")
        print(f"Latest cardiac output: {latest['cardiac_output_lpm']:.3f} L/min")


if __name__ == "__main__":
    main()
