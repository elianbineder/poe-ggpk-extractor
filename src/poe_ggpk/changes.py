"""Table comparison between the current version and a reference one.

Used by ``poe-ggpk tables --changed`` and by the GUI.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .dat import DatFile, schema_mismatch
from .reference import Reference, digest


@dataclass
class TableChange:
    status: str              # "modified" or "new"
    name: str
    path: str
    rows_before: int | None
    rows_after: int
    width_before: int | None
    width_after: int
    schema: str              # "ok", "no schema" or "OUTDATED (...)"

    @property
    def delta(self) -> int | None:
        return None if self.rows_before is None else self.rows_after - self.rows_before


@dataclass
class UnknownTable:
    path: str
    rows: int
    row_size: int
    text_columns: int
    sample: str


@dataclass
class ChangeReport:
    changed: list[TableChange] = field(default_factory=list)
    unknown: list[UnknownTable] = field(default_factory=list)
    unchanged: int = 0
    missing: list[str] = field(default_factory=list)


def _tables_of(index, base: str, suffix: str) -> dict[str, str]:
    out = {}
    for f in index.files:
        p = f.path
        if p and p.startswith(base) and p.endswith(suffix) and "/" not in p[len(base):]:
            out[p[len(base):-len(suffix)]] = p
    return out


def unknown_tables(fs) -> list[UnknownTable]:
    """New unnamed tables of a reconstructed index (``_unknown/...datc64``)."""
    from .tablescan import find_tables, read_string

    out = []
    for f in fs.index.files:
        if not (f.path.startswith("_unknown/") and f.path.endswith((".datc64", ".datcl64"))):
            continue
        raw = fs.read(f.path)
        found = find_tables(raw)
        if not found:
            continue
        g = found[0]
        texts = [c for c in g.columns if c.kind == "string"]
        sample = ""
        if texts and g.rows:
            off = struct.unpack_from("<Q", raw, 4 + texts[0].offset)[0]
            sample = read_string(raw[g.marker:], off, 4 if g.wide else 2) or ""
        out.append(UnknownTable(f.path, g.rows, g.row_size, len(texts), sample))
    return out


def compare_tables(fs, reference: Reference, schema, extension: str = "datc64",
                   match=None) -> ChangeReport:
    """Modified/new/missing tables of ``fs`` compared to ``reference``."""
    ix = fs.index
    status = getattr(ix, "status", None)
    base = "data/balance/" if fs.game == 2 else "data/"
    suffix = "." + extension
    new_tables, old_tables = _tables_of(ix, base, suffix), _tables_of(reference.index, base, suffix)
    report = ChangeReport()
    for name, path in sorted(new_tables.items()):
        if match is not None and not match(name):
            continue
        if status is not None and status.get(path) in ("same", "matched"):
            report.unchanged += 1
            continue
        raw = fs.read(path)
        old_seg = reference.segment_for(path) if name in old_tables else None
        if old_seg is not None and old_seg.size == len(raw) and old_seg.digest == digest(raw):
            report.unchanged += 1
            continue
        new_dat = DatFile.from_path_and_bytes(path, raw)
        table = schema.table(name, fs.game) if schema is not None else None
        note = ("no schema" if table is None else
                "ok" if not schema_mismatch(table, new_dat) else
                f"OUTDATED ({table.row_size}B != {new_dat.row_size}B)")
        label = table.name if table else name
        if old_seg is None:
            report.changed.append(TableChange("new", label, path, None, new_dat.row_count, None,
                                              new_dat.row_size, note))
            continue
        old_raw = reference.read(path)
        old_width = DatFile.from_path_and_bytes(path, old_raw).row_size if old_raw else None
        report.changed.append(TableChange("modified", label, path,
                                          int.from_bytes(old_seg.prefix[:4], "little"), new_dat.row_count,
                                          old_width, new_dat.row_size, note))
    if status is not None:
        report.unknown = unknown_tables(fs)
    report.missing = sorted(n for n in old_tables if n not in new_tables)
    report.changed.sort(key=lambda c: (c.status, c.name))
    return report
