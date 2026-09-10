"""Standalone IWI6 wavelet -> BGRA IWI converter. GPL-3.0, see LICENSE.

Adapted from OpenAssetTools IwiWaveletDecoder (contributors including
michaeloliverx), revision 9dca965366541504b71fa8cfb7ac049cb9b717e1.
Modified 2026-09-09: Python port, bounded file CLI and BGRA normalization.
"""
import argparse
from pathlib import Path
import struct
import sys
from tables import BLUE, RED_GREEN, ALPHA


def lookup(words):
    table = [None] * 4096
    for code, size, value in words:
        for index in range(code, 4096, 1 << size):
            if table[index] is not None:
                raise ValueError('Overlapping Huffman codewords')
            table[index] = (size, value)
    if None in table:
        raise ValueError('Incomplete Huffman table')
    return table


BLUE_TABLE, COLOR_TABLE, ALPHA_TABLE = map(lookup, (BLUE, RED_GREEN, ALPHA))


class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.started = False

    def peek(self, count):
        i, shift = divmod(self.pos, 8)
        return (int.from_bytes(self.data[i:i+5], 'little') >> shift) & ((1 << count) - 1)

    def bits(self, count):
        self.started = True
        if self.pos + count > len(self.data) * 8:
            raise ValueError('Truncated wavelet bitstream')
        value = self.peek(count)
        self.pos += count
        return value

    def raw(self, count):
        if self.started or self.pos % 8:
            raise ValueError('Raw wavelet pixels after coefficient stream')
        begin = self.pos // 8
        if begin + count > len(self.data):
            raise ValueError('Truncated wavelet base mip')
        self.pos += count * 8
        return bytearray(self.data[begin:begin+count])

    def value(self, table, escape_bits, bias):
        size, value = table[self.peek(12)]
        self.bits(size)
        return self.bits(escape_bits) - bias if value is None else value


def level(reader, source, width, height, channels):
    if width == 1 or height == 1:
        return reader.raw(width * height * channels)
    if source is None or len(source) != width * height // 4 * channels:
        raise ValueError('Invalid wavelet predictor mip')
    if reader.bits(1):
        source = bytearray((x + reader.value(ALPHA_TABLE, 9, 255)) & 255 for x in source)
    out = bytearray(width * height * channels)
    for y in range(0, height, 2):
        for x in range(0, width, 2):
            src = ((y // 2) * (width // 2) + x // 2) * channels
            dst = (y * width + x) * channels
            blue = None
            for channel in range(channels):
                if channels == 1 or (channels != 3 and channel == channels - 1):
                    table, count, bias = ALPHA_TABLE, 9, 255
                elif channel == 0:
                    table, count, bias = BLUE_TABLE, 9, 255
                else:
                    table, count, bias = COLOR_TABLE, 10, 510
                parity = reader.bits(1)
                coefficients = [reader.value(table, count, bias) for _ in range(3)]
                if channel == 0:
                    blue = coefficients
                elif channels >= 3 and channel in (1, 2):
                    coefficients = [a+b for a, b in zip(coefficients, blue)]
                h, v, d = coefficients
                base = 2 * source[src+channel]
                values = (parity + ((d+v+h+base) >> 1), (h+base-d-v) >> 1,
                          (v-d+base-h) >> 1, (base-h-v+d) >> 1)
                offsets = (dst, dst+channels, dst+width*channels, dst+(width+1)*channels)
                for offset, value in zip(offsets, values):
                    out[offset+channel] = value & 255
    return out


def bgra(data, fmt):
    if fmt == 6:
        return bytes(data)
    if fmt == 7:
        return b''.join(data[i:i+3] + b'\xff' for i in range(0, len(data), 3))
    if fmt == 8:
        return b''.join(bytes((data[i], data[i], data[i], data[i+1])) for i in range(0, len(data), 2))
    if fmt == 9:
        return b''.join(bytes((x, x, x, 255)) for x in data)
    return b''.join(bytes((255, 255, 255, x)) for x in data)


def convert(data):
    if len(data) < 28 or data[:4] != b'IWi\x06':
        raise ValueError('Expected an IWI v6 header')
    fmt, flags = data[4:6]
    if fmt not in range(6, 11):
        raise ValueError('Expected IWI wavelet format 0x06..0x0A')
    width, height, depth = struct.unpack_from('<HHH', data, 6)
    sizes = struct.unpack_from('<4I', data, 12)
    if sizes[0] != len(data):
        raise ValueError('IWI file size mismatch')
    if depth != 1 or not width or not height or width & (width-1) or height & (height-1):
        raise ValueError('Wavelet conversion requires power-of-two 2D dimensions')
    faces = 6 if flags & 4 else 1
    mip_count = 1 if flags & 2 else max(width, height).bit_length()
    if mip_count == 1 and width > 1 and height > 1:
        raise ValueError('Wavelet images larger than a strip require predictor mips')
    if sum(max(1, width >> i)*max(1, height >> i)*4*faces for i in range(mip_count)) > 256*1024*1024:
        raise ValueError('Decoded wavelet image exceeds 256 MiB allocation limit')
    channels = {6: 4, 7: 3, 8: 2, 9: 1, 10: 1}[fmt]
    reader = Reader(data[28:])
    previous = [None] * faces
    payload = bytearray()
    boundaries = {}
    for mip in reversed(range(mip_count)):
        for face in range(faces):
            pixels = level(reader, previous[face], max(1, width >> mip), max(1, height >> mip), channels)
            previous[face] = pixels
            payload.extend(bgra(pixels, fmt))
        boundaries[mip] = 28 + len(payload)
    header = bytearray(data[:28])
    header[4] = 1
    struct.pack_into('<4I', header, 12, *(boundaries[min(i, mip_count-1)] for i in range(4)))
    return bytes(header + payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        result = convert(args.input.read_bytes())
        args.output.write_bytes(result)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
