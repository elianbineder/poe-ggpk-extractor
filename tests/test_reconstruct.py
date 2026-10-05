"""Tests of the aligner used to reconstruct missing indexes."""

import random

from poe_ggpk.reconstruct import align
from poe_ggpk.reference import Segment, fingerprint_bundle

rng = random.Random(42)


def blob(size: int, kind: str = "bin") -> bytes:
    body = rng.randbytes(size)
    if kind == "dds":
        return b"DDS \x7c\x00\x00\x00" + body
    if kind == "text":
        text = "".join(rng.choice("abcdefghij \r\n") for _ in range(size // 2))
        return b"\xff\xfe" + ("version 3" + text).encode("utf-16-le")
    return body


def old_bundle(files: list[tuple[str, bytes]]) -> tuple[bytes, list[Segment]]:
    data = b""
    segs = []
    for name, content in files:
        segs.append(Segment(len(data), len(content), [name]))
        data += content
    fingerprint_bundle(data, segs)
    return data, segs


def summary(pieces):
    return [(p.segment.paths[0] if p.segment else None, p.status, p.offset, p.size) for p in pieces]


def located(pieces, name):
    return next(p for p in pieces if p.segment and p.segment.paths[0] == name)


FILES = [
    ("a.dds", blob(3000, "dds")),
    ("b.txt", blob(800, "text")),
    ("c.datc64", blob(5000)),
    ("d.dds", blob(4000, "dds")),
    ("e.ogg", b"OggS\x00\x02" + rng.randbytes(2000)),
]


def test_identical_bundle_all_matched():
    data, segs = old_bundle(FILES)
    pieces = align(data, segs)
    assert {p.status for p in pieces} == {"matched"}
    assert [p.offset for p in pieces] == [s.offset for s in segs]


def test_modified_file_changes_size():
    _, segs = old_bundle(FILES)
    new_c = blob(7123)  # the table grew (new rows) and has no recognisable signature
    new = FILES[0][1] + FILES[1][1] + new_c + FILES[3][1] + FILES[4][1]
    pieces = align(new, segs)
    c = located(pieces, "c.datc64")
    assert (c.status, c.offset, c.size) == ("modified", len(FILES[0][1]) + len(FILES[1][1]), len(new_c))
    assert new[c.offset:c.offset + c.size] == new_c
    assert located(pieces, "d.dds").status == "matched"
    assert located(pieces, "e.ogg").status == "matched"


def test_inserted_new_file_is_unknown_with_type():
    _, segs = old_bundle(FILES)
    extra = blob(2500, "dds")
    new = FILES[0][1] + FILES[1][1] + extra + FILES[2][1] + FILES[3][1] + FILES[4][1]
    pieces = align(new, segs)
    unknown = [p for p in pieces if p.status == "unknown"]
    assert len(unknown) == 1 and unknown[0].kind == "dds" and unknown[0].size == len(extra)
    assert all(p.status == "matched" for p in pieces if p.segment)


def test_removed_file_is_missing():
    _, segs = old_bundle(FILES)
    new = FILES[0][1] + FILES[1][1] + FILES[3][1] + FILES[4][1]
    pieces = align(new, segs)
    names = {p.segment.paths[0] for p in pieces if p.segment}
    assert names == {"a.dds", "b.txt", "d.dds", "e.ogg"}
    assert all(p.status == "matched" for p in pieces)


def test_reordered_files_are_found():
    _, segs = old_bundle(FILES)
    new = FILES[3][1] + FILES[0][1] + FILES[1][1] + FILES[2][1] + FILES[4][1]
    pieces = align(new, segs)
    assert {p.status for p in pieces} == {"matched"}
    assert located(pieces, "d.dds").offset == 0


def test_several_modified_files_stay_unnamed():
    # When several files are missing in the same range nothing is guessed: unnamed beats misnamed.
    files = [("x.txt", blob(500, "text")), ("y.txt", blob(600, "text")),
             ("z.dds", blob(900, "dds")), ("w.dds", blob(1000, "dds"))]
    _, segs = old_bundle(files)
    nx, ny = blob(700, "text"), blob(650, "text")
    new = nx + ny + files[2][1] + files[3][1]
    pieces = align(new, segs)
    named = {p.segment.paths[0] for p in pieces if p.segment}
    assert named == {"z.dds", "w.dds"}
    assert [(p.offset, p.size) for p in pieces if p.status == "unknown"] == [(0, len(nx)), (len(nx), len(ny))]


def test_small_file_next_to_modified_one_is_peeled_off():
    files = [("big.dds", blob(3000, "dds")), ("table.datc64", blob(4000)), ("tiny", b""),
             ("other.dds", blob(2000, "dds"))]
    _, segs = old_bundle(files)
    new_table = blob(4500)
    new = files[0][1] + new_table + files[2][1] + files[3][1]
    pieces = align(new, segs)
    t = located(pieces, "table.datc64")
    assert (t.status, t.offset, t.size) == ("modified", len(files[0][1]), len(new_table))
    assert located(pieces, "tiny").status == "matched"


def test_small_files_in_place_and_empty_files():
    files = [("tiny1", b"\x01\x02\x03"), ("big", blob(4000)), ("tiny2", b"abc"), ("empty", b"")]
    _, segs = old_bundle(files)
    new = files[0][1] + files[1][1] + files[2][1]
    pieces = align(new, segs)
    assert {p.segment.paths[0]: p.status for p in pieces} == {
        "tiny1": "matched", "big": "matched", "tiny2": "matched", "empty": "matched"}


# -- adjacent tables split by size (schema) ------------------------------------------------
import struct  # noqa: E402

from poe_ggpk.dat import DatFile  # noqa: E402
from poe_ggpk.reconstruct import ReconstructedIndex, Sizer  # noqa: E402
from poe_ggpk.index import BundleRecord, FileRecord  # noqa: E402
from poe_ggpk.schema import Column, Table  # noqa: E402

TABLE = Table("Things", 3, [Column("Id", "string"), Column("Value", "i32")], [])


def make_table(ids: list[str]) -> bytes:
    var = b"\xbb" * 8
    rows = b""
    for n, s in enumerate(ids):
        rows += struct.pack("<Qi", len(var), n)
        var += s.encode("utf-16-le") + b"\0\0\0\0"
    return struct.pack("<I", len(ids)) + rows + var


class FakeSchema:
    def table(self, name, game):
        return TABLE if name.lower() == "things" else None


class FakeReference:
    trusted_tables = {"data/things.datc64", "data/french/things.datc64"}
    fs = None


def test_content_end_matches_real_size():
    raw = make_table(["One", "Two", "Three"])
    assert DatFile(raw).content_end(TABLE) == len(raw)
    assert DatFile(raw + b"trailing garbage").content_end(TABLE) == len(raw)


def test_adjacent_modified_tables_are_split_with_schema():
    # Typical patch case: a table and its translation change together and stay adjacent.
    dds_x, dds_y = blob(3000, "dds"), blob(2000, "dds")
    a_old, b_old = make_table(["One", "Two"]), make_table(["Un", "Deux"])
    a_new, b_new = make_table(["One", "Two", "Three"]), make_table(["Un", "Deux", "Trois"])
    _, segs = old_bundle([("x.dds", dds_x), ("data/things.datc64", a_old),
                          ("data/french/things.datc64", b_old), ("y.dds", dds_y)])
    new = dds_x + a_new + b_new + dds_y

    # Without a schema there is no safe way to split them: they stay unnamed.
    without = align(new, segs)
    assert not [p for p in without if p.segment and p.segment.paths[0].endswith(".datc64")]

    pieces = align(new, segs, Sizer(FakeSchema(), 1, FakeReference()))
    a, b = located(pieces, "data/things.datc64"), located(pieces, "data/french/things.datc64")
    assert (a.status, a.offset, a.size) == ("modified", len(dds_x), len(a_new))
    assert (b.status, b.offset, b.size) == ("modified", len(dds_x) + len(a_new), len(b_new))


def test_untrusted_table_is_not_split():
    class Untrusted(FakeReference):
        trusted_tables: set[str] = set()

    dds_x, dds_y = blob(3000, "dds"), blob(2000, "dds")
    _, segs = old_bundle([("x.dds", dds_x), ("data/things.datc64", make_table(["A"])),
                          ("data/french/things.datc64", make_table(["B"])), ("y.dds", dds_y)])
    new = dds_x + make_table(["A", "AA"]) + make_table(["B", "BB"]) + dds_y
    pieces = align(new, segs, Sizer(FakeSchema(), 1, Untrusted()))
    assert not [p for p in pieces if p.segment and p.segment.paths[0].endswith(".datc64")]


def test_reconstructed_index_roundtrip(tmp_path):
    files = [FileRecord(0, 0, 0, 10, "data/mods.datc64"), FileRecord(0, 0, 10, 5, "_unknown/B/0000000010.bin")]
    ix = ReconstructedIndex([BundleRecord(0, "B", 15)], files,
                            {"data/mods.datc64": "matched", "_unknown/B/0000000010.bin": "unknown"},
                            {"reference": "test"})
    ix.save(tmp_path / "i.gz")
    back = ReconstructedIndex.load(tmp_path / "i.gz")
    assert back.get("Data/Mods.datc64").size == 10
    assert back.status["_unknown/B/0000000010.bin"] == "unknown"
    assert back.meta["reference"] == "test"
    assert back.bundles[0].name == "B"
