"""Detection and reading of .datc64 tables without a name or schema.

Meant for the new content of a GGPK without an index: the new tables of a league end
up as ``unknown`` pieces and are not in the community schema yet.

Detection (facts measured on the 19,160 tables of PoE1):

* Every table has exactly one ``0xBB×8`` marker, at the start of its variable section,
  at ``start + 4 + rows × width``; ``rows`` is the uint32 at the start.
* The ``.datc64`` and ``.datcl64`` of a table, when adjacent, share row count and
  row width.
* The variable section ends at the last referenced item; if it is a string (the usual
  case), its end marks the start of the next table.

Column inference: rows are not aligned (there are 1-byte columns), so every offset is
tried, looking in all sampled rows for values shaped like a foreign key (index + 0,
null 0xFEFE…), a pointer to a valid string of the variable section, or an array
(count + offset). The remaining bytes are grouped into 32-bit integers (and single
bytes at the end).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from .dat import MAGIC, NULL64

MAX_ROW_SIZE = 8192
MAX_ROWS = 2_000_000
SAMPLE_ROWS = 400
QUALITY_TOP = 8
_ALLOWED_CONTROL = {"\n", "\r", "\t"}


@dataclass
class InferredColumn:
    offset: int
    size: int
    kind: str  # "string", "key", "array", "i32", "u8"
    elem: str | None = None  # arrays: "key" (16 B), "string" (8 B) or "i32" (4 B)

    @property
    def name(self) -> str:
        return f"{self.kind}_{self.offset}"


@dataclass
class TableGuess:
    offset: int        # start, relative to the analysed data
    rows: int
    row_size: int
    marker: int        # position of the 0xBB×8 marker
    end: int           # end: start of the next table or end of the analysed data
    wide: bool         # UTF-32 strings (.datcl64)
    confidence: str    # "high": start deduced with certainty; "medium": chosen by structural quality
    columns: list[InferredColumn] = field(default_factory=list)
    strings_end: int | None = None  # end of the last referenced string (lower bound of the end)

    @property
    def extension(self) -> str:
        return "datcl64" if self.wide else "datc64"


# -- string reading -----------------------------------------------------------------------
def read_string(var: bytes, off: int, char: int) -> str | None:
    """Printable string starting at ``off`` of the variable section, or None."""
    if off < len(MAGIC) or off >= len(var) or off % 2:
        return None
    end = off
    while True:
        end = var.find(b"\0\0\0\0", end)
        if end < 0 or end - off > 40000:
            return None
        if (end - off) % char == 0:
            break
        end += 1
    try:
        text = var[off:end].decode("utf-32-le" if char == 4 else "utf-16-le")
    except UnicodeDecodeError:
        return None
    if any(not ch.isprintable() and ch not in _ALLOWED_CONTROL for ch in text):
        return None
    return text


def _string_end(var: bytes, off: int, char: int) -> int:
    s = read_string(var, off, char)
    return off if s is None else off + len(s.encode("utf-32-le" if char == 4 else "utf-16-le")) + 4


# -- column inference ----------------------------------------------------------------------
class _Rows:
    def __init__(self, fixed: bytes, rows: int, row_size: int, sample: int):
        self.fixed = fixed
        self.row_size = row_size
        step = max(1, rows // sample)
        self.idx = list(range(0, rows, step))[:sample]

    def u64(self, r: int, c: int) -> int:
        return struct.unpack_from("<Q", self.fixed, r * self.row_size + c)[0]


def _is_key(rows: _Rows, c: int) -> bool:
    nulls = 0
    values = set()
    for r in rows.idx:
        a, b = rows.u64(r, c), rows.u64(r, c + 8)
        if a == NULL64 and b == NULL64:
            nulls += 1
        elif a < MAX_ROWS and b == 0:
            values.add(a)
        else:
            return False
    return nulls > 0 or len(values) > 1


def _is_string(rows: _Rows, c: int, var: bytes, char: int) -> bool:
    starts = 0
    for r in rows.idx:
        v = rows.u64(r, c)
        if read_string(var, v, char) is None:
            return False
        starts += v == len(MAGIC) or var[v - 4:v] == b"\0\0\0\0"
    # Strings start after the marker or after another string's terminator; otherwise it is chance.
    return starts >= 0.8 * len(rows.idx)


def _is_array(rows: _Rows, c: int, var: bytes) -> bool:
    nonempty = 0
    for r in rows.idx:
        count, off = rows.u64(r, c), rows.u64(r, c + 8)
        if count == 0:
            continue
        if count > 100_000 or not (len(MAGIC) <= off < len(var)):
            return False
        nonempty += 1
    return nonempty > 0


def _array_elem(rows: _Rows, c: int, var: bytes, char: int) -> str:
    """Element type of an array, trying from most to least specific."""
    arrays = []
    for r in rows.idx:
        count, off = rows.u64(r, c), rows.u64(r, c + 8)
        if count:
            arrays.append((count, off))

    def fits(size: int) -> bool:
        return all(off + count * size <= len(var) for count, off in arrays)

    if fits(16) and all(
            (a == NULL64 and b == NULL64) or (a < MAX_ROWS and b == 0)
            for count, off in arrays for a, b in (struct.unpack_from("<QQ", var, off + 16 * i)
                                                  for i in range(min(count, 32)))):
        return "key"
    if fits(8) and all(read_string(var, struct.unpack_from("<Q", var, off + 8 * i)[0], char) is not None
                       for count, off in arrays for i in range(min(count, 32))):
        return "string"
    return "i32"


def infer_columns(fixed: bytes, rows: int, row_size: int, var: bytes, wide: bool,
                  sample: int = SAMPLE_ROWS) -> list[InferredColumn]:
    if rows == 0 or row_size == 0:
        return []
    char = 4 if wide else 2
    rs = _Rows(fixed, rows, row_size, sample)
    cols: list[InferredColumn] = []
    pending: list[int] = []  # uninterpreted bytes

    def flush() -> None:
        # Unrecognised bytes -> 32-bit integers from the start of the run, the rest as bytes.
        i = 0
        while i < len(pending):
            run = 1
            while i + run < len(pending) and pending[i + run] == pending[i] + run:
                run += 1
            start = pending[i]
            for k in range(0, run - run % 4, 4):
                cols.append(InferredColumn(start + k, 4, "i32"))
            for k in range(run - run % 4, run):
                cols.append(InferredColumn(start + k, 1, "u8"))
            i += run
        pending.clear()

    c = 0
    while c < row_size:
        kind = size = None
        if c + 16 <= row_size and _is_key(rs, c):
            kind, size = "key", 16
        elif c + 8 <= row_size and _is_string(rs, c, var, char):
            kind, size = "string", 8
        elif c + 16 <= row_size and _is_array(rs, c, var):
            kind, size = "array", 16
        if kind is None:
            pending.append(c)
            c += 1
            continue
        flush()
        cols.append(InferredColumn(c, size, kind, _array_elem(rs, c, var, char) if kind == "array" else None))
        c += size
    flush()
    return cols


def read_inferred(fixed: bytes, rows: int, row_size: int, var: bytes, wide: bool,
                  columns: list[InferredColumn]) -> list[dict[str, Any]]:
    """Rows as dicts using inferred columns."""
    char = 4 if wide else 2
    out = []
    for r in range(rows):
        base = r * row_size
        row: dict[str, Any] = {"_index": r}
        for col in columns:
            o = base + col.offset
            if col.kind == "string":
                row[col.name] = read_string(var, struct.unpack_from("<Q", fixed, o)[0], char)
            elif col.kind == "key":
                v = struct.unpack_from("<Q", fixed, o)[0]
                row[col.name] = None if v == NULL64 else v
            elif col.kind == "array":
                count, off = struct.unpack_from("<QQ", fixed, o)
                row[col.name] = _read_array(var, count, off, col.elem, char)
            elif col.kind == "i32":
                row[col.name] = struct.unpack_from("<i", fixed, o)[0]
            else:
                row[col.name] = fixed[o]
        out.append(row)
    return out


def _read_array(var: bytes, count: int, off: int, elem: str | None, char: int) -> list:
    if count == 0:
        return []
    size = {"key": 16, "string": 8}.get(elem or "", 4)
    if off + count * size > len(var):
        return [{"count": count, "offset": off}]
    out: list = []
    for i in range(count):
        p = off + i * size
        if elem == "key":
            v = struct.unpack_from("<Q", var, p)[0]
            out.append(None if v == NULL64 else v)
        elif elem == "string":
            out.append(read_string(var, struct.unpack_from("<Q", var, p)[0], char))
        else:
            out.append(struct.unpack_from("<i", var, p)[0])
    return out


# -- table detection inside a byte range ----------------------------------------------------
def _markers(data: bytes, start: int, end: int) -> list[int]:
    out = []
    pos = data.find(MAGIC, start, end)
    while pos != -1:
        out.append(pos)
        pos = data.find(MAGIC, pos + len(MAGIC), end)
    return out


def _detect_wide(data: bytes, t: int, rows: int, width: int, marker: int, var_end: int) -> bool:
    """Decide whether strings are UTF-32 (.datcl64) by trying both encodings."""
    var = data[marker:var_end]
    fixed = data[t + 4:marker]
    if rows == 0:
        return False
    rs = _Rows(fixed, rows, width, 50)
    score = {2: 0, 4: 0}
    for char in (2, 4):
        for c in range(0, max(0, width - 7)):
            ok = all(read_string(var, rs.u64(r, c), char) not in (None, "") for r in rs.idx[:20])
            score[char] += ok
    return score[4] > score[2]


def _strings_end(data: bytes, t: int, rows: int, width: int, marker: int, var_end: int,
                 wide: bool, columns: list[InferredColumn]) -> int | None:
    """End of the last referenced string: lower bound of the end of the table."""
    var = data[marker:var_end]
    fixed = data[t + 4:marker]
    char = 4 if wide else 2
    end = None
    for col in columns:
        if col.kind != "string":
            continue
        for r in range(rows):
            v = struct.unpack_from("<Q", fixed, r * width + col.offset)[0]
            e = _string_end(var, v, char)
            end = e if end is None else max(end, e)
    return None if end is None else marker + end


def _candidate_starts(data: bytes, lo: int, marker: int) -> list[tuple[int, int, int]]:
    """Every structurally valid start t in [lo, marker-4]: (t, rows, width)."""
    import numpy as np

    if marker - 4 < lo:
        return []
    n = marker - 4 - lo + 1
    buf = np.frombuffer(data, dtype=np.uint8, count=n + 3, offset=lo)
    rows = (buf[:n].astype(np.uint32) | (buf[1:n + 1].astype(np.uint32) << 8)
            | (buf[2:n + 2].astype(np.uint32) << 16) | (buf[3:n + 3].astype(np.uint32) << 24)).astype(np.int64)
    pos = np.arange(lo, lo + n, dtype=np.int64)
    body = marker - pos - 4
    ok_empty = (rows == 0) & (body == 0)
    safe = np.where(rows > 0, rows, 1)
    width = body // safe
    ok = (rows > 0) & (rows <= MAX_ROWS) & (body % safe == 0) & (width >= 1) & (width <= MAX_ROW_SIZE)
    sel = np.flatnonzero(ok | ok_empty)
    return [(int(pos[i]), int(rows[i]), int(width[i]) if rows[i] else 0) for i in sel]


def _quick_score(data: bytes, t: int, rows: int, width: int) -> float:
    """Cheap filter: fraction of constant byte columns in a sample of rows."""
    import numpy as np

    if rows < 3:
        return 0.0
    k = min(rows, 24)
    step = max(1, rows // k)
    arr = np.frombuffer(data, dtype=np.uint8, count=rows * width, offset=t + 4).reshape(rows, width)[::step][:k]
    return float((arr == arr[0]).all(axis=0).mean())


def _quality(data: bytes, t: int, rows: int, width: int, marker: int, var_end: int) -> float:
    """How "table-like" it looks: recognisable columns and constant bytes per column."""
    if rows == 0:
        return 0.5
    fixed = data[t + 4:marker]
    var = data[marker:var_end]
    sample = min(rows, 40)
    step = max(1, rows // sample)
    idx = list(range(0, rows, step))[:sample]
    q = 0.0
    if rows >= 3:
        constant = 0
        for c in range(width):
            vals = [fixed[r * width + c] for r in idx]
            if vals.count(max(set(vals), key=vals.count)) >= 0.9 * len(vals):
                constant += 1
        q += 2.0 * constant / width
    wide_score = 0
    for wide in (False, True):
        cols = infer_columns(fixed, rows, width, var, wide, sample=sample)
        typed = sum(1 for c in cols if c.kind in ("string", "array", "key"))
        strings = sum(1 for c in cols if c.kind == "string")
        wide_score = max(wide_score, typed + strings)
    return q + min(wide_score, 6) * 0.5


def find_tables(data: bytes, start: int = 0, end: int | None = None,
                max_candidates: int = 400) -> list[TableGuess]:
    """Find consecutive .datc64/.datcl64 tables in ``data[start:end]``.

    For every marker, all structurally valid starts between the minimum end of the
    previous table (its last referenced string) and the marker are generated, choosing:
    1. the one continuing exactly where the previous table's strings end, if it looks like a table;
    2. the .datcl64/.datc64 sibling (same rows and width as the previous one);
    3. the one with the best structural quality.
    """
    end = len(data) if end is None else end
    markers = _markers(data, start, end)
    tables: list[TableGuess] = []
    lower = start
    for n, m in enumerate(markers):
        var_end = markers[n + 1] if n + 1 < len(markers) else end
        prev = tables[-1] if tables else None
        cands = _candidate_starts(data, lower, m)
        if not cands:
            continue
        chosen = None
        confidence = "medium"
        if prev is None and cands[0][0] == lower:
            chosen, confidence = cands[0], "high"  # the piece starts at a file boundary
        if chosen is None and prev is not None and prev.strings_end is not None:
            exact = [c for c in cands if c[0] == prev.strings_end]
            if exact and (exact[0][1] > 1 or _quality(data, *exact[0], m, var_end) >= 1.0):
                chosen, confidence = exact[0], "high"
        if chosen is None and prev is not None and prev.rows:
            sib = [c for c in cands if c[1:] == (prev.rows, prev.row_size)]
            if sib:
                chosen, confidence = sib[-1], "high"
        if chosen is None:
            pool = [c for c in cands if c[1] > 1][:max_candidates] or cands[:max_candidates]
            # Cheap filter first; full analysis only for the best candidates.
            pool = sorted(pool, key=lambda c: -_quick_score(data, *c))[:QUALITY_TOP]
            chosen = max(pool, key=lambda c: (_quality(data, *c, m, var_end), -c[0]))
        t, rows, width = chosen
        if prev is not None:
            prev.end = t
        wide = _detect_wide(data, t, rows, width, m, var_end)
        cols = infer_columns(data[t + 4:m], rows, width, data[m:var_end], wide)
        tg = TableGuess(t, rows, width, m, var_end, wide, confidence, cols)
        tg.strings_end = _strings_end(data, t, rows, width, m, var_end, wide, cols)
        tables.append(tg)
        lower = tg.strings_end if tg.strings_end is not None else m + len(MAGIC)
    return tables


def read_unknown_table(raw: bytes, wide: bool | None = None) -> tuple[TableGuess, list[dict[str, Any]]]:
    """Read a table without schema: detect rows/width/encoding and infer columns."""
    tables = find_tables(raw)
    if not tables or tables[0].offset != 0:
        raise ValueError("the data does not start with a recognisable .datc64 table")
    tg = tables[0]
    if wide is not None and wide != tg.wide:
        tg.wide = wide
        tg.columns = infer_columns(raw[4:tg.marker], tg.rows, tg.row_size, raw[tg.marker:], wide)
    rows = read_inferred(raw[4:tg.marker], tg.rows, tg.row_size, raw[tg.marker:], tg.wide, tg.columns)
    return tg, rows
