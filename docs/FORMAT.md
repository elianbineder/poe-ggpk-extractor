# Content.ggpk format specification

All integers are little-endian.

## Overview

```
Content.ggpk
├── loose files (gateway_list.txt, FMOD/, shader caches…)
└── Bundles2/
    ├── _.index.bin        bundle index
    └── **/*.bundle.bin    Oodle-compressed bundles holding the game files
```

Steam and Epic installations have no `Content.ggpk`; the `Bundles2` folder is stored
directly on disk.

## 1. GGPK records

The file is a sequence of records. Each starts with `uint32 length` (whole record, header
included) and `char[4] tag`.

### GGPK — file header, offset 0

| Type | Field | Notes |
|---|---|---|
| uint32 | length | 28 |
| char[4] | tag | `GGPK` |
| uint32 | version | 3 = UTF-16 names (PC), 4 = UTF-32 names (Mac) |
| uint64 | root_offset | root `PDIR` |
| uint64 | free_offset | first `FREE` record |

### PDIR — directory

| Type | Field | Notes |
|---|---|---|
| uint32 | length | |
| char[4] | tag | `PDIR` |
| uint32 | name_length | characters, including the terminator |
| uint32 | entry_count | |
| byte[32] | sha256 | hash of the children's hashes |
| char[name_length] | name | null-terminated; empty for the root |
| {uint32 name_hash, uint64 offset}[entry_count] | entries | sorted by `name_hash` |

`name_hash` is MurmurHash2-32 (seed 0) of the lowercased name encoded as UTF-16LE, which
allows a binary search for a child.

### FILE

| Type | Field | Notes |
|---|---|---|
| uint32 | length | |
| char[4] | tag | `FILE` |
| uint32 | name_length | characters, including the terminator |
| byte[32] | sha256 | hash of `data` |
| char[name_length] | name | |
| byte[] | data | remainder of the record |

### FREE

`uint32 length`, `char[4] "FREE"`, `uint64 next_free_offset`, unused bytes.

## 2. Bundles

| Type | Field | Notes |
|---|---|---|
| int32 | uncompressed_size | |
| int32 | total_payload_size | sum of `chunk_sizes` |
| int32 | head_payload_size | `48 + 4 * chunk_count` |
| int32 | compressor | Oodle: 8 Kraken, 9 Mermaid, 12 Hydra, 13 Leviathan |
| int32 | unknown | 1 |
| int64 | uncompressed_size_long | equals `uncompressed_size` |
| int64 | total_payload_size_long | equals `total_payload_size` |
| int32 | chunk_count | |
| int32 | chunk_size | 262144 |
| int32[4] | unknown | 0 |
| int32[chunk_count] | chunk_sizes | compressed size of each chunk |
| byte[] | chunks | |

Each chunk decompresses independently to `chunk_size` bytes (the last one to the remainder).

The decompressed bundle is the exact concatenation of its files, from offset 0 to the end
without gaps. Files with identical content share the same offset and size. The SHA-256 in the
bundle's `FILE` record identifies its content without decompressing it.

## 3. Bundle index

`Bundles2/_.index.bin` is a bundle. Decompressed:

```
int32 bundle_count
bundle_count × { int32 name_length; char name[name_length]; int32 uncompressed_size }
int32 file_count
file_count × { uint64 path_hash; int32 bundle_index; int32 offset; int32 size }
int32 directory_count
directory_count × { uint64 path_hash; int32 offset; int32 size; int32 recursive_size }
byte[] path_bundle
```

- Bundle `i` is stored at `Bundles2/<name>.bundle.bin`.
- File `offset` and `size` refer to the decompressed bundle.
- `path_bundle` is another bundle holding the compressed path lists.

### Path hash

| Version | Algorithm | Root directory hash |
|---|---|---|
| 3.21.2 and later | MurmurHash64A, seed `0x1337B33F`, of the lowercased UTF-8 path | `0xF42A94E69CFF42FE` |
| Earlier | FNV-1a 64 of the lowercased path followed by `++` | `0x07E47507B4A92E53` |

A trailing `/` is ignored. The algorithm is identified by the hash of the first directory
record, which is the root (empty path). Stored paths are lowercase.

### Path lists

Each directory record points to `path_bundle[offset : offset + size]`, decoded as:

```
temp = []; base = false
while at least 4 bytes remain:
    n = int32
    if n == 0:
        base = not base
        if base: temp = []
        continue
    s = null-terminated UTF-8 string
    if n - 1 < len(temp): s = temp[n - 1] + s
    if base: temp.append(s)     # reusable prefix
    else:    emit(s)            # file path
```

