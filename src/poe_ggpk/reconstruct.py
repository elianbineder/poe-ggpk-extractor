"""Reconstruction of a missing index (e.g. the torrent Content.ggpk released before a league).

Measured facts about the format that make the reconstruction possible:

* Every decompressed bundle is the exact concatenation of its files, from byte 0 to
  the end, without gaps; identical files share bytes.
* The SHA-256 of the GGPK FILE record identifies identical bundles without decompressing.

Strategy for every bundle of the new version:

1. Same name and same SHA-256 as in the reference -> the old layout is copied (``same``).
2. Modified bundle -> its content is scanned for the old segments by fingerprint
   (``matched``). What remains between matches are gaps: if exactly one old file is
   missing between two matches, the gap is its new version (``modified``); the rest is
   split by format signatures and left unnamed (``unknown``).
3. Bundle with a new name -> compared against old bundles with the same base name
   (``Tiny_9`` ~ ``Tiny_9_1``); whatever is not found stays ``unknown``.
"""

from __future__ import annotations

import bisect
import gzip
import os
import re
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .bundle import Bundle
from .ddsinfo import known_size
from .ggpk import GGPK
from .hashing import murmur64a_many
from .index import BundleRecord, FileRecord
from .reference import PREFIX_LEN, Reference, Segment, digest, ggpk_bundle_entries

MIN_SEARCH_SIZE = 24        # smaller segments are only located next to their neighbours
MAX_ANCHOR_FREQ = 4         # max. segments sharing a prefix/suffix for it to be used as an anchor
MAX_SIGNATURE_PROBES = 4096  # format-signature positions probed per gap
MAX_TABLE_RUN = 24          # max. missing files in a gap for splitting tables by size
_KEY_MUL = 0x9E3779B97F4A7C15

STATUSES = ("same", "matched", "modified", "unknown")

# File-start signatures used to split gaps.
_SIGNATURES = re.compile(
    rb"DDS \x7c\x00\x00\x00"                      # DDS texture (header size 124)
    rb"|OggS\x00\x02"                              # start of an Ogg stream
    rb"|\xff\xfe[\x09\x0a\x0d\x20-\x7e]\x00[\x09\x0a\x0d\x20-\x7e]\x00"  # UTF-16LE text with BOM
    rb"|(?<!\xff\xfe)v\x00e\x00r\x00s\x00i\x00o\x00n\x00 \x00[0-9]\x00"  # .sm/.tgt/.amd without BOM
    rb'|\{\x00"\x00v\x00e\x00r\x00s\x00i\x00o\x00n\x00"\x00'            # .mat (UTF-16 JSON without BOM)
)


def detect_type(head: bytes) -> str | None:
    """File type from its first bytes."""
    if head.startswith(b"DDS "):
        return "dds"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"\xff\xfe"):
        return "text"
    if head.startswith((b"\xef\xbb\xbf", b"v\x00e\x00", b'{\x00"\x00')):
        return "text"
    return None


_EXT_BY_TYPE = {"dds": "dds", "ogg": "ogg", "text": "txt", "datc64": "datc64", "datcl64": "datcl64"}


def _split_tables(data: bytes, piece: Piece) -> list[Piece]:
    """Split out the .datc64 tables contained in an unnamed piece (new tables)."""
    # Also in pieces with a signature (e.g. a new DDS followed by new tables without one).
    if piece.segment is not None or piece.size < 12:
        return [piece]
    from .tablescan import find_tables

    end = piece.offset + piece.size
    try:
        tables = find_tables(data, piece.offset, end)
    except Exception:
        return [piece]
    if not tables:
        return [piece]
    out: list[Piece] = []
    if piece.kind is not None and tables[0].offset == piece.offset:
        return [piece]  # a file with a signature (DDS, text...) is not a table
    if tables[0].offset > piece.offset:
        out.append(Piece(piece.offset, tables[0].offset - piece.offset, "unknown", None, piece.kind))
    for n, t in enumerate(tables):
        stop = tables[n + 1].offset if n + 1 < len(tables) else end
        out.append(Piece(t.offset, stop - t.offset, "unknown", None, t.extension))
    return out


