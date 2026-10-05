"""Bundle index: ``Bundles2/_.index.bin``.

The index is itself a bundle. Once decompressed it contains::

    int32 bundle_count
    bundle_count x { int32 name_length; char name[name_length]; int32 uncompressed_size }
    int32 file_count
    file_count x { uint64 path_hash; int32 bundle_index; int32 offset; int32 size }
    int32 directory_count
    directory_count x { uint64 path_hash; int32 offset; int32 size; int32 recursive_size }
    byte  path_bundle[...]   <- another bundle with the compressed paths

Paths are not stored in plain form: for every directory the path bundle holds a
sequence of commands alternating between a "base" phase (which builds reusable
prefixes) and a "generation" phase (which emits full paths by joining a prefix
and a suffix). See :func:`decode_paths`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

from .bundle import Bundle, BytesSource, RandomAccessSource
from .hashing import detect_algorithm, murmur64a_many, path_hash

_FILE = struct.Struct("<QIII")
_DIR = struct.Struct("<QIII")


@dataclass(slots=True)
class BundleRecord:
    index: int
    name: str
    uncompressed_size: int

    @property
    def path(self) -> str:
        """Path relative to the Bundles2 folder."""
        return f"{self.name}.bundle.bin"


@dataclass(slots=True)
class FileRecord:
    path_hash: int
    bundle_index: int
    offset: int
    size: int
    path: str | None = None


@dataclass(slots=True)
class DirectoryRecord:
    path_hash: int
    offset: int
    size: int
    recursive_size: int


def decode_paths(data: bytes, offset: int, size: int) -> Iterator[bytes]:
    """Decode the paths of a directory record.

    32-bit integers are read. A 0 toggles the phase; entering the base phase
    clears the prefix list. Any other value ``n`` is followed by a
    null-terminated string; if ``n-1`` is a valid index into the prefix list,
    the string is appended to that prefix. In the base phase the result is
    added to the list; in the generation phase it is a file path.
    """
    temp: list[bytes] = []
    base = False
    pos = offset
    end = offset + size
    while pos <= end - 4:
        idx = int.from_bytes(data[pos:pos + 4], "little")
        pos += 4
        if idx == 0:
            base = not base
            if base:
                temp.clear()
            continue
        nul = data.index(b"\0", pos)
        s = data[pos:nul]
        pos = nul + 1
        idx -= 1
        if idx < len(temp):
            s = temp[idx] + s
        if base:
            temp.append(s)
        else:
            yield s


class BundleIndex:
    def __init__(self, source: RandomAccessSource, parse_paths: bool = True):
        self.bundle = Bundle(source)
        data = self.bundle.read()
        pos = 0

        def i32() -> int:
            nonlocal pos
            v = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
            return v

        self.bundles: list[BundleRecord] = []
        for i in range(i32()):
            n = i32()
            name = data[pos:pos + n].decode("utf-8")
            pos += n
            self.bundles.append(BundleRecord(i, name, i32()))

        count = i32()
        self.files: list[FileRecord] = [
            FileRecord(*t) for t in _FILE.iter_unpack(data[pos:pos + count * _FILE.size])
        ]
        pos += count * _FILE.size
        self.by_hash: dict[int, FileRecord] = {f.path_hash: f for f in self.files}

        count = i32()
        self.directories: list[DirectoryRecord] = [
            DirectoryRecord(*t) for t in _DIR.iter_unpack(data[pos:pos + count * _DIR.size])
        ]
        pos += count * _DIR.size
        self._path_bundle_data = data[pos:]
        self.hash_algorithm = detect_algorithm(self.directories[0].path_hash) if self.directories else "murmur"
        self.unresolved = 0
        self._paths_parsed = False
        self._by_path: dict[str, FileRecord] = {}
        if parse_paths:
            self.parse_paths()

    def parse_paths(self) -> int:
        """Set ``FileRecord.path`` on every file. Returns how many could not be resolved."""
        if self._paths_parsed:
            return self.unresolved
        path_data = Bundle(BytesSource(self._path_bundle_data)).read()
        paths: list[bytes] = []
        for d in self.directories:
            paths.extend(decode_paths(path_data, d.offset, d.size))

        if self.hash_algorithm == "murmur":
            hashes = murmur64a_many([p.lower() for p in paths])
        else:
            hashes = [path_hash(p, "fnv") for p in paths]

        for p, h in zip(paths, hashes):
            rec = self.by_hash.get(h)
            if rec is None:
                continue
            rec.path = p.decode("utf-8")
            self._by_path[rec.path.lower()] = rec
        self.unresolved = sum(1 for f in self.files if f.path is None)
        self._paths_parsed = True
        return self.unresolved

    def hash(self, path: str) -> int:
        return path_hash(path.replace("\\", "/").strip("/"), self.hash_algorithm)

    def get(self, path: str) -> FileRecord | None:
        path = path.replace("\\", "/").strip("/")
        rec = self._by_path.get(path.lower())
        if rec is None:
            rec = self.by_hash.get(self.hash(path))
        return rec

    def __iter__(self) -> Iterator[FileRecord]:
        return iter(self.files)

    def __len__(self) -> int:
        return len(self.files)
