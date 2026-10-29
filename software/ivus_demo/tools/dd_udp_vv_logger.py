"""Log DDSP IVUS integrity and throughput for a timed verification test.

The measurement timer begins with the first valid DDSP IVUS packet, so time
spent starting the Red Pitaya sender is not included in the results.
"""

import argparse
import csv
import json
import socket
import struct
import time
import zlib
from pathlib import Path


HEADER = struct.Struct("!IBBHIQHHHHHHII")
MAGIC = 0x44445350
VERSION = 1
STREAM_BMODE8 = 1
MAX_PAYLOAD = 1400

# recvfrom() returns the UDP payload, which already contains the 40-byte DDSP
# header. These additional byte-times estimate the remaining on-wire overhead.
WIRE_BYTES_OUTSIDE_UDP_PAYLOAD = 8 + 20 + 14 + 4 + 8 + 12


def crc32_bytes(data):
    """Return the unsigned CRC32 of a completed frame."""
    return zlib.crc32(data) & 0xFFFFFFFF


def new_assembly(frame_id, width, height, count, area_milli, expected_crc):
    """Allocate the state required to reconstruct one IVUS frame."""
    return {
        "id": frame_id,
        "width": width,
        "height": height,
        "count": count,
        "area_milli": area_milli,
        "crc": expected_crc,
        "parts": {},
    }