@dataclass(slots=True)
class Piece:
    offset: int
    size: int
    status: str
    segment: Segment | None = None
    kind: str | None = None


def _signature_positions(data: bytes) -> list[int]:
    return [m.start() for m in _SIGNATURES.finditer(data)]


def _matches(data: bytes, pos: int, s: Segment) -> bool:
    end = pos + s.size
    if end > len(data):
        return False
    if data[pos:pos + len(s.prefix)] != s.prefix:
        return False
    if s.suffix and data[end - len(s.suffix):end] != s.suffix:
        return False
    return digest(memoryview(data)[pos:end]) == s.digest


def _window_keys(block, phase: int):
    """64-bit key of every 16-byte window starting at phase, phase+8, ..."""
    import numpy as np

    usable = (len(block) - phase) // 8 * 8
    words = block[phase:phase + usable].view("<u8")
    with np.errstate(over="ignore"):
        return words[:-1] * np.uint64(_KEY_MUL) ^ words[1:]


def _key16(b: bytes) -> int:
    lo = int.from_bytes(b[:8], "little")
    hi = int.from_bytes(b[8:16], "little")
    return ((lo * _KEY_MUL) & 0xFFFFFFFFFFFFFFFF) ^ hi


def _find_anchors(data: bytes, segments: list[Segment]) -> list[tuple[int, int]]:
    """Find, in a single pass, every exact occurrence of the segments that have a
    distinctive sequence (a rarely repeated 16-byte suffix or prefix). Returns (pos, k)."""
    import numpy as np

    prefix_freq = Counter(s.prefix for s in segments)
    suffix_freq = Counter(s.suffix for s in segments)
    targets: dict[int, list[tuple[int, int]]] = defaultdict(list)  # key -> (k, offset in segment)
    for k, s in enumerate(segments):
        if s.size < MIN_SEARCH_SIZE:
            continue
        if suffix_freq[s.suffix] <= MAX_ANCHOR_FREQ:
            targets[_key16(s.suffix)].append((k, s.size - PREFIX_LEN))
        elif prefix_freq[s.prefix] <= MAX_ANCHOR_FREQ:
            targets[_key16(s.prefix)].append((k, 0))
    if not targets:
        return []
    wanted = np.fromiter(targets.keys(), dtype=np.uint64, count=len(targets))
    wanted.sort()
    arr = np.frombuffer(data, dtype=np.uint8)
    found: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    block = 64 << 20
    for base in range(0, len(data), block):
        chunk = arr[base:base + block + PREFIX_LEN]  # overlap for windows on the border
        for phase in range(8):
            keys = _window_keys(chunk, phase)
            if keys.size == 0:
                continue
            idx = np.searchsorted(wanted, keys)
            idx[idx == len(wanted)] = 0
            for w in np.flatnonzero(wanted[idx] == keys).tolist():
                pos = base + phase + 8 * w
                for k, off in targets[int(keys[w])]:
                    start = pos - off
                    if start >= 0 and (start, k) not in seen and _matches(data, start, segments[k]):
                        seen.add((start, k))
                        found.append((start, k))
    return found


