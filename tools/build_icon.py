from __future__ import annotations

import math
import struct
from pathlib import Path


def color(hex_value: str, alpha: int = 255):
    value = hex_value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha)


def distance_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def make_pixels(size: int) -> list[tuple[int, int, int, int]]:
    scale = size / 32.0
    canvas = [(0, 0, 0, 0)] * (size * size)
    bg = color("#0B0F0C")
    border = color("#263329")
    green = color("#6FA576")
    bright = color("#88B98E")

    def rounded_rect(px, py, left, top, right, bottom, radius):
        cx = min(max(px, left + radius), right - radius)
        cy = min(max(py, top + radius), bottom - radius)
        return (px - cx) ** 2 + (py - cy) ** 2 <= radius ** 2

    def set_pixel(x, y, rgba):
        ix, iy = int(x), int(y)
        if 0 <= ix < size and 0 <= iy < size:
            canvas[iy * size + ix] = rgba

    for y in range(size):
        for x in range(size):
            px, py = (x + 0.5) / scale, (y + 0.5) / scale
            if rounded_rect(px, py, 2, 2, 30, 30, 6):
                canvas[y * size + x] = bg
                if not rounded_rect(px, py, 3, 3, 29, 29, 5):
                    canvas[y * size + x] = border

    def line(ax, ay, bx, by, width, rgba):
        ax, ay, bx, by, width = [value * scale for value in (ax, ay, bx, by, width)]
        for y in range(max(0, int(min(ay, by) - width - 1)), min(size, int(max(ay, by) + width + 2))):
            for x in range(max(0, int(min(ax, bx) - width - 1)), min(size, int(max(ax, bx) + width + 2))):
                if distance_to_segment(x + 0.5, y + 0.5, ax, ay, bx, by) <= width / 2:
                    set_pixel(x, y, rgba)

    # Monitor outline, short stand, and a restrained activity trace.
    line(7, 7, 25, 7, 1.5, green)
    line(7, 7, 7, 21, 1.5, green)
    line(25, 7, 25, 21, 1.5, green)
    line(7, 21, 25, 21, 1.5, green)
    line(12, 25, 20, 25, 1.5, green)
    line(16, 21, 16, 25, 1.5, green)
    line(9, 17, 12, 17, 1.5, bright)
    line(12, 17, 15, 12, 1.5, bright)
    line(15, 12, 18, 16, 1.5, bright)
    line(18, 16, 22, 11, 1.5, bright)
    return canvas


def dib_icon(size: int) -> bytes:
    pixels = make_pixels(size)
    xor = bytearray()
    for y in range(size - 1, -1, -1):
        for x in range(size):
            r, g, b, a = pixels[y * size + x]
            xor.extend((b, g, r, a))
    row_bytes = ((size + 31) // 32) * 4
    mask = bytes(row_bytes * size)
    header = struct.pack(
        "<IIIHHIIIIII", 40, size, size * 2, 1, 32, 0,
        len(xor), 0, 0, 0, 0
    )
    return header + xor + mask


def main():
    sizes = (16, 24, 32)
    images = [dib_icon(size) for size in sizes]
    directory_bytes = 6 + 16 * len(sizes)
    entries = []
    offset = directory_bytes
    for size, image in zip(sizes, images):
        entries.append(struct.pack(
            "<BBBBHHII", size, size, 0, 0, 1, 32, len(image), offset
        ))
        offset += len(image)
    output = struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(entries) + b"".join(images)
    destination = Path(__file__).resolve().parents[1] / "app" / "pythonwatch.ico"
    destination.write_bytes(output)
    print(f"Wrote {destination}")


if __name__ == "__main__":
    main()
