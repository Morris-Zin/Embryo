"""Small dependency-free PNG codec used when Pillow is unavailable.

Supports the non-interlaced 8-bit PNG variants produced by Playwright, Slint MCP,
and this repository's synthetic verification fixtures.
"""
from __future__ import annotations

import binascii
import struct
import zlib
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PngError(ValueError):
    pass


def _chunks(data: bytes):
    if not data.startswith(PNG_SIGNATURE):
        raise PngError("missing PNG signature")
    offset = len(PNG_SIGNATURE)
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        crc = data[offset + 8 + length : offset + 12 + length]
        if len(payload) != length or len(crc) != 4:
            raise PngError("truncated PNG chunk")
        expected = binascii.crc32(kind + payload) & 0xFFFFFFFF
        if struct.unpack(">I", crc)[0] != expected:
            raise PngError(f"CRC mismatch in {kind.decode('latin1')} chunk")
        yield kind, payload
        offset += 12 + length
        if kind == b"IEND":
            return
    raise PngError("missing IEND chunk")


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    distance_left = abs(estimate - left)
    distance_above = abs(estimate - above)
    distance_upper_left = abs(estimate - upper_left)
    if distance_left <= distance_above and distance_left <= distance_upper_left:
        return left
    if distance_above <= distance_upper_left:
        return above
    return upper_left


def read_png_rgba(path: Path) -> tuple[int, int, bytes]:
    data = Path(path).read_bytes()
    width = height = bit_depth = color_type = interlace = None
    palette = b""
    transparency = b""
    compressed = bytearray()
    for kind, payload in _chunks(data):
        if kind == b"IHDR":
            width, height, bit_depth, color_type, compression, png_filter, interlace = struct.unpack(">IIBBBBB", payload)
            if compression != 0 or png_filter != 0:
                raise PngError("unsupported PNG compression or filter method")
        elif kind == b"PLTE":
            palette = payload
        elif kind == b"tRNS":
            transparency = payload
        elif kind == b"IDAT":
            compressed.extend(payload)
    if width is None or height is None or bit_depth is None or color_type is None or interlace is None:
        raise PngError("missing IHDR chunk")
    if bit_depth != 8:
        raise PngError(f"unsupported PNG bit depth {bit_depth}; expected 8")
    if interlace != 0:
        raise PngError("interlaced PNG is unsupported")
    channels_by_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    channels = channels_by_type.get(color_type)
    if channels is None:
        raise PngError(f"unsupported PNG color type {color_type}")
    stride = width * channels
    raw = zlib.decompress(bytes(compressed))
    expected = (stride + 1) * height
    if len(raw) != expected:
        raise PngError(f"unexpected pixel data length {len(raw)}; expected {expected}")

    rows: list[bytearray] = []
    offset = 0
    prior = bytearray(stride)
    for _y in range(height):
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset : offset + stride])
        offset += stride
        if filter_type == 1:  # Sub
            for index in range(channels, stride):
                row[index] = (row[index] + row[index - channels]) & 0xFF
        elif filter_type == 2:  # Up
            for index in range(stride):
                row[index] = (row[index] + prior[index]) & 0xFF
        elif filter_type == 3:  # Average
            for index in range(stride):
                left = row[index - channels] if index >= channels else 0
                row[index] = (row[index] + ((left + prior[index]) // 2)) & 0xFF
        elif filter_type == 4:  # Paeth
            for index in range(stride):
                left = row[index - channels] if index >= channels else 0
                upper_left = prior[index - channels] if index >= channels else 0
                row[index] = (row[index] + _paeth(left, prior[index], upper_left)) & 0xFF
        elif filter_type != 0:
            raise PngError(f"unsupported PNG row filter {filter_type}")
        rows.append(row)
        prior = row

    rgba = bytearray(width * height * 4)
    for y, row in enumerate(rows):
        out = y * width * 4
        if color_type == 0:
            transparent_gray = struct.unpack(">H", transparency[:2])[0] if len(transparency) >= 2 else None
            for x, gray in enumerate(row):
                alpha = 0 if transparent_gray == gray else 255
                rgba[out : out + 4] = bytes((gray, gray, gray, alpha))
                out += 4
        elif color_type == 2:
            transparent_rgb = struct.unpack(">HHH", transparency[:6]) if len(transparency) >= 6 else None
            for x in range(width):
                src = x * 3
                rgb = row[src : src + 3]
                alpha = 0 if transparent_rgb == tuple(rgb) else 255
                rgba[out : out + 4] = rgb + bytes((alpha,))
                out += 4
        elif color_type == 3:
            if not palette:
                raise PngError("indexed PNG missing palette")
            for x, palette_index in enumerate(row):
                src = palette_index * 3
                if src + 3 > len(palette):
                    raise PngError("palette index outside palette")
                alpha = transparency[palette_index] if palette_index < len(transparency) else 255
                rgba[out : out + 4] = palette[src : src + 3] + bytes((alpha,))
                out += 4
        elif color_type == 4:
            for x in range(width):
                src = x * 2
                gray = row[src]
                rgba[out : out + 4] = bytes((gray, gray, gray, row[src + 1]))
                out += 4
        else:
            rgba[out : out + width * 4] = row
    return width, height, bytes(rgba)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)


def png_bytes_rgba(width: int, height: int, pixels: bytes) -> bytes:
    if width <= 0 or height <= 0:
        raise PngError("PNG dimensions must be positive")
    if len(pixels) != width * height * 4:
        raise PngError("RGBA pixel length does not match dimensions")
    scanlines = bytearray()
    stride = width * 4
    for y in range(height):
        scanlines.append(0)
        start = y * stride
        scanlines.extend(pixels[start : start + stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(scanlines))) + _chunk(b"IEND", b"")


def write_png_rgba(path: Path, width: int, height: int, pixels: bytes) -> None:
    Path(path).write_bytes(png_bytes_rgba(width, height, pixels))
