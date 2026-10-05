"""Virtual file system of the game.

Unifies both kinds of installation:

* Standalone client: everything lives inside ``Content.ggpk`` (including the
  ``Bundles2`` folder with the bundles and the index).
* Steam / Epic: there is no GGPK; the ``Bundles2`` folder sits loose on disk.

Since patch 3.11.2 almost all game data lives inside the bundles; the GGPK only
keeps a few loose files (shaders, FMOD, etc.).
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .bundle import Bundle, DiskFile, RandomAccessSource
from .ggpk import GGPK, FileEntry
from .index import BundleIndex, FileRecord

INDEX_PATH = "Bundles2/_.index.bin"


@dataclass(slots=True)
class VirtualFile:
    path: str
    size: int
    source: str  # "bundle" or "ggpk"
    bundle: str | None = None


class PoEFileSystem:
    def __init__(self, location: str | os.PathLike[str], bundle_cache: int = 8,
                 index_file: str | os.PathLike[str] | None = None):
        """``index_file``: reconstructed index (``poe-ggpk reconstruct``) to use instead of
        Bundles2/_.index.bin, for GGPKs that do not ship an index yet."""
        loc = Path(location)
        self.ggpk: GGPK | None = None
        self.bundles_dir: Path | None = None
        if loc.is_dir():
            if (loc / "Content.ggpk").is_file():
                self.ggpk = GGPK(loc / "Content.ggpk")
            elif (loc / INDEX_PATH).is_file():
                self.bundles_dir = loc / "Bundles2"
            elif (loc / "_.index.bin").is_file():
                self.bundles_dir = loc
            else:
                raise FileNotFoundError(f"Neither Content.ggpk nor Bundles2/_.index.bin found in {loc}")
        elif loc.is_file() and loc.name.lower().endswith(".ggpk"):
            self.ggpk = GGPK(loc)
        elif loc.is_file() and loc.name == "_.index.bin":
            self.bundles_dir = loc.parent
        else:
            raise FileNotFoundError(f"Invalid location: {loc}")

        self.location = loc
        if index_file is not None:
            from .reconstruct import ReconstructedIndex
            self.index = ReconstructedIndex.load(index_file)
        else:
            self.index = BundleIndex(self._open_bundle_source(INDEX_PATH.split("/", 1)[1]))
        self._bundle_cache: OrderedDict[int, Bundle] = OrderedDict()
        self._cache_lock = threading.RLock()
        self._bundle_cache_size = bundle_cache
        self._ggpk_loose: dict[str, FileEntry] | None = None

    # -- infrastructure ----------------------------------------------------------
    def _open_bundle_source(self, relative: str) -> RandomAccessSource:
        """Open a file inside Bundles2 (in the GGPK or on disk)."""
        if self.ggpk is not None:
            entry = self.ggpk.find(f"Bundles2/{relative}")
            if entry is None:
                raise FileNotFoundError(f"Bundles2/{relative} does not exist in the GGPK")
            return self.ggpk.open_entry(entry)
        assert self.bundles_dir is not None
        return DiskFile(self.bundles_dir / relative)

    def read_bundle_file(self, relative: str) -> bytes:
        """Raw (compressed) bytes of a file in Bundles2, e.g. '_.index.bin'."""
        src = self._open_bundle_source(relative)
        try:
            return src.read_at(0, src.size)
        finally:
            close = getattr(src, "close", None)
            if close:
                close()

    def _bundle(self, bundle_index: int) -> Bundle:
        with self._cache_lock:
            b = self._bundle_cache.get(bundle_index)
            if b is not None:
                self._bundle_cache.move_to_end(bundle_index)
                return b
            b = Bundle(self._open_bundle_source(self.index.bundles[bundle_index].path))
            self._bundle_cache[bundle_index] = b
            if len(self._bundle_cache) > self._bundle_cache_size:
                # The evicted source is not closed: another thread may still be reading it;
                # the file is released once nothing references it.
                self._bundle_cache.popitem(last=False)
            return b

    def close(self) -> None:
        for b in self._bundle_cache.values():
            close = getattr(b.source, "close", None)
            if close:
                close()
        self._bundle_cache.clear()
        if self.ggpk:
            self.ggpk.close()

    def __enter__(self) -> "PoEFileSystem":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- queries ---------------------------------------------------------------
    def missing_bundles(self) -> list[int]:
        """Bundles listed in the index that are not present in this installation."""
        if self.ggpk is not None:
            from .reference import ggpk_bundle_entries
            present = {k.lower() for k in ggpk_bundle_entries(self.ggpk)}
            return [b.index for b in self.index.bundles if b.name.lower() not in present]
        assert self.bundles_dir is not None
        return [b.index for b in self.index.bundles if not (self.bundles_dir / b.path).is_file()]

    @property
    def game(self) -> int:
        """1 for Path of Exile, 2 for Path of Exile 2 (heuristic based on data paths)."""
        if self.index.get("data/balance/mods.datc64") is not None:
            return 2
        return 1

    def loose_ggpk_files(self) -> dict[str, FileEntry]:
        """Loose GGPK files outside of Bundles2."""
        if self.ggpk is None:
            return {}
        if self._ggpk_loose is None:
            self._ggpk_loose = {
                e.path: e
                for e in self.ggpk.walk(skip=lambda d: d.path.lower() == "bundles2/")
                if isinstance(e, FileEntry)
            }
        return self._ggpk_loose

    def iter_files(self, include_loose: bool = False) -> Iterator[VirtualFile]:
        for f in self.index.files:
            if f.path is None:
                continue
            yield VirtualFile(f.path, f.size, "bundle", self.index.bundles[f.bundle_index].name)
        if include_loose:
            for p, e in self.loose_ggpk_files().items():
                yield VirtualFile(p, e.size, "ggpk")

    def exists(self, path: str) -> bool:
        return self.index.get(path) is not None

    def read(self, path: str) -> bytes:
        rec = self.index.get(path)
        if rec is not None:
            return self._bundle(rec.bundle_index).read(rec.offset, rec.size)
        if self.ggpk is not None:
            entry = self.ggpk.find(path)
            if entry is not None:
                return self.ggpk.read(entry)
        raise FileNotFoundError(path)

    # -- bulk extraction --------------------------------------------------------
    def extract(
        self,
        paths: Iterable[str],
        out_dir: str | os.PathLike[str],
        progress: Callable[[int, int, str], None] | None = None,
    ) -> int:
        """Extract several files, grouped by bundle.

        Each 256 KiB chunk of a bundle is decompressed only once even when it
        contains several requested files.
        """
        out = Path(out_dir)
        by_bundle: dict[int, list[FileRecord]] = defaultdict(list)
        loose: list[FileEntry] = []
        missing: list[str] = []
        loose_map = {k.lower(): v for k, v in self.loose_ggpk_files().items()} if self.ggpk else {}
        for p in paths:
            rec = self.index.get(p)
            if rec is not None:
                by_bundle[rec.bundle_index].append(rec)
            elif p.lower() in loose_map:
                loose.append(loose_map[p.lower()])
            else:
                missing.append(p)
        if missing:
            raise FileNotFoundError(f"{len(missing)} paths do not exist, e.g.: {missing[:5]}")

        total = sum(len(v) for v in by_bundle.values()) + len(loose)
        done = 0
        self.skipped: list[str] = []  # files from bundles not present in this installation
        for entry in loose:
            target = out / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.ggpk.read(entry))  # type: ignore[union-attr]
            done += 1
            if progress:
                progress(done, total, entry.path)
        for bundle_index, records in by_bundle.items():
            try:
                bundle = self._bundle(bundle_index)
            except FileNotFoundError:
                # E.g. PoE2 lists bundles for other platforms (Xbox shaders) that PC never downloads.
                self.skipped.extend(r.path or "" for r in records)
                total -= len(records)
                continue
            cs = bundle.header.chunk_size
            chunks: dict[int, bytes] = {}
            records.sort(key=lambda r: r.offset)
            for rec in records:
                if rec.size == 0:
                    data = b""
                else:
                    first = rec.offset // cs
                    last = (rec.offset + rec.size - 1) // cs
                    for i in range(first, last + 1):
                        if i not in chunks:
                            chunks[i] = bundle.read_chunk(i)
                    # Drop chunks that are no longer needed (records are sorted).
                    for i in [i for i in chunks if i < first]:
                        del chunks[i]
                    blob = b"".join(chunks[i] for i in range(first, last + 1))
                    start = rec.offset - first * cs
                    data = blob[start:start + rec.size]
                target = out / rec.path  # type: ignore[operator]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                done += 1
                if progress:
                    progress(done, total, rec.path or "")
        return done
