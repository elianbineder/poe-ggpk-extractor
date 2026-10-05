"""Reader for the Content.ggpk container.

The file is a sequence of records sharing the header
``uint32 length`` + ``char[4] tag``:

* ``GGPK``: header (version + offsets of the root and of the free list).
* ``PDIR``: directory (name, SHA-256 hash and list of child entries).
* ``FILE``: file (name, SHA-256 hash and the raw data).
* ``FREE``: reusable free space.

Names are UTF-16LE (version 3, PC) or UTF-32LE (version 4, Mac),
null-terminated; the length includes the terminator.
"""

from __future__ import annotations

import os
import struct
import threading
from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Iterator

from .hashing import ggpk_name_hash

TAG_GGPK = b"GGPK"
TAG_PDIR = b"PDIR"
TAG_FILE = b"FILE"
TAG_FREE = b"FREE"

_HEADER = struct.Struct("<I4s")
_GGPK_BODY = struct.Struct("<IQQ")
_ENTRY = struct.Struct("<IQ")  # name hash (murmur2 32) + offset of the child record
HASH_SIZE = 32


class GGPKError(Exception):
    pass


@dataclass(slots=True)
class FileEntry:
    """File stored directly inside the GGPK."""

    name: str
    path: str
    record_offset: int
    data_offset: int
    size: int
    sha256: bytes


@dataclass(slots=True)
class DirectoryEntry:
    name: str
    path: str
    record_offset: int
    sha256: bytes
    children: list[tuple[int, int]] = field(default_factory=list)  # (name_hash, offset)