Each emitted path is hashed and matched against the file records.

### Auxiliary files

- `_.index.high.bin`, `_.index.low.bin`: valid indexes with no bundles or files.
- `ggdh`: 32 ASCII hexadecimal characters.

The PoE2 index also lists Xbox shader-cache bundles (`shadercached3d12_xs`) that are not
present in PC installations.

## 4. Data tables

Tables are stored at `data/<name>.datc64` (PoE1) and `data/balance/<name>.datc64` (PoE2), with
translations in language subfolders (`data/french/…`).

```
uint32 row_count
byte   rows[row_count × row_size]
byte   variable_section[]      starts with 8 bytes 0xBB
```

`row_size` is not stored. The variable section starts at the first `0xBB×8` marker whose
offset from byte 4 is a multiple of `row_count`; each table contains exactly one such marker.
Strings and arrays store offsets relative to the start of the variable section, marker
included.

| Column type | Bytes | Encoding |
|---|---|---|
| bool, i8, u8 | 1 | |
| i16, u16 | 2 | |
| i32, u32, f32 | 4 | |
| i64, u64, f64 | 8 | |
| string | 8 | offset to a UTF-16LE string terminated by `00 00 00 00` |
| row | 8 | row index in the same table; null is `0xFEFEFEFEFEFEFEFE` |
| foreignrow | 16 | row index in another table + 8 unused bytes; same null |
| enumrow | 4 | enumeration index |
| array of T | 16 | `uint64 count`, `uint64 offset` to `count` consecutive T |
| interval of T | 2 × T | minimum and maximum |

Column layouts are not stored in the file; they come from
[dat-schema](https://github.com/poe-tool-dev/dat-schema) (`schema.min.json`, format 7), where
`validFor` marks tables for PoE1 (1), PoE2 (2) or both (3).

The variable section ends at the last byte referenced by any row, so a table's size can be
derived from its rows and the schema.

### `.datcl64`

PoE1 ships a `.datcl64` beside each `.datc64`, with identical rows and layout but UTF-32LE
strings (terminated by `00 00 00 00`). When both are stored contiguously they share
`row_count` and `row_size`.

## 5. Other formats

| Extension | Content |
|---|---|
| `.dds` | DirectDraw Surface texture (mostly DX10 BC7, BC1, BC3). Its size follows from the header. |
| `.dds.header` | Texture dimensions and a reduced DDS. |
| `.txt`, `.ot`, `.it`, `.ais`, `.csd`, `.ao`, `.pet`, `.epk` | UTF-16LE text, usually with BOM. |
| `.mat` | UTF-16LE JSON. |
| `.ogg` | Ogg Vorbis audio; ends at the page flagged end-of-stream. |
| `.bank` | FMOD sound bank (loose in the GGPK). |
| `.sm`, `.smd`, `.tgm`, `.ast`, `.amd`, `.fxgraph` | Meshes, animations and effects (proprietary). |

## 6. Index reconstruction

Used when `_.index.bin` is missing, with a previous version as reference. For each bundle:

1. **Unchanged** — same name and SHA-256 as in the reference: the reference layout applies.
2. **Changed** — the bundle is decompressed and searched for the reference files by
   fingerprint (size, BLAKE2b-64, first and last 16 bytes):
   - a vectorised pass finds every occurrence of each file's least common 16-byte prefix or
     suffix, verified by the full hash;
   - matches are extended to neighbouring files in reference order;
   - positions with a format signature are probed for files that moved.
3. **Gaps** between matches:
   - a gap missing exactly one reference file is that file's new version;
   - a table's new version is delimited by its schema size, if the schema reproduces the size
     of its reference version;
   - DDS and Ogg files are delimited by their headers;
   - remaining bytes are scanned for tables (one `0xBB×8` marker each), and the rest is left
     unnamed.
4. **New bundle names** are matched against reference bundles with the same base name
   (`Tiny_9`, `Tiny_9_1`).

Tables without a schema are read by inferring columns: at each row offset, values are tested
for the shape of a foreign key, a string pointer or an array; remaining bytes are shown as
32-bit integers.

## References

- [poedb.tw/us/GGPK](https://poedb.tw/us/GGPK)
- [LibGGPK3](https://github.com/aianlinb/LibGGPK3)
- [poe-dat-viewer](https://github.com/SnosMe/poe-dat-viewer)
- [dat-schema](https://github.com/poe-tool-dev/dat-schema)
- [ooz](https://github.com/powzix/ooz)
