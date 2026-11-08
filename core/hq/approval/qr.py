"""Dependency-free QR byte encoding: fixed version 8, error level L, mask 0.

The fixed capacity is ample for the local otpauth URI. No network QR service,
login name or host identifier is used. ISO/IEC 18004 byte mode and BCH/RS codes.
"""
from __future__ import annotations


def _mul(a, b):
    value = 0
    while b:
        if b & 1:
            value ^= a
        b >>= 1
        a <<= 1
        if a & 256:
            a ^= 0x11d
    return value


def _ecc(block):
    generator = [1]
    alpha = 1
    for _ in range(24):
        expanded = [0] * (len(generator) + 1)
        for i, coefficient in enumerate(generator):
            expanded[i] ^= coefficient
            expanded[i + 1] ^= _mul(coefficient, alpha)
        generator = expanded
        alpha = _mul(alpha, 2)
    remainder = list(block) + [0] * 24
    for i in range(len(block)):
        factor = remainder[i]
        for j, coefficient in enumerate(generator):
            remainder[i + j] ^= _mul(coefficient, factor)
    return remainder[-24:]


def _bch(value, polynomial):
    while value.bit_length() >= polynomial.bit_length():
        value ^= polynomial << (value.bit_length() - polynomial.bit_length())
    return value


def matrix(text: str):
    raw = text.encode("utf-8")
    if len(raw) > 192:
        raise ValueError("qr_capacity")
    bits = [0, 1, 0, 0]
    bits += [(len(raw) >> shift) & 1 for shift in range(7, -1, -1)]
    for byte in raw:
        bits += [(byte >> shift) & 1 for shift in range(7, -1, -1)]
    bits += [0] * min(4, 194 * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [sum(bits[i + j] << (7 - j) for j in range(8)) for i in range(0, len(bits), 8)]
    while len(data) < 194:
        data.append(0xec if (len(data) - (len(bits) // 8)) % 2 == 0 else 0x11)
    blocks = [data[:97], data[97:]]
    parity = [_ecc(b) for b in blocks]
    codewords = [b[i] for i in range(97) for b in blocks] + [b[i] for i in range(24) for b in parity]
    stream = [(b >> shift) & 1 for b in codewords for shift in range(7, -1, -1)]
    n = 49
    cells = [[None] * n for _ in range(n)]
    for top, left in ((0, 0), (n - 7, 0), (0, n - 7)):
        for row in range(-1, 8):
            for col in range(-1, 8):
                if 0 <= top + row < n and 0 <= left + col < n:
                    cells[top + row][left + col] = (0 <= row <= 6 and 0 <= col <= 6 and
                        (row in (0, 6) or col in (0, 6) or (2 <= row <= 4 and 2 <= col <= 4)))
    for row in (6, 24, 42):
        for col in (6, 24, 42):
            if cells[row][col] is not None:
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    cells[row + dy][col + dx] = max(abs(dy), abs(dx)) != 1
    for i in range(8, n - 8):
        if cells[i][6] is None:
            cells[i][6] = i % 2 == 0
        if cells[6][i] is None:
            cells[6][i] = i % 2 == 0
    version = (8 << 12) | _bch(8 << 12, 0x1f25)
    for i in range(18):
        value = bool((version >> i) & 1)
        cells[i // 3][i % 3 + n - 11] = value
        cells[i % 3 + n - 11][i // 3] = value
    fmt_data = 1 << 3  # level L, mask 0
    fmt = ((fmt_data << 10) | _bch(fmt_data << 10, 0x537)) ^ 0x5412
    for i in range(15):
        value = bool((fmt >> i) & 1)
        row = i if i < 6 else i + 1 if i < 8 else n - 15 + i
        col = n - i - 1 if i < 8 else 15 - i if i < 9 else 14 - i
        cells[row][8] = value
        cells[8][col] = value
    cells[n - 8][8] = True
    bit = 0
    row, direction = n - 1, -1
    for right in range(n - 1, 0, -2):
        if right <= 6:
            right -= 1
        while True:
            for col in (right, right - 1):
                if cells[row][col] is None:
                    value = bool(stream[bit]) if bit < len(stream) else False
                    cells[row][col] = value ^ ((row + col) % 2 == 0)
                    bit += 1
            row += direction
            if row < 0 or row >= n:
                row -= direction
                direction = -direction
                break
    return cells


def svg(text: str) -> str:
    cells = matrix(text)
    path = ''.join(f'M{col + 4},{row + 4}h1v1h-1z' for row, line in enumerate(cells)
                   for col, dark in enumerate(line) if dark)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 57 57" width="342" height="342" '
            'shape-rendering="crispEdges"><rect width="57" height="57" fill="white"/>'
            f'<path d="{path}" fill="black"/></svg>')