def align(data: bytes, segments: list[Segment], sizer: "Sizer | None" = None) -> list[Piece]:
    """Locate the old segments inside ``data`` and classify the rest.

    1. Anchors: global, vectorized search for the segments with a distinctive sequence.
    2. Non-overlapping anchors are chosen (preferring those that follow the old order).
    3. Each anchor is extended forwards and backwards with its neighbouring segments in
       the old order (recovers small files or files with a common header).
    4. Inside the gaps, positions with a format signature are probed (moved files).
    5. Whatever remains is classified by _assign_gap.
    """
    n = len(segments)
    total = len(data)
    by_prefix: dict[bytes, dict[int, dict[bytes, list[int]]]] = defaultdict(lambda: defaultdict(dict))
    for k, s in enumerate(segments):
        if s.size >= MIN_SEARCH_SIZE:
            by_prefix[s.prefix][s.size].setdefault(s.suffix, []).append(k)
    signatures = _signature_positions(data)

    def probe(pos: int, limit: int, prefer: int) -> int | None:
        """Old segment matching exactly at ``pos`` without going past ``limit``."""
        if 0 <= prefer < n and segments[prefer].size > 0 and pos + segments[prefer].size <= limit \
                and _matches(data, pos, segments[prefer]):
            return prefer
        bucket = by_prefix.get(data[pos:pos + PREFIX_LEN])
        if not bucket:
            return None
        for size, by_suffix in bucket.items():
            end = pos + size
            if end > limit:
                continue
            for k in by_suffix.get(data[end - PREFIX_LEN:end], ()):
                if digest(memoryview(data)[pos:end]) == segments[k].digest:
                    return k
        return None

    # 1-2. Non-overlapping anchors; with duplicated content the one following the order wins.
    anchors = sorted(_find_anchors(data, segments))
    chosen: list[tuple[int, int]] = []
    used: set[int] = set()
    last_end = 0
    for pos, k in anchors:
        if k in used or pos < last_end:
            continue
        chosen.append((pos, k))
        used.add(k)
        last_end = pos + segments[k].size

    # 3. Extend every anchor to its neighbours (also from the start of the bundle).
    matched: list[tuple[int, int]] = []
    bounds = chosen + [(total, n)]
    prev_end, prev_k = 0, -1
    for pos, k in bounds:
        cursor, i = prev_end, prev_k + 1
        while cursor < pos:  # forwards from the previous anchor
            while i < n and segments[i].size == 0:
                i += 1
            hit = probe(cursor, pos, i)
            if hit is None or hit in used:
                break
            matched.append((cursor, hit))
            used.add(hit)
            cursor += segments[hit].size
            i = hit + 1
        back, j = pos, k - 1
        tail: list[tuple[int, int]] = []
        while back > cursor and 0 <= j and j not in used:  # backwards from the next one
            s = segments[j]
            if s.size == 0:
                j -= 1
                continue
            start = back - s.size
            if start < cursor or not _matches(data, start, s):
                break
            tail.append((start, j))
            used.add(j)
            back = start
            j -= 1
        matched.extend(reversed(tail))
        # 4. Format signatures inside the remaining gap: files that moved.
        sig = bisect.bisect_left(signatures, cursor)
        probes = 0
        while sig < len(signatures) and signatures[sig] < back and probes < MAX_SIGNATURE_PROBES:
            p = signatures[sig]
            hit = probe(p, back, -1)
            probes += 1
            if hit is not None and hit not in used:
                matched.append((p, hit))
                used.add(hit)
                end = p + segments[hit].size
                sig = bisect.bisect_left(signatures, end)
            else:
                sig += 1
        if k < n:
            matched.append((pos, k))
            prev_end, prev_k = pos + segments[k].size, k

    matched.sort()
    return _build_pieces(data, segments, matched, used, signatures, sizer)


def _build_pieces(data: bytes, segments: list[Segment], matched: list[tuple[int, int]],
                  used: set[int], signatures: list[int], sizer: "Sizer | None") -> list[Piece]:
    pieces: list[Piece] = []
    for k, s in enumerate(segments):
        if s.size == 0:
            pieces.append(Piece(0, 0, "matched", s))
            used.add(k)

    prev_end, prev_k = 0, -1  # prev_k: highest old index seen (ignores out-of-order matches)
    for pos, k in matched + [(len(data), len(segments))]:
        if pos > prev_end:
            # Candidates: old files not found between the two neighbouring matches.
            cand_idx = [c for c in range(prev_k + 1, k) if c not in used] if prev_k < k else []
            gap = _assign_gap(data, prev_end, pos, [segments[c] for c in cand_idx], signatures, sizer)
            assigned = {id(p.segment) for p in gap if p.segment is not None}
            used.update(c for c in cand_idx if id(segments[c]) in assigned)
            pieces.extend(gap)
        if k < len(segments):
            pieces.append(Piece(pos, segments[k].size, "matched", segments[k]))
            prev_end, prev_k = pos + segments[k].size, max(prev_k, k)
    return pieces


