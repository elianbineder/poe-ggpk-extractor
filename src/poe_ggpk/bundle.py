"""Oodle-compressed bundles (``*.bundle.bin``).

Layout (little-endian)::

    int32  uncompressed_size
    int32  total_payload_size      (compressed size)
    int32  head_payload_size       (= 48 + 4 * chunk_count)
    int32  first_file_encode       (Oodle compressor: 8 Kraken, 9 Mermaid, 12 Hydra/Leviathan...)
    int32  unk                     (= 1)
    int64  uncompressed_size_long
    int64  total_payload_size_long
    int32  chunk_count
    int32  chunk_size              (= 256 KiB)
    int32  unk[4]
    int32  chunk_sizes[chunk_count] (compressed size of each chunk)
    byte   chunks[...]

Every chunk is decompressed independently, so any range can be read by
decompressing only the chunks that cover it.
"""

from __future__ import annotations

import os
import struct
import threading
from dataclasses import dataclass
from typing import Protocol

_HEADER = struct.Struct("<iiiiiqqii4i")  # 60 bytes

COMPRESSORS = {
    8: "Kraken",
    9: "Mermaid",
    10: "Bitknit",
    11: "Selkie",
    12: "Hydra",
    13: "Leviathan",
}


class BundleError(Exception):
    pass


class RandomAccessSource(Protocol):
    size: int

    def read_at(self, offset: int, size: int) -> bytes: ...


class DiskFile:
    """Byte source backed by a file on disk (Steam/Epic installs)."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = os.fspath(path)
        self.size = os.path.getsize(self.path)
        self._fh = open(self.path, "rb")
        self._lock = threading.Lock()

    def read_at(self, offset: int, size: int) -> bytes:
        with self._lock:
            self._fh.seek(offset)
            return self._fh.read(size)

    def close(self) -> None:
        self._fh.close()


class BytesSource:
    def __init__(self, data: bytes):
        self.data = data
        self.size = len(data)

    def read_at(self, offset: int, size: int) -> bytes:
        return self.data[offset:offset + size]


def _oodle_decompress(data: bytes, raw_size: int) -> bytes:
    try:
        import ooz
    except ImportError as exc:  # pragma: no cover
        raise BundleError("The 'pyooz' package is required to decompress Oodle (pip install pyooz)") from exc
    out = ooz.decompress(data, raw_size)
    if len(out) != raw_size:
        raise BundleError(f"Oodle returned {len(out)} bytes, expected {raw_size}")
    return bytes(out)


@dataclass(slots=True)
class BundleHeader:
    uncompressed_size: int
    compressed_size: int
    head_size: int
    compressor: int
    chunk_count: int
    chunk_size: int

    @property
    def compressor_name(self) -> str:
        return COMPRESSORS.get(self.compressor, f"unknown({self.compressor})")


class Bundle:
    def __init__(self, source: RandomAccessSource):
        self.source = source
        raw = source.read_at(0, _HEADER.size)
        if len(raw) < _HEADER.size:
            raise BundleError("Bundle too small")
        (unc, comp, head, encoder, _unk, unc_long, comp_long,
         chunk_count, chunk_size, *_rest) = _HEADER.unpack(raw)
        if unc != unc_long or comp != comp_long or chunk_size <= 0:
            raise BundleError("Inconsistent bundle header")
        self.header = BundleHeader(unc, comp, head, encoder, chunk_count, chunk_size)
        sizes_raw = source.read_at(_HEADER.size, 4 * chunk_count)
        self.chunk_sizes = list(struct.unpack(f"<{chunk_count}i", sizes_raw))
        # Absolute offset of every compressed chunk inside the bundle.
        self.chunk_offsets: list[int] = []
        pos = _HEADER.size + 4 * chunk_count
        for s in self.chunk_sizes:
            self.chunk_offsets.append(pos)
            pos += s
        if pos > source.size:
            raise BundleError("Chunks exceed the bundle size")

    @property
    def uncompressed_size(self) -> int:
        return self.header.uncompressed_size

    def _chunk_raw_size(self, i: int) -> int:
        h = self.header
        if i == h.chunk_count - 1:
            return h.uncompressed_size - h.chunk_size * (h.chunk_count - 1)
        return h.chunk_size

    def read_chunk(self, i: int) -> bytes:
        comp = self.source.read_at(self.chunk_offsets[i], self.chunk_sizes[i])
        raw_size = self._chunk_raw_size(i)
        if len(comp) == raw_size:
            # Incompressible chunks may be stored raw; try to decompress anyway and
            # return the bytes as-is if that fails.
            try:
                return _oodle_decompress(comp, raw_size)
            except Exception:
                return comp
        return _oodle_decompress(comp, raw_size)

    def read(self, offset: int = 0, size: int | None = None) -> bytes:
        """Decompress only the chunks needed for the requested range."""
        total = self.header.uncompressed_size
        if size is None:
            size = total - offset
        if offset < 0 or size < 0 or offset + size > total:
            raise BundleError(f"Range {offset}+{size} outside of bundle ({total} bytes)")
        if size == 0:
            return b""
        cs = self.header.chunk_size
        first = offset // cs
        last = (offset + size - 1) // cs
        data = b"".join(self.read_chunk(i) for i in range(first, last + 1))
        start = offset - first * cs
        return data[start:start + size]
