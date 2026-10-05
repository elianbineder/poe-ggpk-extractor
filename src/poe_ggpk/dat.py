"""Reader for data tables (.datc64 / .datcl64).

Layout::

    uint32 row_count
    byte   fixed[row_count * row_size]     fixed-width rows
    byte   variable[...]                   starts with 8 bytes 0xBB

The row width is not stored: it is derived by finding the 0xBB*8 marker (at a
position that is a multiple of ``row_count``). Strings and arrays store an
offset relative to the start of the variable section (marker included).

* ``.datc64``: UTF-16LE strings terminated by ``00 00 00 00``.
* ``.datcl64``: same layout but UTF-32LE strings (found empirically).

Null row / foreign-key indexes are 0xFEFEFEFEFEFEFEFE.
"""

from __future__ import annotations

import csv
import io
import json
import struct
from dataclasses import dataclass
from typing import Any, Callable

from .schema import Column, Schema, Table

MAGIC = b"\xbb" * 8
NULL64 = 0xFEFEFEFEFEFEFEFE
NULL32 = 0xFEFEFEFE

LANGUAGES = [
    "English", "French", "German", "Japanese", "Korean", "Portuguese",
    "Russian", "Spanish", "Thai", "Traditional Chinese",
]

_SCALAR_FMT = {
    "bool": "?", "i8": "b", "u8": "B", "i16": "h", "u16": "H",
    "i32": "i", "u32": "I", "f32": "f", "i64": "q", "u64": "Q", "f64": "d",
}


class DatError(Exception):
    pass


@dataclass(slots=True)
class ColumnLayout:
    column: Column
    name: str
    offset: int