def _assign_gap(data: bytes, start: int, end: int, candidates: list[Segment],
                signatures: list[int], sizer: "Sizer | None" = None) -> list[Piece]:
    """Assign a gap [start, end) to the old files missing in that range.

    Conservative by design: leaving a piece unnamed is better than naming it wrongly.
    """
    # 1. Unchanged candidates stuck to the gap borders (e.g. very small files that cannot
    #    be anchors but sit right before/after a match).
    head: list[Piece] = []
    tail: list[Piece] = []
    cands = list(candidates)
    while cands and start < end:
        c = cands[0]
        if c.size and _matches(data, start, c) and start + c.size <= end:
            head.append(Piece(start, c.size, "matched", c))
            start += c.size
            cands.pop(0)
            continue
        c = cands[-1]
        if c.size and end - c.size >= start and _matches(data, end - c.size, c):
            tail.insert(0, Piece(end - c.size, c.size, "matched", c))
            end -= c.size
            cands.pop()
            continue
        # 2. Modified table at the start of the gap: its exact size comes from the schema, so
        #    adjacent tables can be split (.datc64, .datcl64 and translations change together).
        length = sizer(cands[0], data, start, end) if sizer is not None and len(cands) <= MAX_TABLE_RUN else None
        if length:
            head.append(Piece(start, length, "modified", cands[0], "dat"))
            start += length
            cands.pop(0)
            continue
        break
    if start >= end:
        return head + tail
    return head + _split_gap(data, start, end, cands, signatures) + tail


def _split_gap(data: bytes, start: int, end: int, candidates: list[Segment],
               signatures: list[int]) -> list[Piece]:
    lo = bisect.bisect_right(signatures, start)
    hi = bisect.bisect_left(signatures, end)
    cuts = [start] + signatures[lo:hi] + [end]
    chunks: list[tuple[int, int]] = []
    kinds: list[str | None] = []
    for a, b in zip(cuts, cuts[1:]):
        if b <= a:
            continue
        kd = detect_type(data[a:a + 4])
        # DDS and Ogg declare their size: whatever follows is another file (e.g. new tables).
        size = known_size(kd, data, a, b)
        if size and a + size < b:
            chunks += [(a, a + size), (a + size, b)]
            kinds += [kd, detect_type(data[a + size:a + size + 4])]
        else:
            chunks.append((a, b))
            kinds.append(kd)
    cand_kinds = [detect_type(c.prefix) for c in candidates]

    def unknown() -> list[Piece]:
        return [Piece(a, b - a, "unknown", None, kd) for (a, b), kd in zip(chunks, kinds)]

    if len(candidates) == 1:
        # Most common case: a single old file changed its content (and maybe its size).
        cand, want = candidates[0], cand_kinds[0]
        if len(chunks) == 1 and want in (None, kinds[0]):
            return [Piece(start, end - start, "modified", cand, kinds[0])]
        hits = [n for n, kd in enumerate(kinds) if want is not None and kd == want]
        if len(hits) == 1:
            out = unknown()
            a, b = chunks[hits[0]]
            out[hits[0]] = Piece(a, b - a, "modified", cand, kinds[hits[0]])
            return out
        return unknown()

    # Several files missing in the same range: assigning them by order proved unreliable in
    # simulations (most assignments were wrong), so they stay unnamed.
    return unknown()


