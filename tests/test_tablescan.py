"""Tests of unnamed/schema-less table detection and of DDS/Ogg sizes."""

import struct

from poe_ggpk.dat import NULL64
from poe_ggpk.ddsinfo import dds_size, ogg_size
from poe_ggpk.tablescan import find_tables, read_unknown_table


def build_table(rows: list[tuple[str, int, int | None, list[int], bool]], wide: bool = False) -> bytes:
    """Table with columns: text (8) | i32 (4) | key (16) | i32 array (16) | bool (1) = 45 bytes."""
    enc = "utf-32-le" if wide else "utf-16-le"
    var = bytearray(b"\xbb" * 8)
    fixed = bytearray()
    # As in real tables, strings are almost always stored one after another.
    text_offsets = {}
    for text, *_ in rows:
        if text not in text_offsets:
            text_offsets[text] = len(var)
            var += text.encode(enc) + b"\0\0\0\0"
    for text, value, key, arr, flag in rows:
        s_off = text_offsets[text]
        a_off = len(var)
        var += b"".join(struct.pack("<i", x) for x in arr)
        fixed += struct.pack("<Q", s_off) + struct.pack("<i", value)
        fixed += struct.pack("<QQ", NULL64, NULL64) if key is None else struct.pack("<QQ", key, 0)
        fixed += struct.pack("<QQ", len(arr), a_off if arr else 0) + struct.pack("<?", flag)
    return struct.pack("<I", len(rows)) + bytes(fixed) + bytes(var)


ROWS_A = [(f"Element{i}", i * 10, None if i % 3 == 0 else i, list(range(i % 4)), i % 2 == 0)
          for i in range(12)]
ROWS_B = [(f"Something else {i}", -i, i, [7] * (i % 3), True) for i in range(9)]


def test_infers_columns_and_values():
    raw = build_table(ROWS_A)
    tg, rows = read_unknown_table(raw)
    assert (tg.rows, tg.row_size, tg.wide) == (12, 45, False)
    assert [(c.offset, c.kind) for c in tg.columns if c.kind in ("string", "key", "array")] == [
        (0, "string"), (12, "key"), (28, "array")]
    assert rows[4]["string_0"] == "Element4"
    assert rows[4]["i32_8"] == 40
    assert rows[3]["key_12"] is None and rows[4]["key_12"] == 4
    assert rows[3]["array_28"] == [0, 1, 2]


def test_detects_wide_strings():
    tg, rows = read_unknown_table(build_table(ROWS_A, wide=True))
    assert tg.wide and tg.extension == "datcl64"
    assert rows[1]["string_0"] == "Element1"


def test_finds_consecutive_tables():
    a, b, c = build_table(ROWS_A), build_table(ROWS_A, wide=True), build_table(ROWS_B)
    blob = a + b + c
    found = find_tables(blob)
    assert [(t.offset, t.rows, t.extension) for t in found] == [
        (0, 12, "datc64"), (len(a), 12, "datcl64"), (len(a) + len(b), 9, "datc64")]
    assert [t.end for t in found] == [len(a), len(a) + len(b), len(blob)]


def test_no_tables_in_random_data():
    import random

    assert find_tables(random.Random(3).randbytes(50_000)) == []


def _dds(width: int, height: int, mips: int, dxgi: int) -> bytes:
    head = bytearray(148)
    head[0:4] = b"DDS "
    struct.pack_into("<7I", head, 4, 124, 0, height, width, 0, 0, mips)
    struct.pack_into("<I4s", head, 80, 0x4, b"DX10")
    struct.pack_into("<4I", head, 128, dxgi, 3, 0, 1)
    return bytes(head)


def test_dds_size_bc7_with_mips():
    # 64x64 BC7 (16 B per 4x4 block) with 3 mips: 16x16 + 8x8 + 4x4 blocks.
    expected = 148 + (256 + 64 + 16) * 16
    assert dds_size(_dds(64, 64, 3, 98) + b"\0" * 9999) == expected


def test_ogg_size_stops_at_end_of_stream():
    def page(flags: int, body: bytes) -> bytes:
        return b"OggS\x00" + bytes([flags]) + b"\0" * 20 + bytes([1, len(body)]) + body
    stream = page(0x02, b"a" * 10) + page(0x00, b"b" * 20) + page(0x04, b"c" * 5)
    assert ogg_size(stream + b"otra cosa") == len(stream)