class DatFile:
    def __init__(self, data: bytes, wide_strings: bool = False):
        if len(data) < 4 + len(MAGIC):
            raise DatError(".dat file too small")
        self.data = data
        self.row_count = struct.unpack_from("<I", data)[0]
        boundary = self._find_boundary()
        self.row_size = (boundary - 4) // self.row_count if self.row_count else 0
        self.fixed = memoryview(data)[4:boundary]
        self.var = data[boundary:]
        self.wide_strings = wide_strings
        self._char = 4 if wide_strings else 2
        self._encoding = "utf-32-le" if wide_strings else "utf-16-le"

    @classmethod
    def from_path_and_bytes(cls, path: str, data: bytes) -> "DatFile":
        return cls(data, wide_strings=path.lower().endswith(".datcl64"))

    def _find_boundary(self) -> int:
        """First marker whose position is a multiple of row_count.

        A boundary at 0 (zero-width rows) is only accepted when there is no other,
        because it also shows up when the first row starts with 0xBB bytes.
        """
        pos = 4
        zero_width = None
        while True:
            i = self.data.find(MAGIC, pos)
            if i < 0:
                if zero_width is not None:
                    return zero_width
                raise DatError("Variable section not found (0xBB marker)")
            if self.row_count == 0:
                return i
            if (i - 4) % self.row_count == 0:
                if i > 4:
                    return i
                zero_width = i
            pos = i + 1

    # -- primitives ---------------------------------------------------------------
    def string_at(self, offset: int) -> str:
        """Read a string terminated by 4 zero bytes, aligned to the character size."""
        var = self.var
        c = self._char
        end = offset
        while True:
            end = var.find(b"\0\0\0\0", end)
            if end < 0:
                end = len(var)
                break
            if (end - offset) % c == 0:
                break
            end += 1
        return var[offset:end].decode(self._encoding, errors="replace")

    def raw_row(self, i: int) -> bytes:
        return bytes(self.fixed[i * self.row_size:(i + 1) * self.row_size])

    # -- schema-based reading --------------------------------------------------------
    def layout(self, table: Table) -> list[ColumnLayout]:
        out: list[ColumnLayout] = []
        offset = 0
        for n, col in enumerate(table.columns):
            out.append(ColumnLayout(col, col.name or f"Unknown{n}", offset))
            offset += col.size
        return out

    def _scalar_reader(self, col: Column) -> tuple[Callable[[bytes | memoryview, int], Any], int]:
        """Return (function reading one value at buffer+offset, element size)."""
        t = col.type
        if t == "string":
            def rd(buf, off):
                return self.string_at(struct.unpack_from("<Q", buf, off)[0])
            return rd, 8
        if t in ("row", "foreignrow"):
            def rd(buf, off):
                v = struct.unpack_from("<Q", buf, off)[0]
                return None if v == NULL64 else v
            return rd, 8 if t == "row" else 16
        if t == "enumrow":
            def rd(buf, off):
                v = struct.unpack_from("<I", buf, off)[0]
                return None if v == NULL32 else v
            return rd, 4
        fmt = _SCALAR_FMT.get(t)
        if fmt is None:
            raise DatError(f"Unsupported type: {t}")
        s = struct.Struct("<" + fmt)
        return (lambda buf, off: s.unpack_from(buf, off)[0]), s.size

    def column_reader(self, layout: ColumnLayout) -> Callable[[int], Any]:
        col = layout.column
        base = layout.offset
        rs = self.row_size
        fixed = self.fixed
        if col.array:
            if col.type == "array":
                # Array of unknown type: only the element count is reported.
                return lambda r: {"unknown_array_length": struct.unpack_from("<Q", fixed, r * rs + base)[0]}
            one, size = self._scalar_reader(col)
            var = self.var

            def read_array(r: int) -> list[Any]:
                count, off = struct.unpack_from("<QQ", fixed, r * rs + base)
                if count == 0:
                    return []
                if off + count * size > len(var):
                    raise DatError(f"Array out of range in column {layout.name}, row {r}")
                return [one(var, off + i * size) for i in range(count)]
            return read_array
        one, size = self._scalar_reader(col)
        if col.interval:
            return lambda r: [one(fixed, r * rs + base), one(fixed, r * rs + base + size)]
        return lambda r: one(fixed, r * rs + base)

    def content_end(self, table: Table) -> int | None:
        """Total file size according to the data referenced by its rows.

        The variable section ends at the last referenced item (string or array), so with
        the schema the end of the table can be found even when other bytes follow it
        (used to split adjacent tables inside a bundle without an index).
        Matched the real size for 99 % of PoE1 tables. Returns None if the schema does
        not fit or has columns of unknown type.
        """
        if self.row_count == 0 or table.row_size != self.row_size:
            return None
        if any(c.type == "array" for c in table.columns):
            return None
        enc = self._encoding
        end_var = len(MAGIC)

        def string_end(off: int) -> int:
            return off + len(self.string_at(off).encode(enc)) + 4

        for lay in self.layout(table):
            c = lay.column
            if not (c.array or c.type == "string"):
                continue
            for r in range(self.row_count):
                base = r * self.row_size + lay.offset
                if c.array:
                    count, off = struct.unpack_from("<QQ", self.fixed, base)
                    if count == 0:
                        continue
                    _, elem = self._scalar_reader(c)
                    end_var = max(end_var, off + count * elem)
                    if c.type == "string":
                        for i in range(count):
                            end_var = max(end_var, string_end(struct.unpack_from("<Q", self.var, off + i * 8)[0]))
                else:
                    end_var = max(end_var, string_end(struct.unpack_from("<Q", self.fixed, base)[0]))
        if end_var > len(self.var):
            return None
        return 4 + self.row_count * self.row_size + end_var

    def read_rows(self, table: Table, strict: bool = False) -> list[dict[str, Any]]:
        """Read every row as a dict using the schema.

        If the schema describes fewer bytes than the real row, the known columns are
        read; if it describes more, columns that do not fit are skipped
        (or an exception is raised with ``strict``).
        """
        layout = self.layout(table)
        usable = [l for l in layout if l.offset + l.column.size <= self.row_size]
        if strict and (len(usable) != len(layout) or table.row_size != self.row_size):
            raise DatError(
                f"The schema of {table.name} spans {table.row_size} bytes but rows are {self.row_size} bytes")
        readers = [(l.name, self.column_reader(l)) for l in usable]
        rows = []
        for r in range(self.row_count):
            row: dict[str, Any] = {"_index": r}
            for name, rd in readers:
                try:
                    row[name] = rd(r)
                except (struct.error, DatError, UnicodeDecodeError):
                    row[name] = None
            rows.append(row)
        return rows


