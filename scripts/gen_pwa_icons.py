"""Write the installable app's icons, deterministically, with no imaging dependency.

The web front end has no build step, so the icons are committed files. This script is
how they were made and how anyone can check them: it draws the same mark as
`web/icons/veris.svg` (a white V on the product's navy tile) by supersampling each pixel,
and encodes the PNG itself with zlib, as `pdfrender.py` does for page images. Running it
twice writes identical bytes; `tests/test_pwa.py` holds the committed files to that.

    python scripts/gen_pwa_icons.py           # rewrite web/icons/veris-{192,512}.png
    python scripts/gen_pwa_icons.py --check   # exit 1 if a committed icon differs
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib
from pathlib import Path

ICON_DIR = Path(__file__).resolve().parents[1] / "web" / "icons"
SIZES = (192, 512)

_NAVY = (15, 23, 42)
_WHITE = (255, 255, 255)
_JADE = (45, 160, 125)
_RADIUS = 0.22          # tile corner radius, as a fraction of the side
_STROKE = 0.11          # V stroke width, as a fraction of the side
_V = ((0.28, 0.27), (0.50, 0.74), (0.72, 0.27))
_DOT = ((0.50, 0.86), 0.045)
_SAMPLES = 4            # 4 x 4 supersampling per pixel


def _segment_distance(px: float, py: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _inside_tile(x: float, y: float) -> bool:
    r = _RADIUS
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    return math.hypot(x - cx, y - cy) <= r


def _sample(x: float, y: float) -> tuple[int, int, int, int]:
    if not _inside_tile(x, y):
        return (0, 0, 0, 0)
    (dot_x, dot_y), dot_r = _DOT
    if math.hypot(x - dot_x, y - dot_y) <= dot_r:
        return (*_JADE, 255)
    half = _STROKE / 2
    if (_segment_distance(x, y, _V[0], _V[1]) <= half
            or _segment_distance(x, y, _V[1], _V[2]) <= half):
        return (*_WHITE, 255)
    return (*_NAVY, 255)


def render(size: int) -> bytes:
    rows = bytearray()
    step = 1.0 / (size * _SAMPLES)
    for py in range(size):
        rows.append(0)                                   # PNG filter type 0 (None)
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(_SAMPLES):
                for sx in range(_SAMPLES):
                    r, g, b, a = _sample((px * _SAMPLES + sx + 0.5) * step,
                                         (py * _SAMPLES + sy + 0.5) * step)
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
            total = _SAMPLES * _SAMPLES
            alpha = acc[3] // total
            if acc[3]:
                rows.extend((acc[0] // acc[3], acc[1] // acc[3], acc[2] // acc[3], alpha))
            else:
                rows.extend((0, 0, 0, 0))
    return _png(size, size, bytes(rows))


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def _png(width: int, height: int, raw: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)   # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="compare the committed icons with fresh output and exit 1 on a difference")
    args = parser.parse_args(argv)
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    stale = []
    for size in SIZES:
        path = ICON_DIR / f"veris-{size}.png"
        data = render(size)
        if args.check:
            if not path.is_file() or path.read_bytes() != data:
                stale.append(path.name)
        else:
            path.write_bytes(data)
            print(f"wrote {path.name} ({len(data)} bytes)")
    if stale:
        print("stale icons: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
