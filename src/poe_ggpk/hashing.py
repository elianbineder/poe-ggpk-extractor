"""Hash functions used by the bundle index (_.index.bin).

The index identifies every file by a 64-bit hash of its path:

* Since patch 3.21.2: MurmurHash64A (seed 0x1337B33F) over the lowercased
  UTF-8 path.
* Before 3.21.2: 64-bit FNV-1a over the lowercased path + "++".

The algorithm in use is detected from the hash of the first directory record,
which is always the root directory (empty string).
"""

from __future__ import annotations

MASK64 = 0xFFFFFFFFFFFFFFFF

MURMUR_SEED = 0x1337B33F
MURMUR_M = 0xC6A4A7935BD1E995
MURMUR_R = 47

FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3

# Hash of the empty path (root directory) with each algorithm.
ROOT_HASH_MURMUR = 0xF42A94E69CFF42FE
ROOT_HASH_FNV = 0x07E47507B4A92E53


def murmur64a(data: bytes, seed: int = MURMUR_SEED) -> int:
    """MurmurHash64A as used by Path of Exile (trailing '/' ignored)."""
    if not data:
        return ROOT_HASH_MURMUR
    if data[-1:] == b"/":
        data = data[:-1]

    m = MURMUR_M
    r = MURMUR_R
    length = len(data)
    h = (seed ^ (length * m)) & MASK64

    nblocks = length // 8
    for i in range(nblocks):
        k = int.from_bytes(data[i * 8:i * 8 + 8], "little")
        k = (k * m) & MASK64
        k ^= k >> r
        k = (k * m) & MASK64
        h ^= k
        h = (h * m) & MASK64

    tail = data[nblocks * 8:]
    if tail:
        h ^= int.from_bytes(tail, "little")
        h = (h * m) & MASK64

    h ^= h >> r
    h = (h * m) & MASK64
    h ^= h >> r
    return h


def fnv1a64(data: bytes) -> int:
    """64-bit FNV-1a used before patch 3.21.2."""
    h = FNV_OFFSET
    # Directories (ending in '/') are not lowercased (same as LibBundle3).
    body = data[:-1] if data[-1:] == b"/" else data.lower()
    for b in body:
        h = ((h ^ b) * FNV_PRIME) & MASK64
    for b in b"++":
        h = ((h ^ b) * FNV_PRIME) & MASK64
    return h


def murmur2_32(data: bytes, seed: int = 0) -> int:
    """Standard 32-bit MurmurHash2."""
    m = 0x5BD1E995
    mask = 0xFFFFFFFF
    length = len(data)
    h = (seed ^ length) & mask
    nblocks = length // 4
    for i in range(nblocks):
        k = int.from_bytes(data[i * 4:i * 4 + 4], "little")
        k = (k * m) & mask
        k ^= k >> 24
        k = (k * m) & mask
        h = ((h * m) & mask) ^ k
    tail = data[nblocks * 4:]
    if tail:
        h ^= int.from_bytes(tail, "little")
        h = (h * m) & mask
    h ^= h >> 13
    h = (h * m) & mask
    h ^= h >> 15
    return h


def ggpk_name_hash(name: str) -> int:
    """Hash of an entry name inside a GGPK PDIR record.

    MurmurHash2 (seed 0) of the lowercased name encoded as UTF-16LE.
    The entries of every directory are sorted by this value.
    """
    return murmur2_32(name.lower().encode("utf-16-le"))


def detect_algorithm(root_directory_hash: int) -> str:
    """Return 'murmur' or 'fnv' depending on the hash of the index root directory."""
    if root_directory_hash == ROOT_HASH_MURMUR:
        return "murmur"
    if root_directory_hash == ROOT_HASH_FNV:
        return "fnv"
    raise ValueError(f"Unknown hash algorithm (root hash 0x{root_directory_hash:016X})")


def path_hash(path: str | bytes, algorithm: str = "murmur") -> int:
    """Hash of an index path. With murmur the path is lowercased first."""
    data = path.encode("utf-8") if isinstance(path, str) else path
    if algorithm == "murmur":
        return murmur64a(data.lower())
    if algorithm == "fnv":
        return fnv1a64(data)
    raise ValueError(f"Unknown algorithm: {algorithm}")


def murmur64a_many(paths: list[bytes], seed: int = MURMUR_SEED) -> list[int]:
    """Vectorized (numpy) murmur64a for hundreds of thousands of paths.

    Paths must already be lowercased and have no trailing '/'. Falls back to the
    pure Python implementation if numpy is not available.
    """
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy is a declared dependency
        return [murmur64a(p, seed) for p in paths]

    # Paths are grouped by (full blocks, has tail) so that every row in a group
    # follows exactly the same steps, without masks.
    groups: dict[tuple[int, bool], list[int]] = {}
    for i, p in enumerate(paths):
        n = len(p)
        groups.setdefault((n >> 3, (n & 7) != 0), []).append(i)

    m = np.uint64(MURMUR_M)
    r = np.uint64(MURMUR_R)
    out = [0] * len(paths)
    with np.errstate(over="ignore"):
        for (full, has_tail), members in groups.items():
            if full == 0 and not has_tail:
                for i in members:
                    out[i] = ROOT_HASH_MURMUR
                continue
            width = (full + has_tail) * 8
            buf = b"".join(paths[i].ljust(width, b"\0") for i in members)
            words = np.frombuffer(buf, dtype="<u8").reshape(len(members), full + has_tail)
            lengths = np.fromiter((len(paths[i]) for i in members), dtype=np.uint64, count=len(members))
            h = np.uint64(seed) ^ (lengths * m)
            for j in range(full):
                k = words[:, j] * m
                k ^= k >> r
                k *= m
                h ^= k
                h *= m
            if has_tail:
                # Padding bytes are zero, which is equivalent to masking the tail.
                h ^= words[:, full]
                h *= m
            h ^= h >> r
            h *= m
            h ^= h >> r
            for i, v in zip(members, h.tolist()):
                out[i] = v
    return out