def schema_mismatch(table: Table, dat: DatFile) -> str | None:
    # Without rows the width cannot be derived, so there is nothing to compare.
    if dat.row_count == 0 or table.row_size == dat.row_size:
        return None
    return (f"The schema of '{table.name}' describes {table.row_size} bytes per row, "
            f"but the file has {dat.row_size} (the schema may be outdated)")


class GameData:
    """High-level access to the game tables, with foreign-key resolution."""

    def __init__(self, fs, schema: Schema, language: str = "English", game: int | None = None,
                 extension: str = "datc64"):
        self.fs = fs
        self.schema = schema
        self.game = game or fs.game
        self.language = language
        self.extension = extension
        self._cache: dict[str, tuple[Table, list[dict[str, Any]]]] = {}

    def table_path(self, name: str, language: str | None = None) -> str:
        lang = language or self.language
        base = "data/balance" if self.game == 2 else "data"
        if lang and lang.lower() != "english":
            localized = f"{base}/{lang.lower()}/{name.lower()}.{self.extension}"
            if self.fs.exists(localized):
                return localized
        return f"{base}/{name.lower()}.{self.extension}"

    def list_tables(self) -> list[str]:
        base = "data/balance/" if self.game == 2 else "data/"
        suffix = "." + self.extension
        out = []
        for f in self.fs.index.files:
            p = f.path
            if p and p.startswith(base) and p.endswith(suffix) and "/" not in p[len(base):]:
                out.append(p[len(base):-len(suffix)])
        return sorted(out)

    def open(self, name: str) -> tuple[Table, DatFile]:
        table = self.schema.table(name, self.game)
        if table is None:
            raise DatError(f"Table '{name}' is not in the schema")
        path = self.table_path(table.name)
        return table, DatFile.from_path_and_bytes(path, self.fs.read(path))

    def rows(self, name: str) -> list[dict[str, Any]]:
        key = name.lower()
        if key not in self._cache:
            table, dat = self.open(name)
            self._cache[key] = (table, dat.read_rows(table))
        return self._cache[key][1]

    def _label_column(self, table: Table) -> str | None:
        names = [c.name for c in table.columns if c.name]
        for preferred in ("Id", "Name", "Text"):
            if preferred in names:
                return preferred
        for c in table.columns:
            if c.name and c.type == "string" and c.unique:
                return c.name
        return None

    def resolve(self, name: str, enums: bool = False) -> list[dict[str, Any]]:
        """Table rows with foreign keys replaced by the label of the referenced row
        (its Id/Name column).

        With ``enums=True`` enumeration values are also replaced by their name; this is
        optional because the community schema enumerations are often incomplete or out
        of date with respect to the game.
        """
        table = self.schema.table(name, self.game)
        if table is None:
            raise DatError(f"Table '{name}' is not in the schema")
        rows = [dict(r) for r in self.rows(name)]
        for col in table.columns:
            if not col.name or not col.references:
                continue
            if col.type == "enumrow":
                if not enums:
                    continue
                enum = self.schema.enumeration(col.references)
                if enum is None:
                    continue
                def label(v, enum=enum):
                    i = v - enum.indexing if isinstance(v, int) else None
                    if i is not None and 0 <= i < len(enum.enumerators):
                        return enum.enumerators[i] or v
                    return v
            elif col.type in ("foreignrow", "row"):
                target_name = table.name if col.type == "row" else col.references
                try:
                    target_rows = self.rows(target_name)
                    target_table = self.schema.table(target_name, self.game)
                except (DatError, FileNotFoundError):
                    continue
                lab = self._label_column(target_table) if target_table else None
                if lab is None:
                    continue
                def label(v, target_rows=target_rows, lab=lab):
                    if isinstance(v, int) and 0 <= v < len(target_rows):
                        return target_rows[v].get(lab, v)
                    return v
            else:
                continue
            for r in rows:
                v = r.get(col.name)
                if isinstance(v, list):
                    r[col.name] = [label(x) for x in v]
                elif v is not None:
                    r[col.name] = label(v)
        return rows


def rows_to_json(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, ensure_ascii=False, indent=2)


def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                         for k, v in r.items()})
    return buf.getvalue()