def write_outputs(output_dir, rows, summary):
    """Write the per-second CSV and final JSON summary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "network_timeseries.csv"
    json_path = output_dir / "network_summary.json"

    fieldnames = [
        "elapsed_s",
        "interval_udp_datagrams",
        "interval_udp_bytes",
        "interval_estimated_wire_bytes",
        "interval_udp_mbps",
        "interval_estimated_wire_mbps",
        "frames_started",
        "valid_frames",
        "damaged_frames",
        "crc_failures",
        "missing_packets",
        "duplicate_packets",
        "malformed_packets",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with json_path.open("w", encoding="utf-8") as stream:
        json.dump(summary, stream, indent=2, sort_keys=True)
        stream.write("\n")

    print(f"Wrote {csv_path}")
    print(f"Wrote {json_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--duration", type=float, default=300.0,
                        help="Measured seconds beginning with the first valid packet")
    parser.add_argument("--wait-timeout", type=float, default=600.0,
                        help="Maximum seconds to wait for the sender to begin")
    parser.add_argument("--fps", type=float, default=10.0 / 3.0,
                        help="Expected playback frame rate for theoretical throughput")
    parser.add_argument("--output-dir", type=Path, default=Path("vv_results"))
    args = parser.parse_args()

    if args.duration <= 0 or args.wait_timeout <= 0 or args.fps <= 0:
        raise SystemExit("Duration, wait timeout, and FPS must be positive")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.2)

    counters = {
        "udp_datagrams": 0,
        "udp_bytes": 0,
        "estimated_wire_bytes": 0,
        "frames_started": 0,
        "valid_frames": 0,
        "damaged_frames": 0,
        "crc_failures": 0,
        "missing_packets": 0,
        "duplicate_packets": 0,
        "malformed_packets": 0,
    }
    interval = {"datagrams": 0, "udp_bytes": 0, "wire_bytes": 0}
    rows = []
    assembly = None
    measurement_start = None
    waiting_start = time.monotonic()
    next_sample = 1.0
    source_ip = None
    first_width = None
    first_height = None
    first_packet_count = None
    trailing_partial_ignored = False

    print(f"Listening on UDP {args.port}...")
    print("The 5-minute timer will start with the first valid DDSP IVUS packet.")

    try:
        while True:
            now = time.monotonic()
            if measurement_start is None:
                if now - waiting_start >= args.wait_timeout:
                    raise SystemExit("Timed out waiting for the Red Pitaya sender")
            elif now - measurement_start >= args.duration:
                if assembly is not None and len(assembly["parts"]) < assembly["count"]:
                    trailing_partial_ignored = True
                break

            try:
                datagram, source = sock.recvfrom(HEADER.size + MAX_PAYLOAD)
            except socket.timeout:
                datagram = None

            if datagram is not None:
                counters["udp_datagrams"] += 1
                counters["udp_bytes"] += len(datagram)
                counters["estimated_wire_bytes"] += (
                    len(datagram) + WIRE_BYTES_OUTSIDE_UDP_PAYLOAD
                )
                interval["datagrams"] += 1
                interval["udp_bytes"] += len(datagram)
                interval["wire_bytes"] += (
                    len(datagram) + WIRE_BYTES_OUTSIDE_UDP_PAYLOAD
                )

                if len(datagram) < HEADER.size:
                    counters["malformed_packets"] += 1
                    continue

                fields = HEADER.unpack(datagram[:HEADER.size])
                (magic, version, stream_type, _flags, frame_id, _timestamp_us,
                 packet_index, packet_count, payload_bytes, width, height,
                 _reserved, area_milli, expected_crc) = fields
                payload = datagram[HEADER.size:]
                frame_bytes = width * height
                expected_count = (frame_bytes + MAX_PAYLOAD - 1) // MAX_PAYLOAD
                offset = packet_index * MAX_PAYLOAD

                valid_header = (
                    magic == MAGIC and
                    version == VERSION and
                    stream_type == STREAM_BMODE8 and
                    width > 0 and height > 0 and
                    frame_bytes <= 2048 * 2048 and
                    packet_count == expected_count and
                    packet_index < packet_count and
                    payload_bytes == len(payload) and
                    payload_bytes <= MAX_PAYLOAD and
                    offset + payload_bytes <= frame_bytes
                )
                if not valid_header:
                    counters["malformed_packets"] += 1
                    continue

                if measurement_start is None:
                    measurement_start = time.monotonic()
                    source_ip = source[0]
                    first_width = width
                    first_height = height
                    first_packet_count = packet_count
                    print(
                        f"Measurement started: source={source_ip}, "
                        f"frame={width}x{height}, packets/frame={packet_count}"
                    )

                if assembly is None or assembly["id"] != frame_id:
                    if assembly is not None and len(assembly["parts"]) < assembly["count"]:
                        counters["damaged_frames"] += 1
                        counters["missing_packets"] += (
                            assembly["count"] - len(assembly["parts"])
                        )
                    assembly = new_assembly(
                        frame_id, width, height, packet_count,
                        area_milli, expected_crc
                    )
                    counters["frames_started"] += 1

                if (assembly["width"] != width or assembly["height"] != height or
                        assembly["count"] != packet_count or
                        assembly["crc"] != expected_crc):
                    counters["malformed_packets"] += 1
                    continue

                if packet_index in assembly["parts"]:
                    counters["duplicate_packets"] += 1
                    continue

                assembly["parts"][packet_index] = payload
                if len(assembly["parts"]) == assembly["count"]:
                    pixels = b"".join(
                        assembly["parts"][index]
                        for index in range(assembly["count"])
                    )[:frame_bytes]
                    if len(pixels) != frame_bytes or crc32_bytes(pixels) != assembly["crc"]:
                        counters["crc_failures"] += 1
                        counters["damaged_frames"] += 1
                    else:
                        counters["valid_frames"] += 1
                    assembly = None

            if measurement_start is not None:
                elapsed = time.monotonic() - measurement_start
                while elapsed >= next_sample:
                    rows.append({
                        "elapsed_s": round(next_sample, 3),
                        "interval_udp_datagrams": interval["datagrams"],
                        "interval_udp_bytes": interval["udp_bytes"],
                        "interval_estimated_wire_bytes": interval["wire_bytes"],
                        "interval_udp_mbps": round(interval["udp_bytes"] * 8 / 1e6, 6),
                        "interval_estimated_wire_mbps": round(
                            interval["wire_bytes"] * 8 / 1e6, 6
                        ),
                        "frames_started": counters["frames_started"],
                        "valid_frames": counters["valid_frames"],
                        "damaged_frames": counters["damaged_frames"],
                        "crc_failures": counters["crc_failures"],
                        "missing_packets": counters["missing_packets"],
                        "duplicate_packets": counters["duplicate_packets"],
                        "malformed_packets": counters["malformed_packets"],
                    })
                    interval = {"datagrams": 0, "udp_bytes": 0, "wire_bytes": 0}
                    print(
                        f"{int(next_sample):4d}s  valid={counters['valid_frames']}  "
                        f"damaged={counters['damaged_frames']}  "
                        f"missing={counters['missing_packets']}"
                    )
                    next_sample += 1.0
    except KeyboardInterrupt:
        print("\nStopped by user; writing partial results.")
    finally:
        sock.close()

    measured_duration = 0.0
    if measurement_start is not None:
        measured_duration = min(time.monotonic() - measurement_start, args.duration)

    decided_frames = counters["valid_frames"] + counters["damaged_frames"]
    frame_completion_percent = (
        100.0 * counters["valid_frames"] / decided_frames
        if decided_frames else 0.0
    )
    frame_bytes = (first_width or 0) * (first_height or 0)
    theoretical_wire_mbps = 0.0
    if first_packet_count:
        theoretical_wire_bytes_per_frame = (
            frame_bytes + first_packet_count *
            (HEADER.size + WIRE_BYTES_OUTSIDE_UDP_PAYLOAD)
        )
        theoretical_wire_mbps = (
            theoretical_wire_bytes_per_frame * 8 * args.fps / 1e6
        )

    summary = {
        "source_ip": source_ip,
        "measurement_duration_s": round(measured_duration, 6),
        "configured_duration_s": args.duration,
        "expected_fps": args.fps,
        "width": first_width,
        "height": first_height,
        "packets_per_frame": first_packet_count,
        "frame_bytes": frame_bytes,
        **counters,
        "decided_frames": decided_frames,
        "frame_completion_percent": round(frame_completion_percent, 6),
        "average_udp_mbps": round(
            counters["udp_bytes"] * 8 / measured_duration / 1e6, 6
        ) if measured_duration else 0.0,
        "average_estimated_wire_mbps": round(
            counters["estimated_wire_bytes"] * 8 / measured_duration / 1e6, 6
        ) if measured_duration else 0.0,
        "theoretical_estimated_wire_mbps": round(theoretical_wire_mbps, 6),
        "trailing_partial_frame_ignored": trailing_partial_ignored,
        "notes": (
            "The final partial frame caused only by the timed cutoff is not counted "
            "as damaged. Wire throughput is estimated from standard Ethernet byte-times."
        ),
    }
    write_outputs(args.output_dir, rows, summary)

    print("\nVerification summary")
    print(f"Valid frames: {counters['valid_frames']}")
    print(f"Damaged frames: {counters['damaged_frames']}")
    print(f"Frame completion: {frame_completion_percent:.3f}%")
    print(f"Missing packets: {counters['missing_packets']}")
    print(f"CRC failures: {counters['crc_failures']}")
    print(f"Average estimated wire rate: {summary['average_estimated_wire_mbps']:.3f} Mb/s")
    print(f"Theoretical estimated wire rate: {theoretical_wire_mbps:.3f} Mb/s")


if __name__ == "__main__":
    main()
