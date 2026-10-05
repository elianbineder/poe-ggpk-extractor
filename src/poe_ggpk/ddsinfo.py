"""Exact size of a DDS texture (and Ogg stream) from its header.

Tells where a DDS ends inside a bundle without an index (what follows may be another
new file, e.g. league tables).
"""

from __future__ import annotations

import struct

# DXGI_FORMAT -> (bytes per block, block side). 4x4 blocks for BC formats.
_DXGI = {
    **{f: (8, 4) for f in (70, 71, 72, 79, 80, 81)},                          # BC1, BC4
    **{f: (16, 4) for f in (73, 74, 75, 76, 77, 78, 82, 83, 84, 94, 95, 96, 97, 98, 99)},  # BC2/3/5/6H/7
    **{f: (4, 1) for f in (24, 26, 27, 28, 29, 30, 31, 32, 87, 88, 90, 91, 92, 93)},  # 32 bpp
    **{f: (8, 1) for f in (10, 11, 12, 13, 14, 15)},                          # 64 bpp
    **{f: (16, 1) for f in (2, 3, 4)},                                        # 128 bpp
    **{f: (2, 1) for f in (34, 35, 36, 37, 38, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59)},  # 16 bpp
    **{f: (1, 1) for f in (60, 61, 62, 63, 64, 65)},                          # 8 bpp
}
_FOURCC = {b"DXT1": (8, 4), b"DXT2": (16, 4), b"DXT3": (16, 4), b"DXT4": (16, 4), b"DXT5": (16, 4),
           b"ATI1": (8, 4), b"BC4U": (8, 4), b"BC4S": (8, 4), b"ATI2": (16, 4), b"BC5U": (16, 4),
           b"BC5S": (16, 4)}
_D3DFMT = {113: (8, 1), 116: (16, 1), 111: (2, 1), 112: (4, 1), 114: (4, 1), 115: (8, 1)}  # float/half

DDSCAPS2_CUBEMAP = 0x200
DDSCAPS2_VOLUME = 0x200000


def dds_size(data: bytes, offset: int = 0) -> int | None:
    """Total size of the DDS starting at ``offset``, or None if the format is not recognised."""
    if data[offset:offset + 4] != b"DDS " or len(data) < offset + 128:
        return None
    (hsize, flags, height, width, _pitch, depth, mips) = struct.unpack_from("<7I", data, offset + 4)
    if hsize != 124:
        return None
    pf_flags, fourcc, rgb_bits = struct.unpack_from("<I4sI", data, offset + 80)
    caps2 = struct.unpack_from("<I", data, offset + 112)[0]
    header = 128
    faces = 6 if caps2 & DDSCAPS2_CUBEMAP else 1
    array = 1
    if pf_flags & 0x4 and fourcc == b"DX10":
        if len(data) < offset + 148:
            return None
        dxgi, dim, misc, array = struct.unpack_from("<4I", data, offset + 128)[:4]
        header = 148
        fmt = _DXGI.get(dxgi)
        if misc & 0x4:  # TEXTURECUBE
            faces = 6
        array = max(1, array)
    elif pf_flags & 0x4:
        fmt = _FOURCC.get(fourcc) or _D3DFMT.get(int.from_bytes(fourcc, "little"))
    elif rgb_bits:
        fmt = (rgb_bits // 8, 1)
    else:
        fmt = None
    if fmt is None or width == 0 or height == 0:
        return None
    block_bytes, block = fmt
    mips = max(1, mips)
    depth = max(1, depth) if caps2 & DDSCAPS2_VOLUME else 1
    total = 0
    w, h, d = width, height, depth
    for _ in range(mips):
        bw = max(1, (w + block - 1) // block)
        bh = max(1, (h + block - 1) // block)
        total += bw * bh * block_bytes * d
        w, h, d = max(1, w // 2), max(1, h // 2), max(1, d // 2)
    return header + total * faces * array


def ogg_size(data: bytes, offset: int = 0, limit: int | None = None) -> int | None:
    """Size of an Ogg stream, walking its pages up to the one flagged as last (EOS)."""
    limit = len(data) if limit is None else limit
    pos = offset
    while pos + 27 <= limit and data[pos:pos + 4] == b"OggS":
        header_type = data[pos + 5]
        nseg = data[pos + 26]
        if pos + 27 + nseg > limit:
            return None
        body = sum(data[pos + 27:pos + 27 + nseg])
        pos += 27 + nseg + body
        if header_type & 0x04:  # end of stream
            return pos - offset if pos <= limit else None
    return None


def known_size(kind: str | None, data: bytes, offset: int, limit: int) -> int | None:
    """Exact size of a file in a self-describing format, when it can be computed."""
    if kind == "dds":
        return dds_size(data, offset)
    if kind == "ogg":
        return ogg_size(data, offset, limit)
    return None