class Sizer:
    """Compute the size of a new version of a .datc64/.datcl64 table using the schema.

    Only used for tables whose schema exactly reproduces the size of their previous
    version (self-validation against the reference); this rules out the few tables whose
    columns are described wrongly in the community schema.
    """

    def __init__(self, schema, game: int, reference: Reference | None = None):
        self.schema = schema
        self.game = game
        self.reference = reference
        self._trusted: dict[str, bool] = {}

    def _table_for(self, path: str):
        return self.schema.table(path.rsplit("/", 1)[-1].rsplit(".", 1)[0], self.game)

    def trusted(self, path: str) -> bool:
        if path not in self._trusted:
            ok = False
            ref = self.reference
            if ref is not None and ref.trusted_tables is not None:
                ok = path in ref.trusted_tables
            elif ref is not None and ref.fs is not None:
                from .dat import DatFile

                try:
                    old = ref.fs.read(path)
                    table = self._table_for(path)
                    ok = table is not None and DatFile.from_path_and_bytes(path, old).content_end(table) == len(old)
                except Exception:
                    ok = False
            self._trusted[path] = ok
        return self._trusted[path]

    def __call__(self, seg: Segment, data: bytes, start: int, end: int) -> int | None:
        from .dat import DatError, DatFile

        path = seg.paths[0].lower()
        if not path.endswith((".datc64", ".datcl64")) or not self.trusted(seg.paths[0]):
            return None
        table = self._table_for(path)
        if table is None:
            return None
        try:
            dat = DatFile(data[start:end], wide_strings=path.endswith(".datcl64"))
            length = dat.content_end(table)
        except (DatError, struct.error, UnicodeDecodeError, ValueError):
            return None
        return length if length and length <= end - start else None


# -- reconstructed index ------------------------------------------------------------------------
HEADER = "#poe-ggpk-reconstructed-index v1"


class ReconstructedIndex:
    """Index equivalent to BundleIndex, built without _.index.bin."""

    hash_algorithm = "murmur"
    unresolved = 0
    directories: list = []
    bundle = None

    def __init__(self, bundles: list[BundleRecord], files: list[FileRecord],
                 status: dict[str, str], meta: dict[str, str] | None = None):
        self.bundles = bundles
        self.files = files
        self.status = status
        self.meta = meta or {}
        hashes = murmur64a_many([f.path.lower().encode("utf-8") for f in files])
        for f, h in zip(files, hashes):
            f.path_hash = h
        self.by_hash = {f.path_hash: f for f in files}
        self._by_path = {f.path.lower(): f for f in files}

    def get(self, path: str) -> FileRecord | None:
        return self._by_path.get(path.replace("\\", "/").strip("/").lower())

    def __iter__(self) -> Iterator[FileRecord]:
        return iter(self.files)

    def __len__(self) -> int:
        return len(self.files)

    def status_counts(self) -> Counter:
        return Counter(self.status.values())

    def save(self, path: str | os.PathLike[str]) -> None:
        with gzip.open(path, "wt", encoding="utf-8", newline="\n") as fh:
            fh.write(HEADER + "\n")
            for k, v in self.meta.items():
                fh.write(f"#{k}\t{v}\n")
            for b in self.bundles:
                fh.write(f"B\t{b.index}\t{b.name}\t{b.uncompressed_size}\n")
            for f in self.files:
                fh.write(f"F\t{f.bundle_index}\t{f.offset}\t{f.size}\t{self.status[f.path]}\t{f.path}\n")

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "ReconstructedIndex":
        bundles: list[BundleRecord] = []
        files: list[FileRecord] = []
        status: dict[str, str] = {}
        meta: dict[str, str] = {}
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            if fh.readline().rstrip("\n") != HEADER:
                raise ValueError(f"{path} is not a reconstructed index")
            for line in fh:
                line = line.rstrip("\n")
                if line.startswith("#"):
                    k, _, v = line[1:].partition("\t")
                    meta[k] = v
                elif line.startswith("B\t"):
                    _, i, name, size = line.split("\t")
                    bundles.append(BundleRecord(int(i), name, int(size)))
                elif line.startswith("F\t"):
                    _, b, off, size, st, p = line.split("\t", 5)
                    files.append(FileRecord(0, int(b), int(off), int(size), p))
                    status[p] = st
        return cls(bundles, files, status, meta)


# -- orchestration -------------------------------------------------------------------------------
_BASE_RE = re.compile(r"(?:[_.][0-9A-Za-z]{1,3})+$")


def _base_name(name: str) -> str:
    return _BASE_RE.sub("", name).lower()


@dataclass
class ReconstructReport:
    bundles: Counter = field(default_factory=Counter)
    files: Counter = field(default_factory=Counter)
    bytes: Counter = field(default_factory=Counter)
    missing_old_files: int = 0


