# 3D Model Library

Catalog a large, messy 3D-printing collection into a browsable, searchable gallery — without moving, renaming, or touching a single one of your files.

Built for the situation where you have tens of thousands of STLs spread across drives and cloud folders, you know you own something, and you cannot find it.

![gallery screenshot](docs/gallery.png)

---

## What it does

- **Scans folders you choose** for `.stl`, `.obj`, `.3mf`, `.step`/`.stp`, `.ply`, plus `.zip` archives and slicer project files.
- **Groups by project.** A folder containing parts is one entry, not forty.
- **Makes a thumbnail for each project.** Reuses an artist's shipped preview image when one exists; otherwise renders the mesh.
- **Classifies and tags** by category, faction, type and source using keyword rules you control.
- **Builds a self-contained `gallery.html`** — filter, search, group, and copy any item's filename or full path to your clipboard to go find it on disk.
- **Exports `catalog.csv`** for spreadsheets, and `catalog.json` for anything else.

Strictly read-only on your model files. The only things it writes are its own catalog, gallery, and thumbnails.

## Why not just use Explorer

Explorer shows you one folder at a time and can't tell you that the Warhound titan you're hunting is in `- Paid\SomeCreator\April releases-20200426T063026Z-001\`. This gives you every project in one grid, with pictures, filters, and a copy-the-filename button.

---

## Requirements

- Python 3.9+
- Windows for the double-click menu (`LIBRARY.bat`). The Python script itself runs anywhere.

## Install

```bash
git clone https://github.com/njefferson/3dmodel-library.git
cd 3dmodel-library
pip install -r requirements.txt
```

## Quick start

Tell it where your models live:

```bash
python update_catalog.py --add-source "D:\path\to\your\models"
python update_catalog.py --rescan-all
```

That scans, classifies, renders thumbnails, and writes `gallery.html`. Open it in any browser.

On Windows you can skip the commands entirely — double-click **`LIBRARY.bat`** for a numbered menu.

---

## The menu

```
BROWSE           1  gallery          2  spreadsheet
CHECK (safe)     3  status           4  scanned folders   5  test 6 thumbnails
PICTURES         6  make missing     7  redo all
FOLDERS/FILES    8  add folder       9  stop scanning    10  find new files
                11  fix moved       12  remove dead entries
OTHER            R  refresh          B  back up           H  help
```

Everything that can modify the catalog backs it up to `backups/` first.

## Commands

| Command | What it does |
|---|---|
| `--add-source PATH` / `--remove-source PATH` / `--sources` | manage the folders that get scanned |
| `"D:\some\folder"` | scan a folder and fold it into the library |
| `--rescan-all` | rescan every listed folder, adding what's new |
| `--thumbs-only` | make thumbnails only for items that lack one |
| `--thumbs-only --force` | rebuild every thumbnail |
| `--rebuild-views` | regenerate gallery + csv + json, no rendering |
| `--relocate` | find items whose folder moved and update them in place |
| `--relocate --prune` | also drop entries whose files are truly gone |
| `--diagnose` | what's downloaded, what's cloud-only, is rendering working |
| `--compare-engines 6` | render 6 small models with both engines, side by side |
| `--sample 8` | preview 8 thumbnails into `_render_test/` |
| `--jobs N` | render workers (default: about half your cores) |
| `--max-mb N` | skip projects bigger than this (default 1500) |
| `--engine shell\|mesh` | thumbnail engine (see below) |

---

## Two thumbnail engines

**`mesh`** (reliable, default in the menu) renders the geometry with trimesh + matplotlib. A couple of seconds per model. Install `fast-simplification` for noticeably cleaner output on high-poly sculpts.

**`shell`** (Windows only) asks Windows for the same thumbnail Explorer shows. Near-instant when it works — but it depends entirely on whether you have an STL thumbnail handler installed, and on some machines it fails for every file. The tool detects that and falls back to `mesh` automatically. It also rejects the case where Windows hands back the same generic icon for everything.

Use `--compare-engines 6` to see which one your machine actually produces better results with.

## Cloud storage (Dropbox, OneDrive, Google Drive)

Cataloging works on **metadata only** — filenames and sizes — so it never forces your cloud provider to download anything.

Thumbnails are different: rendering needs the actual bytes. Files that are still online-only are detected via their Windows attributes, skipped without being touched, and reported by `--diagnose`. Mark the folders "available offline", let them sync, then run option 6 again to fill in the gaps. It's resumable — interrupt it whenever you like.

## When you reorganize

Items are identified by a fingerprint of their contents (the set of model filenames, count, and total size), not by path alone. So if you move a folder, scan its new location and run `--relocate`: it recognizes the same kit in a new place, updates the path, and **keeps the existing thumbnail** rather than re-rendering. Ambiguous matches are left alone rather than guessed at.

## Configuring the rules

`rules.json` holds every keyword pattern used for categories, factions, types, and sources. It ships tuned for Warhammer 40k because that's what it was built against — set `"factions": {}` and rewrite `categories` if you catalog something else entirely. Patterns are ordinary case-insensitive regular expressions matched against each item's path.

---

## Privacy

**Never commit your catalog.** `catalog.json` and `catalog.csv` contain absolute paths to everything you own. `gallery.html` is worse — it embeds the *entire* catalog inline as JSON, so a single file gives away your whole collection and folder structure. And `thumbnails/` holds rendered images of models you may have licensed for personal use only.

The included `.gitignore` excludes all of it, plus `sources.txt` and `backups/`. Publish the tool, not your library. If you fork this and add features, check `git status` before your first commit.

## Known limitations

- No per-render timeout on Windows (`SIGALRM` is Unix-only). A corrupt or gigantic mesh can stall a worker; `--max-mb` limits the blast radius.
- `.step`/`.stp` files are catalogued but not thumbnailed — no CAD converter is bundled.
- `.zip` archives are indexed by name only. Nothing is extracted.
- Classification is keyword-based, so it is approximate. Fix it by editing `rules.json`.
- `LIBRARY.bat` is Windows-only. Everything it does is available as a command elsewhere.

## License

MIT — see [LICENSE](LICENSE).
