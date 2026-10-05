# poe-ggpk-extractor

[![tests](https://github.com/elianbineder/poe-ggpk-extractor/actions/workflows/tests.yml/badge.svg)](https://github.com/elianbineder/poe-ggpk-extractor/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Read and extract the game data of **Path of Exile** and **Path of Exile 2** from `Content.ggpk`
(standalone client) or the `Bundles2` folder (Steam/Epic).

> Unofficial fan-made tool, not affiliated with or endorsed by Grinding Gear Games.
> See the [legal notice](#legal-notice).

- Browse and extract any game file; convert `.dds` textures to PNG and UTF-16 texts to UTF-8.
- Export the `.datc64` data tables to JSON/CSV in all 10 game languages, with foreign keys
  resolved, using the community [dat-schema](https://github.com/poe-tool-dev/dat-schema).
- Open the pre-league torrent `Content.ggpk`, which ships without its index: the index is
  reconstructed from the previous version, and new tables can be read without a schema.
- Graphical interface and command line.

The file formats are specified in [docs/FORMAT.md](docs/FORMAT.md).

## Installation

Requires Python 3.10+ (tested on Windows; shortcuts are Windows-only).

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
poe-ggpk shortcut
```

`poe-ggpk shortcut` adds **PoE GGPK Extractor** to the Start menu and the desktop
(`--no-desktop` to skip the desktop, `--remove` to delete both). The GUI can also be started
with `poe-ggpk-gui`.

## Graphical interface

| Tab | Purpose |
|---|---|
| Files | Browse or search the game files, preview textures, texts and tables, extract a selection. |
| Tables | View any table in a grid, choose the language, resolve references, export to JSON/CSV. *Changed only* lists the tables that differ from the reference version, including new unnamed tables. |
| Torrent / no index | Reconstruct the index of a torrent `Content.ggpk`, open it, and create snapshots. |
| Info | Summary of the opened location. |

## Command line

The game is located automatically (`--game 2` for PoE2); `--ggpk` accepts a `Content.ggpk`,
a game folder or a `_.index.bin`.

| Command | Purpose |
|---|---|
| `info` | Summary of the installation and its index |
| `ls [patterns]` | List files (`-l` size and bundle, `-r` regex) |
| `cat <path>` | Print a file, decoding UTF-16 text |
| `extract <patterns> -o <dir>` | Extract files (`-c` also converts DDS and texts) |
| `tables` | List tables and whether the schema matches them |
| `dat <tables> [-o <path>]` | Export tables (`-f csv`, `--lang`, `--resolve`, `--all`) |
| `snapshot -o <dir>` | Save the current version as a reconstruction reference |
| `reconstruct --reference <ref>` | Rebuild a missing index |
| `schema` | Update the cached dat-schema |
| `gui`, `shortcut` | Open the GUI / create its shortcuts |

```bash
poe-ggpk extract "art/2ditems/currency/*.dds" -o out -c
poe-ggpk dat BaseItemTypes --resolve --lang Spanish -o base_items.json
```

Paths are lowercase and patterns are case-insensitive. The schema is cached in
`%LOCALAPPDATA%\poe-ggpk\cache` and refreshed daily.

## Index-less GGPK (pre-league torrent)

The torrent `Content.ggpk` contains the new bundles but not `Bundles2/_.index.bin`, so file
names and positions are unknown. `reconstruct` rebuilds the index by comparing the bundles
with the installed version or with a snapshot taken before the patch.

```bash
poe-ggpk snapshot -o snapshots/current --fingerprints        # optional, before the patch
poe-ggpk reconstruct --ggpk D:\torrent --reference "C:\Program Files (x86)\Grinding Gear Games\Path of Exile" -o league.index.gz
poe-ggpk tables --changed --ggpk D:\torrent --index league.index.gz
poe-ggpk dat "_unknown/Folders/A/data.datc64/0000217452.datc64" --ggpk D:\torrent --index league.index.gz
```

Any command accepts `--index` to use a reconstructed index. Each file gets a confidence level:

| Level | Meaning |
|---|---|
| `same` | Bundle unchanged; layout copied. Exact. |
| `matched` | Unchanged file found by its fingerprint in a changed bundle. Exact. |
| `modified` | New version of an existing file, delimited by its neighbours or by its schema size. |
| `unknown` | New content, extracted as `_unknown/<bundle>/<offset>.<ext>`. |

Ambiguous content is left `unknown` rather than given a possibly wrong name. In simulated
patches on real bundles, 99.9 % of unchanged files were located and 0.13 % of the names
assigned were wrong.

New tables have no name and are not in the schema yet; `dat` shows them with inferred
columns named by type and byte offset (`string_8`, `i32_24`, `array_32`, …).

## Library

```python
from poe_ggpk import GameData, PoEFileSystem, Schema

with PoEFileSystem(r"C:\Program Files (x86)\Grinding Gear Games\Path of Exile") as fs:
    icon = fs.read("art/2ditems/currency/currencyrerollrare.dds")
    items = GameData(fs, Schema.load()).resolve("BaseItemTypes")
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

Integration tests run against the installed game (or the path in `POE_GGPK`) and are skipped
when it is missing. `tools/fake_torrent.py` builds an index-less GGPK for testing and
`tools/simulate_patch.py` measures the reconstruction accuracy.

## Limitations

- Read-only.
- Table columns depend on the community schema, which may lag behind new patches.
- Meshes, animations and materials are extracted without conversion.
- Names of files added in a new league cannot be recovered without the real index.

## Legal notice

**Not affiliated with Grinding Gear Games.** This is an unofficial, fan-made project. It is
not affiliated with, endorsed, sponsored or approved by Grinding Gear Games. *Path of Exile*,
*Path of Exile 2* and *Grinding Gear Games* are trademarks or registered trademarks of
Grinding Gear Games Ltd.; they are used here only to identify the game the tool works with.

**Game content.** All game files, data, texts, images, audio and other assets read by this
tool are the property of Grinding Gear Games. This repository contains no game content. The
tool works on a copy of the game that you install yourself and only reads it locally: it
never modifies the game files, interacts with the running game client or connects to the
game servers.

**Your responsibility.** You are responsible for how you use the tool and the data you
extract, including compliance with the
[Path of Exile Terms of Use](https://www.pathofexile.com/legal/terms-of-use-and-privacy-policy)
and applicable copyright law. Do not redistribute extracted assets without the rights
holder's permission. Content read from pre-release game files (such as the pre-league
torrent download) may be unannounced; consider this before publishing it.

**No warranty.** The software is provided "as is", without warranty of any kind, as stated
in the [license](LICENSE). Use it at your own risk.

## License

This project's source code is released under the [MIT License](LICENSE).
Copyright © 2026 Elian Bineder.

Third-party components keep their own licenses. They are installed separately and are not
included in this repository:

| Component | Use | License |
|---|---|---|
| [pyooz](https://pypi.org/project/pyooz/) (bindings to [ooz](https://github.com/powzix/ooz)) | Oodle decompression | GPL-3.0-or-later |
| [NumPy](https://numpy.org) | Vectorized hashing and scanning | BSD-3-Clause |
| [Pillow](https://python-pillow.org) | Texture conversion and preview | MIT-CMU |
| [dat-schema](https://github.com/poe-tool-dev/dat-schema) | Table column definitions, downloaded at runtime | MIT |

Because pyooz is licensed under the GPL, any redistribution that bundles it together with
this project (for example a standalone executable) must comply with the GPL-3.0.

ooz is an independent open-source implementation of the Oodle compression formats; Oodle is
a product of Epic Games Tools (formerly RAD Game Tools), which is not affiliated with this
project. The file formats were implemented independently, using the public documentation
and projects listed in [docs/FORMAT.md](docs/FORMAT.md#references); no code from those
projects is included.
