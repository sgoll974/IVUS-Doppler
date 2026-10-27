"""Receive one DDSP B-mode frame, verify it, and save it as a portable graymap."""

import argparse
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--output", type=Path, default=Path("received_frame.pgm"))
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.5)
    deadline = time.monotonic() + args.timeout
    active = None

    print(f"Listening on UDP {args.port}...")
    while time.monotonic() < deadline:
        try:
            datagram, source = sock.recvfrom(HEADER.size + MAX_PAYLOAD)
        except socket.timeout:
            continue
        if len(datagram) < HEADER.size:
            continue
        fields = HEADER.unpack(datagram[:HEADER.size])
        (magic, version, stream_type, flags, frame_id, timestamp_us,
         packet_index, packet_count, payload_bytes, width, height, reserved,
         area_milli, expected_crc) = fields
        payload = datagram[HEADER.size:]
        if (magic, version, stream_type) != (MAGIC, VERSION, STREAM_BMODE8):
            continue
        frame_bytes = width * height
        offset = packet_index * MAX_PAYLOAD
        if (len(payload) != payload_bytes or packet_index >= packet_count or
                packet_count != (frame_bytes + MAX_PAYLOAD - 1) // MAX_PAYLOAD or
                offset + payload_bytes > frame_bytes):
            continue
        if active is None or active["id"] != frame_id:
            active = {
                "id": frame_id, "width": width, "height": height,
                "area": area_milli, "crc": expected_crc,
                "count": packet_count, "parts": {}, "source": source,
            }
        active["parts"].setdefault(packet_index, payload)
        if len(active["parts"]) == active["count"]:
            pixels = b"".join(active["parts"][i] for i in range(active["count"]))
            pixels = pixels[:active["width"] * active["height"]]
            if len(pixels) != active["width"] * active["height"]:
                raise SystemExit("Completed frame has the wrong byte count")
            calculated = zlib.crc32(pixels) & 0xFFFFFFFF
            if calculated != active["crc"]:
                raise SystemExit("Completed frame failed CRC validation")
            args.output.write_bytes(
                f"P5\n{active['width']} {active['height']}\n255\n".encode() + pixels
            )
            print(f"Received frame {active['id']} from {active['source'][0]}")
            print(f"Dimensions: {active['width']}x{active['height']}")
            print(f"Lumen area: {active['area'] / 1000.0:.3f} mm^2")
            print(f"CRC32: {calculated:08x}; saved {args.output}")
            return
    raise SystemExit("Timed out without receiving one complete valid frame")


if __name__ == "__main__":
    main()
