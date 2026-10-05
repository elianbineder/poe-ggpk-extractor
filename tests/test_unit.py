"""Unit tests with synthetic data (the game does not need to be installed)."""

import random
import struct

import pytest

from poe_ggpk import bundle as bundle_mod
from poe_ggpk.bundle import Bundle, BytesSource
from poe_ggpk.convert import decode_text
from poe_ggpk.dat import NULL64, DatFile
from poe_ggpk.ggpk import GGPK
from poe_ggpk.hashing import (
    ROOT_HASH_FNV,
    ROOT_HASH_MURMUR,
    detect_algorithm,
    fnv1a64,
    ggpk_name_hash,
    murmur64a,
    murmur64a_many,
    path_hash,
)
from poe_ggpk.index import BundleIndex, decode_paths
from poe_ggpk.schema import Column, Table


# -- hashing -----------------------------------------------------------------------
def test_root_hashes_identify_algorithms():
    assert murmur64a(b"") == ROOT_HASH_MURMUR
    assert fnv1a64(b"") == ROOT_HASH_FNV
    assert detect_algorithm(ROOT_HASH_MURMUR) == "murmur"
    assert detect_algorithm(ROOT_HASH_FNV) == "fnv"
    with pytest.raises(ValueError):
        detect_algorithm(123)


def test_murmur_ignores_trailing_slash_and_case():
    assert murmur64a(b"data/") == murmur64a(b"data")
    assert path_hash("Data/Mods.datc64") == path_hash("data/mods.datc64")


def test_vectorized_murmur_matches_reference():
    rng = random.Random(0)
    alphabet = b"abcdefghijklmnopqrstuvwxyz0123456789/_."
    paths = [bytes(rng.choice(alphabet) for _ in range(n)) for n in list(range(0, 40)) * 5 + [200, 255]]
    paths = [p.rstrip(b"/") for p in paths]
    assert murmur64a_many(paths) == [murmur64a(p) for p in paths]


def test_ggpk_name_hash_is_case_insensitive():
    assert ggpk_name_hash("Bundles2") == ggpk_name_hash("bundles2")


# -- compressed index paths -----------------------------------------------------------
def _cmd(n: int, s: bytes | None = None) -> bytes:
    out = struct.pack("<I", n)
    if s is not None:
        out += s + b"\0"
    return out


def test_decode_paths_base_and_generation_phases():
    data = (
        _cmd(0)                       # enter the base phase
        + _cmd(1, b"art/")            # temp[0] = "art/"
        + _cmd(1, b"textures/")       # temp[1] = "art/textures/"
        + _cmd(0)                     # generation phase
        + _cmd(2, b"a.dds")           # temp[1] + "a.dds"
        + _cmd(1, b"b.txt")           # temp[0] + "b.txt"
        + _cmd(9, b"loose.bin")       # index out of range: no prefix
    )
    assert list(decode_paths(data, 0, len(data))) == [
        b"art/textures/a.dds",
        b"art/b.txt",
        b"loose.bin",
    ]


# -- bundles ---------------------------------------------------------------------------------
def _fake_bundle(payload: bytes, chunk_size: int = 4) -> bytes:
    """Bundle with identity 'compression' (the decompressor is patched in the test)."""
    chunks = [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)] or [b""]
    head = struct.pack(
        "<iiiiiqqii4i",
        len(payload), sum(map(len, chunks)), 48 + 4 * len(chunks), 9, 1,
        len(payload), sum(map(len, chunks)), len(chunks), chunk_size, 0, 0, 0, 0,
    )
    return head + struct.pack(f"<{len(chunks)}i", *map(len, chunks)) + b"".join(chunks)


@pytest.fixture
def identity_oodle(monkeypatch):
    monkeypatch.setattr(bundle_mod, "_oodle_decompress", lambda data, size: data[:size])


def test_bundle_random_access(identity_oodle):
    payload = bytes(range(50))
    b = Bundle(BytesSource(_fake_bundle(payload, chunk_size=8)))
    assert b.header.chunk_count == 7
    assert b.read() == payload
    assert b.read(5, 20) == payload[5:25]
    assert b.read(49, 1) == payload[49:]
    with pytest.raises(bundle_mod.BundleError):
        b.read(45, 10)


def test_index_parsing(identity_oodle):
    paths_blob = _cmd(0) + _cmd(1, b"data/") + _cmd(0) + _cmd(1, b"mods.datc64") + _cmd(1, b"stats.datc64")
    path_bundle = _fake_bundle(paths_blob, chunk_size=64)
    files = [(path_hash("data/mods.datc64"), 0, 0, 10), (path_hash("data/stats.datc64"), 0, 10, 5)]
    body = struct.pack("<i", 1) + struct.pack("<i", 4) + b"Data" + struct.pack("<i", 15)
    body += struct.pack("<i", len(files)) + b"".join(struct.pack("<QIII", *f) for f in files)
    body += struct.pack("<i", 1) + struct.pack("<QIII", ROOT_HASH_MURMUR, 0, len(paths_blob), len(paths_blob))
    body += path_bundle
    ix = BundleIndex(BytesSource(_fake_bundle(body, chunk_size=1024)))
    assert ix.hash_algorithm == "murmur"
    assert ix.unresolved == 0
    assert ix.bundles[0].path == "Data.bundle.bin"
    assert ix.get("Data/Stats.datc64").offset == 10