def reconstruct(new_location: str | os.PathLike[str], reference: Reference,
                progress: Callable[[int, int, str], None] | None = None,
                schema=None) -> tuple[ReconstructedIndex, ReconstructReport]:
    """Reconstruct the index of ``new_location`` using ``reference`` (previous version).

    With ``schema`` (dat-schema) modified tables are split by their exact size."""
    loc = Path(new_location)
    ggpk = GGPK(loc / "Content.ggpk" if loc.is_dir() else loc)
    entries = ggpk_bundle_entries(ggpk)
    report = ReconstructReport()
    ref_index = reference.index
    sizer = Sizer(schema, reference.game, reference) if schema is not None else None

    old_by_base: dict[str, list[int]] = defaultdict(list)
    for b in ref_index.bundles:
        old_by_base[_base_name(b.name)].append(b.index)
    old_names_present = {n.lower() for n in entries}

    bundles: list[BundleRecord] = []
    files: list[FileRecord] = []
    status: dict[str, str] = {}
    placed_paths: set[str] = set()

    def emit(bi: int, piece: Piece, bundle_name: str) -> None:
        if piece.segment is not None:
            paths = piece.segment.paths
        else:
            ext = _EXT_BY_TYPE.get(piece.kind or "", "bin")
            paths = [f"_unknown/{bundle_name}/{piece.offset:010d}.{ext}"]
        for p in paths:
            if p in placed_paths:
                continue  # same file found in two bundles: the first one is used
            placed_paths.add(p)
            files.append(FileRecord(0, bi, piece.offset, piece.size, p))
            status[p] = piece.status
            report.files[piece.status] += 1
        report.bytes[piece.status] += piece.size

    names = sorted(entries)
    for n, name in enumerate(names, 1):
        entry = entries[name]
        bi = len(bundles)
        old_i = reference.by_name.get(name.lower())
        if progress:
            progress(n, len(names), name)

        if old_i is not None and reference.bundle_sha.get(name.lower()) == entry.sha256:
            old = ref_index.bundles[old_i]
            bundles.append(BundleRecord(bi, name, old.uncompressed_size))
            for seg in reference._segments.get(old_i, []):
                emit(bi, Piece(seg.offset, seg.size, "same", seg), name)
            report.bundles["same"] += 1
            continue

        bundle = Bundle(ggpk.open_entry(entry))
        data = bundle.read()
        bundles.append(BundleRecord(bi, name, len(data)))
        if old_i is not None:
            segs = reference.segments(old_i)
            report.bundles["changed"] += 1
        else:
            # New name: compare against old bundles with the same base name that no longer exist.
            segs = []
            for cand in old_by_base.get(_base_name(name), []):
                if ref_index.bundles[cand].name.lower() not in old_names_present:
                    segs.extend(reference.segments(cand))
            report.bundles["new"] += 1
        for piece in align(data, segs, sizer):
            for part in _split_tables(data, piece):
                emit(bi, part, name)

    old_paths = {f.path for f in ref_index.files if f.path}
    report.missing_old_files = len(old_paths - placed_paths)
    meta = {"source": str(new_location), "reference": reference.label}
    ggpk.close()
    return ReconstructedIndex(bundles, files, status, meta), report


def compare_with_truth(rec: ReconstructedIndex, truth) -> Counter:
    """Compare a reconstructed index with the real one (for validation)."""
    out: Counter = Counter()
    truth_bundles = {b.index: b.name.lower() for b in truth.bundles}
    rec_bundles = {b.index: b.name.lower() for b in rec.bundles}
    for f in rec.files:
        st = rec.status[f.path]
        if f.path.startswith("_unknown/"):
            out[f"{st}:unnamed"] += 1
            continue
        t = truth.get(f.path)
        if t is None:
            out[f"{st}:nonexistent-path"] += 1
        elif (truth_bundles[t.bundle_index] == rec_bundles[f.bundle_index]
              and t.offset == f.offset and t.size == f.size):
            out[f"{st}:correct"] += 1
        else:
            out[f"{st}:wrong"] += 1
    rec_paths = {f.path for f in rec.files}
    out["not-found"] = sum(1 for f in truth.files if f.path and f.path not in rec_paths)
    return out
