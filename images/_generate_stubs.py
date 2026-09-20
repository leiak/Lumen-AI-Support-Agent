"""Generate 1x1 stub PNGs for M2.B Act 5 demo screenshots.

Real screenshots require SES + MinIO + Doubao credentials + live services.
These stubs are placeholders that get replaced in real demos.
"""
import os
import struct
import zlib


def make_1x1_png(path: str) -> None:
    """Generate a minimal 1x1 PNG (1 red pixel)."""
    # PNG signature
    sig = b"\x89PNG\r\n\x1a\n"
    # IHDR chunk (1x1, 8-bit RGB)
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data)
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc & 0xFFFFFFFF)
    # IDAT chunk (1 red pixel)
    raw = b"\x00\xff\x00\x00"  # filter byte + RGB
    compressed = zlib.compress(raw)
    idat_crc = zlib.crc32(b"IDAT" + compressed)
    idat = struct.pack(">I", len(compressed)) + b"IDAT" + compressed + struct.pack(">I", idat_crc & 0xFFFFFFFF)
    # IEND chunk
    iend_crc = zlib.crc32(b"IEND")
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(sig + ihdr + idat + iend)


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    for name in [
        "demo-act5-01-email.png",
        "demo-act5-02-multimodal.png",
        "demo-act5-03-history-mining.png",
    ]:
        make_1x1_png(os.path.join(base, name))
        print(f"Generated {name}")