# -- GGPK -------------------------------------------------------------------------------------
def _ggpk_file_record(name: str, data: bytes) -> bytes:
    n = (name + "\0").encode("utf-16-le")
    length = 12 + 32 + len(n) + len(data)
    return struct.pack("<I4sI", length, b"FILE", len(name) + 1) + b"\0" * 32 + n + data


def _ggpk_dir_record(name: str, children: list[tuple[str, int]]) -> bytes:
    n = (name + "\0").encode("utf-16-le")
    entries = b"".join(struct.pack("<IQ", ggpk_name_hash(c), off) for c, off in sorted(
        children, key=lambda c: ggpk_name_hash(c[0])))
    length = 16 + 32 + len(n) + len(entries)
    return struct.pack("<I4sII", length, b"PDIR", len(name) + 1, len(children)) + b"\0" * 32 + n + entries


def test_ggpk_reading(tmp_path):
    # Layout: header | FILE hello.txt | PDIR sub (with FILE x.bin) | PDIR root | FREE
    header_size = 28
    f1 = _ggpk_file_record("hello.txt", "héllo!".encode("utf-16-le"))
    f1_off = header_size
    f2 = _ggpk_file_record("x.bin", b"\x01\x02\x03")
    f2_off = f1_off + len(f1)
    sub = _ggpk_dir_record("Sub", [("x.bin", f2_off)])
    sub_off = f2_off + len(f2)
    root = _ggpk_dir_record("", [("hello.txt", f1_off), ("Sub", sub_off)])
    root_off = sub_off + len(sub)
    free = struct.pack("<I4s", 8, b"FREE")
    free_off = root_off + len(root)
    head = struct.pack("<I4sIQQ", 28, b"GGPK", 3, root_off, free_off)
    p = tmp_path / "Content.ggpk"
    p.write_bytes(head + f1 + f2 + sub + root + free)

    with GGPK(p) as g:
        assert g.version == 3
        assert set(g.files) == {"hello.txt", "Sub/x.bin"}
        e = g.find("sub/X.BIN")
        assert e is not None and g.read(e) == b"\x01\x02\x03"
        assert decode_text(g.read(g.find("hello.txt"))) == "héllo!"
        assert g.find("nope.txt") is None


# -- .dat tables ----------------------------------------------------------------------------------
def _build_dat(rows: list[bytes], var: bytes) -> bytes:
    return struct.pack("<I", len(rows)) + b"".join(rows) + var


@pytest.mark.parametrize("wide", [False, True])
def test_dat_reading(wide):
    enc = "utf-32-le" if wide else "utf-16-le"
    term = b"\0\0\0\0"
    var = b"\xbb" * 8
    s1_off = len(var); var += "One".encode(enc) + term
    s2_off = len(var); var += "Two".encode(enc) + term
    arr_off = len(var); var += struct.pack("<3i", 7, 8, 9)
    table = Table("Test", 3, [
        Column("Id", "string"),
        Column("Value", "i32"),
        Column("Ref", "foreignrow", references="Other"),
        Column("Parent", "row"),
        Column("List", "i32", array=True),
        Column("Range", "i32", interval=True),
        Column("Flag", "bool"),
    ], [])
    def row(s_off, value, ref, parent, arr, flag):
        return (struct.pack("<Q", s_off) + struct.pack("<i", value) + struct.pack("<QQ", ref, 0)
                + struct.pack("<Q", parent) + struct.pack("<QQ", *arr) + struct.pack("<ii", 1, 5)
                + struct.pack("<?", flag))
    rows = [row(s1_off, 10, 3, NULL64, (3, arr_off), True), row(s2_off, -1, NULL64, 0, (0, 0), False)]
    dat = DatFile(_build_dat(rows, var), wide_strings=wide)
    assert dat.row_count == 2 and dat.row_size == table.row_size
    out = dat.read_rows(table, strict=True)
    assert out[0] == {"_index": 0, "Id": "One", "Value": 10, "Ref": 3, "Parent": None,
                      "List": [7, 8, 9], "Range": [1, 5], "Flag": True}
    assert out[1]["Id"] == "Two" and out[1]["Ref"] is None and out[1]["Parent"] == 0 and out[1]["List"] == []


def test_dat_detects_row_size_with_magic_inside_rows():
    # A 0xBB*8 value inside the rows must not be mistaken for the start of the variable section.
    rows = [b"\xbb" * 8 + b"\x01\x00", b"\x00" * 10, b"\x00" * 10]
    dat = DatFile(_build_dat(rows, b"\xbb" * 8))
    assert dat.row_size == 10
