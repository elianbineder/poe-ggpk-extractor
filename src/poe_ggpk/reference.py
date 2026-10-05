"""Reference (previous) version used to reconstruct a missing index.

For every bundle of the old version a reference provides:

* the SHA-256 of the compressed bundle (identical bundles are detected without decompressing);
* the list of segments (offset, size, paths) according to its index;
* the fingerprint of each segment: size + blake2b-64 of the content + first/last 16 bytes.

It can come from an old GGPK that is still installed (fingerprints are computed on
demand, only for the bundles that changed) or from a snapshot saved on disk with
``poe-ggpk snapshot`` (useful when the old GGPK has already been updated).
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .bundle import BytesSource
from .filesystem import PoEFileSystem
from .ggpk import GGPK, FileEntry
from .index import BundleIndex

PREFIX_LEN = 16
_FP = struct.Struct("<III8s16s16s")  # bundle, offset, size, digest, prefix, suffix (52 bytes)
SNAPSHOT_VERSION = 2


def digest(data: bytes | memoryview) -> bytes:
    return hashlib.blake2b(data, digest_size=8).digest()


@dataclass(slots=True)
class Segment:
    """Byte range of an old bundle (one or several identical files)."""

    offset: int
    size: int
    paths: list[str]
    digest: bytes = b""
    prefix: bytes = b""
    suffix: bytes = b""  # last bytes: rules out false candidates without hashing


def ggpk_bundle_entries(ggpk: GGPK) -> dict[str, FileEntry]:
    """Bundles present in a GGPK, found without the index: name -> FILE record."""
    bundles_dir = ggpk._directory("Bundles2")
    if bundles_dir is None:
        raise FileNotFoundError("The GGPK has no Bundles2 folder")
    out: dict[str, FileEntry] = {}
    prefix = len(bundles_dir.path)
    for e in ggpk.walk(bundles_dir):
        if isinstance(e, FileEntry) and e.name.endswith(".bundle.bin"):
            out[e.path[prefix:-len(".bundle.bin")]] = e
    return out


def _segments_from_index(index: BundleIndex) -> dict[int, list[Segment]]:
    """Group the index files into unique segments per bundle, sorted by offset."""
    spans: dict[int, dict[tuple[int, int], list[str]]] = defaultdict(dict)
    for f in index.files:
        if f.path is None:
            continue
        spans[f.bundle_index].setdefault((f.offset, f.size), []).append(f.path)
    return {
        b: [Segment(off, size, sorted(paths)) for (off, size), paths in sorted(d.items())]
        for b, d in spans.items()
    }


def fingerprint_bundle(data: bytes, segments: list[Segment]) -> None:
    for s in segments:
        chunk = memoryview(data)[s.offset:s.offset + s.size]
        s.digest = digest(chunk)
        s.prefix = bytes(chunk[:PREFIX_LEN])
        s.suffix = bytes(chunk[-PREFIX_LEN:]) if s.size else b""


class Reference:
    def __init__(self, index: BundleIndex, bundle_sha: dict[str, bytes],
                 fs: PoEFileSystem | None = None, fingerprints: dict[int, list[Segment]] | None = None,
                 label: str = "", game: int | None = None):
        self.index = index
        self.fs = fs
        self.label = label
        self.game = game or (fs.game if fs is not None else 1)
        # Tables whose size the schema reproduces (snapshots only; computed when a GGPK is available).
        self.trusted_tables: set[str] | None = None
        self._segments = _segments_from_index(index)
        self._fingerprinted: set[int] = set()
        if fingerprints:
            for b, segs in fingerprints.items():
                self._segments[b] = segs
                self._fingerprinted.add(b)
        self.by_name: dict[str, int] = {b.name.lower(): b.index for b in index.bundles}
        self.bundle_sha = {k.lower(): v for k, v in bundle_sha.items()}

    # -- construction ------------------------------------------------------------
    @classmethod
    def from_location(cls, location: str | os.PathLike[str]) -> "Reference":
        fs = PoEFileSystem(location)
        sha = {name: e.sha256 for name, e in ggpk_bundle_entries(fs.ggpk).items()} if fs.ggpk else {}
        return cls(fs.index, sha, fs=fs, label=str(location))

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Reference":
        p = Path(path)
        if (p / "snapshot.json").is_file():
            return cls.from_snapshot(p)
        return cls.from_location(p)

    @classmethod
    def from_snapshot(cls, directory: str | os.PathLike[str]) -> "Reference":
        d = Path(directory)
        meta = json.loads((d / "snapshot.json").read_text(encoding="utf-8"))
        index = BundleIndex(BytesSource((d / "_.index.bin").read_bytes()))
        sha = {k: bytes.fromhex(v) for k, v in meta["bundles"].items()}
        fps: dict[int, list[Segment]] | None = None
        fp_file = d / "fingerprints.bin"
        if fp_file.is_file():
            segs = _segments_from_index(index)
            lookup = {(b, s.offset, s.size): s for b, lst in segs.items() for s in lst}
            raw = fp_file.read_bytes()
            for b, off, size, dg, pf, sf in _FP.iter_unpack(raw):
                s = lookup.get((b, off, size))
                if s is not None:
                    s.digest = dg
                    s.prefix = pf[:min(size, PREFIX_LEN)]
                    s.suffix = sf[:min(size, PREFIX_LEN)]
            fps = segs
        ref = cls(index, sha, fingerprints=fps, label=meta.get("label", str(d)), game=meta.get("game"))
        if "trusted_tables" in meta:
            ref.trusted_tables = set(meta["trusted_tables"])
        return ref

    # -- queries ------------------------------------------------------------------
    def segments(self, bundle_index: int) -> list[Segment]:
        """Fingerprinted segments; computed from the old bundle when needed."""
        segs = self._segments.get(bundle_index, [])
        if bundle_index not in self._fingerprinted:
            if self.fs is None:
                raise RuntimeError("The snapshot has no fingerprints and no old GGPK is available")
            fingerprint_bundle(self.fs._bundle(bundle_index).read(), segs)
            self._fingerprinted.add(bundle_index)
        return segs

    def segment_for(self, path: str) -> Segment | None:
        """Fingerprinted segment that ``path`` occupied in the reference version."""
        rec = self.index.get(path)
        if rec is None:
            return None
        for s in self.segments(rec.bundle_index):
            if s.offset == rec.offset and s.size == rec.size:
                return s
        return None

    def read(self, path: str) -> bytes | None:
        """Old content of ``path`` (only when the reference is an installation)."""
        if self.fs is None or self.index.get(path) is None:
            return None
        return self.fs.read(path)

    # -- snapshot ------------------------------------------------------------------
    def save_snapshot(self, directory: str | os.PathLike[str], with_fingerprints: bool = False,
                      jobs: int | None = None, progress: Callable[[int, int], None] | None = None,
                      schema=None) -> Path:
        if self.fs is None:
            raise RuntimeError("A snapshot can only be created from an installation")
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        (d / "_.index.bin").write_bytes(self.fs.read_bundle_file("_.index.bin"))
        meta = {
            "format": SNAPSHOT_VERSION,
            "label": self.label,
            "game": self.fs.game,
            "files": len(self.index.files),
            "bundles": {k: v.hex() for k, v in self.bundle_sha.items()},
        }
        if with_fingerprints:
            write_fingerprints(self.fs, self._segments, d / "fingerprints.bin", jobs, progress)
            if schema is not None:
                meta["trusted_tables"] = sorted(trusted_tables(self.fs, schema, self.game))
        (d / "snapshot.json").write_text(json.dumps(meta), encoding="utf-8")
        return d


def trusted_tables(fs: PoEFileSystem, schema, game: int) -> set[str]:
    """Tables whose real size matches the one computed from the schema (see DatFile.content_end)."""
    from .dat import DatFile

    out: set[str] = set()
    for f in fs.index.files:
        p = f.path
        if not p or not p.startswith("data/") or not p.endswith((".datc64", ".datcl64")):
            continue
        table = schema.table(p.rsplit("/", 1)[-1].rsplit(".", 1)[0], game)
        if table is None:
            continue
        try:
            raw = fs.read(p)
            if DatFile.from_path_and_bytes(p, raw).content_end(table) == len(raw):
                out.add(p)
        except Exception:
            continue
    return out


# -- parallel fingerprinting (full snapshot) ------------------------------------------------
def _fp_worker(args) -> bytes:
    location, items = args
    fs = PoEFileSystem(location, bundle_cache=1)
    out = bytearray()
    try:
        for b, spans in items:
            data = fs._bundle(b).read()
            for off, size in spans:
                chunk = memoryview(data)[off:off + size]
                out += _FP.pack(b, off, size, digest(chunk),
                                bytes(chunk[:PREFIX_LEN]).ljust(PREFIX_LEN, b"\0"),
                                bytes(chunk[-PREFIX_LEN:] if size else b"").ljust(PREFIX_LEN, b"\0"))
    finally:
        fs.close()
    return bytes(out)


def write_fingerprints(fs: PoEFileSystem, segments: dict[int, list[Segment]], target: Path,
                       jobs: int | None, progress: Callable[[int, int], None] | None) -> None:
    items = sorted(((b, [(s.offset, s.size) for s in segs]) for b, segs in segments.items()),
                   key=lambda x: x[0])
    batches = [items[i:i + 200] for i in range(0, len(items), 200)]
    loc = str(fs.location)
    done = 0
    with open(target, "wb") as fh, ProcessPoolExecutor(max_workers=jobs) as pool:
        for chunk in pool.map(_fp_worker, [(loc, b) for b in batches]):
            fh.write(chunk)
            done += 1
            if progress:
                progress(done, len(batches))