class GGPK:
    """Read-only access to a Content.ggpk."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = os.fspath(path)
        self._fh: BinaryIO = open(self.path, "rb")
        # Seek + read must be atomic: the GUI reads from several threads at once.
        self._lock = threading.RLock()
        length, tag = _HEADER.unpack(self._read_at(0, _HEADER.size))
        if tag != TAG_GGPK:
            self._fh.close()
            raise GGPKError(f"{self.path} is not a GGPK file (tag {tag!r})")
        self.version, self.root_offset, self.free_offset = _GGPK_BODY.unpack(self._fh.read(_GGPK_BODY.size))
        if self.version not in (2, 3, 4):
            raise GGPKError(f"Unsupported GGPK version: {self.version}")
        self._char_size = 4 if self.version == 4 else 2
        self._encoding = "utf-32-le" if self.version == 4 else "utf-16-le"
        self._files: dict[str, FileEntry] | None = None
        self._dir_cache: dict[str, DirectoryEntry] = {}

    # -- low-level helpers ------------------------------------------------------------
    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "GGPK":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _read_at(self, offset: int, size: int) -> bytes:
        with self._lock:
            self._fh.seek(offset)
            data = self._fh.read(size)
        if len(data) != size:
            raise GGPKError(f"Truncated read at offset {offset} ({len(data)}/{size} bytes)")
        return data

    def read_record(self, offset: int, parent_path: str = "") -> FileEntry | DirectoryEntry | tuple[bytes, int]:
        """Read the record at ``offset``. FREE/GGPK records are returned as (tag, length)."""
        # A single read is usually enough for header + name; it is extended when needed.
        with self._lock:
            self._fh.seek(offset)
            buf = self._fh.read(512)
        if len(buf) < _HEADER.size:
            raise GGPKError(f"Truncated record at offset {offset}")
        length, tag = _HEADER.unpack_from(buf)
        if tag == TAG_FILE:
            name_len = struct.unpack_from("<I", buf, 8)[0]
            header_size = 12 + HASH_SIZE + name_len * self._char_size
            if len(buf) < header_size:
                buf = self._read_at(offset, header_size)
            sha = buf[12:12 + HASH_SIZE]
            name = buf[12 + HASH_SIZE:header_size].decode(self._encoding).rstrip("\0")
            return FileEntry(name, f"{parent_path}{name}", offset, offset + header_size, length - header_size, sha)
        if tag == TAG_PDIR:
            name_len, count = struct.unpack_from("<II", buf, 8)
            name_end = 16 + HASH_SIZE + name_len * self._char_size
            total = name_end + count * _ENTRY.size
            if len(buf) < total:
                buf = self._read_at(offset, total)
            sha = buf[16:16 + HASH_SIZE]
            name = buf[16 + HASH_SIZE:name_end].decode(self._encoding).rstrip("\0")
            children = list(_ENTRY.iter_unpack(buf[name_end:total]))
            path = f"{parent_path}{name}/" if name else parent_path
            return DirectoryEntry(name, path, offset, sha, children)
        if tag in (TAG_FREE, TAG_GGPK):
            return tag, length
        raise GGPKError(f"Unknown record tag {tag!r} at offset {offset}")

    # -- tree traversal -----------------------------------------------------------------
    def walk(self, start: DirectoryEntry | None = None,
             skip: Callable[[DirectoryEntry], bool] | None = None) -> Iterator[FileEntry | DirectoryEntry]:
        """Depth-first traversal yielding directories and files.

        ``skip`` prunes subdirectories (they are neither yielded nor traversed).
        """
        root = start or self.read_record(self.root_offset)
        assert isinstance(root, DirectoryEntry)
        stack = [root]
        while stack:
            d = stack.pop()
            yield d
            for _, child_off in d.children:
                rec = self.read_record(child_off, d.path)
                if isinstance(rec, DirectoryEntry):
                    if skip is None or not skip(rec):
                        stack.append(rec)
                elif isinstance(rec, FileEntry):
                    yield rec

    @property
    def files(self) -> dict[str, FileEntry]:
        """Path -> FileEntry map of every loose file in the GGPK (lazy)."""
        if self._files is None:
            self._files = {e.path: e for e in self.walk() if isinstance(e, FileEntry)}
        return self._files

    def _child(self, directory: DirectoryEntry, name: str) -> FileEntry | DirectoryEntry | None:
        """Find a child by name using the name hash (with a linear fallback)."""
        wanted = ggpk_name_hash(name)
        lname = name.lower()
        candidates = [off for h, off in directory.children if h == wanted]
        for off in candidates or [off for _, off in directory.children]:
            rec = self.read_record(off, directory.path)
            if isinstance(rec, (FileEntry, DirectoryEntry)) and rec.name.lower() == lname:
                return rec
        return None

    def find(self, path: str) -> FileEntry | None:
        """Find a file by path, visiting only the directories on the way."""
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if not parts:
            return None
        node = self._directory("/".join(parts[:-1]))
        if node is None:
            return None
        rec = self._child(node, parts[-1])
        return rec if isinstance(rec, FileEntry) else None

    def _directory(self, path: str) -> DirectoryEntry | None:
        key = path.lower()
        cached = self._dir_cache.get(key)
        if cached is not None:
            return cached
        if not path:
            node = self.read_record(self.root_offset)
        else:
            parent_path, _, name = path.rpartition("/")
            parent = self._directory(parent_path)
            node = self._child(parent, name) if parent is not None else None
        if not isinstance(node, DirectoryEntry):
            return None
        self._dir_cache[key] = node
        return node

    def read(self, entry: FileEntry, offset: int = 0, size: int | None = None) -> bytes:
        if size is None:
            size = entry.size - offset
        if offset < 0 or offset + size > entry.size:
            raise GGPKError(f"Range outside of file {entry.path}")
        return self._read_at(entry.data_offset + offset, size)

    def open_entry(self, entry: FileEntry) -> "GGPKFileView":
        return GGPKFileView(self, entry)


class GGPKFileView:
    """Random-access view of a file inside the GGPK (used by bundles)."""

    def __init__(self, ggpk: GGPK, entry: FileEntry):
        self.ggpk = ggpk
        self.entry = entry
        self.size = entry.size

    def read_at(self, offset: int, size: int) -> bytes:
        return self.ggpk.read(self.entry, offset, size)
