"""Tests against a real installation. Skipped when the game is not found.

Location: POE_GGPK variable or the default path of the standalone client.
"""

import os
from pathlib import Path

import pytest

from poe_ggpk import GameData, PoEFileSystem, Schema
from poe_ggpk.dat import DatFile, schema_mismatch
from poe_ggpk.hashing import ggpk_name_hash

DEFAULT = r"C:\Program Files (x86)\Grinding Gear Games\Path of Exile"
LOCATION = os.environ.get("POE_GGPK", DEFAULT)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not Path(LOCATION).exists(), reason="Game not installed"),
]


@pytest.fixture(scope="module")
def fs():
    with PoEFileSystem(LOCATION) as f:
        yield f


def test_index_resolves_paths(fs):
    assert len(fs.index.files) > 100_000
    # A few orphan files are tolerable (it happens in PoE2), not more.
    assert fs.index.unresolved < 50


def test_ggpk_entry_hashes_match(fs):
    if fs.ggpk is None:
        pytest.skip("installation without GGPK")
    root = fs.ggpk.read_record(fs.ggpk.root_offset)
    for h, off in root.children:
        rec = fs.ggpk.read_record(off)
        assert ggpk_name_hash(rec.name) == h


def test_read_table_with_schema(fs):
    schema = Schema.load()
    gd = GameData(fs, schema)
    table, dat = gd.open("BaseItemTypes")
    assert schema_mismatch(table, dat) is None
    rows = gd.resolve("BaseItemTypes")
    chaos = next(r for r in rows if r["Id"] == "Metadata/Items/Currency/CurrencyRerollRare")
    assert chaos["Name"] == "Chaos Orb"
    assert chaos["ItemClassesKey" if fs.game == 1 else "ItemClass"] == "StackableCurrency"


def test_spanish_translation(fs):
    gd = GameData(fs, Schema.load(), language="Spanish")
    rows = gd.rows("BaseItemTypes")
    assert any(r["Name"] == "Orbe de caos" for r in rows)


def test_datcl64_matches_datc64(fs):
    base = "data/balance/" if fs.game == 2 else "data/"
    if not fs.exists(base + "stats.datcl64"):
        pytest.skip("This version has no .datcl64")
    schema = Schema.load()
    table = schema.table("Stats", fs.game)
    a = DatFile.from_path_and_bytes("x.datc64", fs.read(base + "stats.datc64")).read_rows(table)
    b = DatFile.from_path_and_bytes("x.datcl64", fs.read(base + "stats.datcl64")).read_rows(table)
    assert [r["Id"] for r in a] == [r["Id"] for r in b]


def test_reconstruct_fake_torrent(fs, tmp_path):
    """GGPK without index (like the torrent one): identical, modified and newly named bundle."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from fake_torrent import make_fake_torrent

    from poe_ggpk.reconstruct import reconstruct
    from poe_ggpk.reference import Reference

    if fs.game != 1:
        pytest.skip("the fake GGPK is built from PoE1 bundles")
    truth = make_fake_torrent(tmp_path, poe1=LOCATION)
    rec, report = reconstruct(tmp_path, Reference.from_location(LOCATION), schema=Schema.load())
    assert report.bundles == {"same": 1, "changed": 1, "new": 1}
    assert all(rec.status[p] == "same" for p in truth["same"])
    for m in truth["modified"]:
        f = rec.get(m["paths"][0])
        assert (rec.status[f.path], f.offset, f.size) == ("modified", m["offset"], m["size"])
    assert all(rec.get(p) is None for p in truth["deleted"])
    unknown = {(rec.bundles[f.bundle_index].name, f.offset, f.size) for f in rec.files
               if rec.status[f.path] == "unknown"}
    for n in truth["new_content"]:
        assert (n["bundle"], n["offset"], n["size"]) in unknown

    # The reconstructed index is used like a normal one.
    rec.save(tmp_path / "rec.index.gz")
    with PoEFileSystem(tmp_path, index_file=tmp_path / "rec.index.gz") as fake:
        m = truth["modified"][0]
        assert len(fake.read(m["paths"][0])) == m["size"]


POE2 = r"C:\Program Files (x86)\Grinding Gear Games\Path of Exile 2"


@pytest.mark.skipif(not Path(POE2).exists(), reason="PoE2 is needed to simulate new tables")
def test_new_league_tables_are_found_and_readable(fs, tmp_path, capsys):
    """New tables (only present in PoE2) inserted into the fake torrent: they are detected with
    exact boundaries, appear in `tables --changed` and can be read without a schema."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from fake_torrent import make_fake_torrent

    from poe_ggpk.cli import main
    from poe_ggpk.reconstruct import reconstruct
    from poe_ggpk.reference import Reference
    from poe_ggpk.tablescan import read_unknown_table

    if fs.game != 1:
        pytest.skip("the fake GGPK is built from PoE1 bundles")
    truth = make_fake_torrent(tmp_path, poe1=LOCATION, poe2=POE2)
    rec, _ = reconstruct(tmp_path, Reference.from_location(LOCATION), schema=Schema.load())
    rec.save(tmp_path / "rec.index.gz")

    found = {(f.offset, f.size): f.path for f in rec.files if f.path.endswith(".datc64")
             and f.path.startswith("_unknown/")}
    poe2 = PoEFileSystem(POE2)
    real_tables = GameData(poe2, Schema.load())
    with PoEFileSystem(tmp_path, index_file=tmp_path / "rec.index.gz") as fake:
        for t in truth["new_tables"]:
            path = found[(t["offset"], t["size"])]  # exact boundaries
            _, rows = read_unknown_table(fake.read(path))
            name = t["source"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
            real = real_tables.rows(name)
            real_texts = {v for r in real for v in r.values() if isinstance(v, str) and v}
            got_texts = {v for r in rows for k, v in r.items() if k.startswith("string_") and v}
            assert got_texts <= real_texts            # nothing invented
            assert len(got_texts) >= 0.9 * len(real_texts)

    main(["tables", "--changed", "--ggpk", str(tmp_path), "--index", str(tmp_path / "rec.index.gz")])
    out = capsys.readouterr().out
    assert "modified" in out and "New unnamed tables (3)" in out


def test_extract_and_convert_icon(fs, tmp_path):
    pytest.importorskip("PIL")
    from poe_ggpk.convert import dds_to_png

    path = "art/2ditems/currency/currencyrerollrare.dds"
    fs.extract([path], tmp_path)
    data = (tmp_path / path).read_bytes()
    assert data[:4] == b"DDS "
    assert dds_to_png(data)[:8] == b"\x89PNG\r\n\x1a\n"


def test_concurrent_reads_are_consistent(fs):
    """The GUI reads from several threads: the result must match sequential reads."""
    import random
    from concurrent.futures import ThreadPoolExecutor

    paths = [f.path for f in random.Random(5).sample(fs.index.files, 400) if f.path and f.size < 2_000_000]
    expected = {p: fs.read(p) for p in paths}
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = dict(zip(paths, pool.map(fs.read, paths)))
    assert got == expected
