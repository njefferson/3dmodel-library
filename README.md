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

Both the scan and the thumbnail run report progress every couple of seconds — on a timer, so they keep talking even while one enormous mesh is rendering — with a time-left estimate that accounts for big meshes taking longer than small ones.

Strictly read-only on your model files. The only things it writes are its own catalog, gallery, and thumbnails.

## Why not just use Explorer

Explorer shows you one folder at a time and can't tell you that the Warhound titan you're hunting is in `- Paid\SomeCreator\April releases-20200426T063026Z-001\`. This gives you every project in one grid, with pictures, filters, and a copy-the-filename button.

---

## Requirements

- Python 3.9+
- A menu is provided for every platform: double-click **`LIBRARY.bat`** on Windows, or run **`./library.sh`** on macOS and Linux. The Python script itself runs anywhere, with or without either.

## Install

```bash
git clone https://github.com/njefferson/3dmodel-library.git
cd 3dmodel-library
pip install -r requirements.txt
```

That installs everything, including the STEP reader and the mesh simplifier — both matter, and neither announces itself if missing except through worse-looking thumbnails.

## Quick start

Tell it where your models live:

```bash
python update_catalog.py --add-source "D:\path\to\your\models"
python update_catalog.py --rescan-all
```

That scans, classifies, renders thumbnails, and writes `gallery.html`. Open it in any browser.

You can skip the commands entirely — double-click **`LIBRARY.bat`** on Windows, or run **`./library.sh`** on macOS and Linux. Both are the same numbered menu, and every option runs the identical command, so everything below describes both.

---

## The menu

`LIBRARY.bat` on Windows, `./library.sh` on macOS and Linux — same options, same commands:

```
BROWSE           1  gallery          2  spreadsheet
CHECK (safe)     3  status           4  scanned folders   5  test 6 thumbnails
                14  what the rules missed   15  find duplicates
PICTURES         6  make missing     7  redo all
FOLDERS/FILES    8  add folder       9  stop scanning    10  find new files
                11  fix moved       12  remove dead entries
                13  re-apply rules
OTHER            R  refresh          B  back up   U  undo   H  help
```

Anything that writes the catalog copies it into `backups/` first — including the
long picture runs, which are the ones people interrupt. `U` puts the newest copy
back.

## Commands

**Folders to scan**

- `--sources` — list them, with a live OK/MISSING check and an item count each
- `--add-source PATH` — add one
- `--remove-source PATH` — stop scanning one (entries and files both stay)

**Building the catalog**

- `"D:\some\folder"` — scan a folder and fold it into the library
- `--rescan-all` — rescan every listed folder, adding what's new
- `--rebuild-views` — regenerate gallery, CSV and JSON from what's already known; no rendering
- `--reclassify` — re-apply `rules.json` to everything already catalogued
- `--reclassify --dry-run` — show exactly what that would change, and write nothing
- `--unmatched 25` — list what no rule recognised, with the part names inside, to help write patterns
- `--duplicates` — find kits you hold more than one copy of, and write `potential_duplicates.csv`

**Pictures**

- `--thumbs-only` — make thumbnails only for items that lack one
- `--thumbs-only --force` — rebuild every thumbnail
- `--sample 8` — preview 8 thumbnails into `_render_test/`, changing nothing else
- `--compare-engines 6` — render 6 small models with both engines, side by side
- `--jobs N` — render workers (default: about half your cores)
- `--max-mb N` — skip projects bigger than this (default 1500)
- `--timeout N` — give up on any single model after N seconds and move on (default 300; `0` = no limit)
- `--engine shell|mesh` — thumbnail engine (see below)
- `--style NAME` — colour scheme for rendered thumbnails: `slate` (default), `paper`, `blueprint`, `bronze`, `mono`, `resin`

**When things move or go wrong**

- `--relocate` — find items whose folder moved and update them in place
- `--relocate --prune` — also drop entries whose files are truly gone
- `--diagnose` — what's downloaded, what's cloud-only, what failed and why
- `--backup` — copy `catalog.json` into `backups/` right now
- `--restore-backup` — put the newest backup back (the file it replaces is kept too)

---

## Two thumbnail engines

**`mesh`** (reliable, default in the menu) renders the geometry with trimesh + matplotlib. A couple of seconds per model.

`fast-simplification` is **required**, not optional, despite what its old comment said: trimesh's own `simplify_quadric_decimation()` is built on top of it, so without it *both* reduction paths fail and a high-poly sculpt falls back to keeping every Nth triangle — which scatters the surface into loose specks instead of simplifying it. If you see speckled thumbnails, that is why.

**`shell`** (Windows only) asks Windows for the same thumbnail Explorer shows. Near-instant when it works — but it depends entirely on whether you have an STL thumbnail handler installed, and on some machines it fails for every file. The tool detects that and falls back to `mesh` automatically. It also rejects the case where Windows hands back the same generic icon for everything.

Use `--compare-engines 6` to see which one your machine actually produces better results with.

## Duplicates

Kits get downloaded twice. Because every item already carries a fingerprint of its contents — the model filenames, how many, and the total size — finding the repeats costs one pass over the catalog, with no folder walked and no file opened:

```bash
python update_catalog.py --duplicates
```

It reports the biggest offenders, says how much space keeping one of each would give back, and writes the full list to `potential_duplicates.csv`. A second section lists kits with the *same filenames but different sizes* — the same model re-exported or re-downloaded at another quality.

It **deletes nothing** and never touches a model folder. Deciding which copy to keep is yours to do in Explorer. Note that the CSV holds absolute paths; it is gitignored, and should stay that way.

## Inside `.zip` archives

Archives used to be indexed by filename alone, so every one was a blank card. Now, once an archive is downloaded (never while it is still a cloud placeholder), the tool reads its index and:

- records how many model files are inside, and in what formats
- uses an artist's preview image from inside the archive as the card, if there is one
- otherwise renders the largest model inside it, if that model is under 96 MB

Nothing is ever extracted to disk. Single members are read into memory under a size cap, the archive is opened read-only, and a corrupt or encrypted one is reported rather than skipped in silence.

## Why an item has no picture

Every project carries a status saying what happened to its thumbnail, and it is
in the CSV, in `--diagnose`, on the card in the gallery, and in the filter at the
top of the page. Nothing is skipped silently.

- **reused** — the artist shipped a preview image and it was used as-is
- **shell** / **mesh** — rendered, by the Windows handler or the Python renderer
- **existing** — a picture from an earlier run was already on disk
- **cloud_only** — the files are still online-only placeholders; nothing was opened
- **missing** — the files are not at the recorded path any more
- **too_big** — over `--max-mb`, deliberately deferred
- **unsupported** — a CAD file with no reader installed; the message says what to install
- **failed** — the renderer ran and produced nothing; the reason is recorded alongside it
- **timeout** — the render ran past `--timeout` and the worker was killed

## How thumbnails look

Surfaces are shaded from averaged vertex normals rather than per-face ones, so a curved model reads as curved instead of as a bag of flat plates. (matplotlib paints each polygon a single colour and cannot interpolate across one, so the shading *field* is what has to vary — true Gouraud shading is not available here at any face count.) Winding is made consistent first, because downloaded STLs are full of reversed faces and two opposite normals meeting at a vertex cancel out.

**Multi-part kits.** Most downloaded kits are laid out on a print plate — every part authored where it sits on the bed, nowhere near the others. Drawing all of them together gives a picture of scattered specks rather than of the model. When a kit's whole envelope is more than 2.5x its largest single part, the parts are taken to be laid out rather than assembled and the largest part is drawn on its own, with the reason recorded. Kits that genuinely fit together are drawn whole. Change the threshold with `"render_spread_limit"` in `rules.json`.

Six colour schemes ship: `slate` (default), `paper`, `blueprint`, `bronze`, `mono`, `resin`. Try one before committing to a full re-render:

```bash
python update_catalog.py --sample 6 --style blueprint
```

Then apply it with `--thumbs-only --force --style blueprint`. Set `"preset"` under `thumbnail_style` in `rules.json` to make a choice stick, and override `background`, `model`, `ambient`, `specular`, `rim` or `rim_color` individually there if you want something of your own.

Note that changing the look means re-rendering everything, which is the slowest thing this tool does.

## Cloud storage (Dropbox, OneDrive, Google Drive)

Cataloging works on **metadata only** — filenames and sizes — so it never forces your cloud provider to download anything.

Thumbnails are different: rendering needs the actual bytes. Files that are still online-only are detected via their Windows attributes, skipped without being touched, and reported by `--diagnose`. This is judged per file, not per project: a kit with eight parts downloaded and three still in the cloud renders from the eight, and the other three are not so much as named to the renderer. Mark the folders "available offline", let them sync, then run option 6 again to fill in the gaps. It's resumable — interrupt it whenever you like.

## When you reorganize

Items are identified by a fingerprint of their contents (the set of model filenames, count, and total size), not by path alone. So if you move a folder, scan its new location and run `--relocate`: it recognizes the same kit in a new place, updates the path, and **keeps the existing thumbnail** rather than re-rendering. Ambiguous matches are left alone rather than guessed at.

## Configuring the rules

`rules.json` holds every keyword pattern used for categories, factions, types, and sources. It ships tuned for Warhammer 40k because that's what it was built against — set `"factions": {}` and rewrite `categories` if you catalog something else entirely. Patterns are ordinary case-insensitive regular expressions.

**What gets matched.** The folder path and name first. If that matches nothing, the names of the model files *inside* the folder are tried as a fallback — so a kit in a folder called `KitA` full of `warhound_titan_*.stl` is still found, while a folder that already says what it is cannot be overruled by one part called `wall_mount.stl`. Set `"match_filenames": false` for path-only matching.

To see what the rules are missing:

```bash
python update_catalog.py --unmatched 25
```

That lists the largest items no rule recognised, with the filenames inside them. It prints folder and file names only, never absolute paths, so it is safe to paste somewhere.

Edit it, then apply it to what you already have:

```bash
python update_catalog.py --reclassify --dry-run   # what would change
python update_catalog.py --reclassify             # do it
```

That reads the catalog and rewrites the labels in it. No folder is walked, no model file is opened, no thumbnail is re-made. Put your own patterns in `rules.local.json` instead if you would rather not edit the shipped file — it wins where it exists, and it is gitignored.

## Tests

```bash
python -m unittest discover -s tests -v
```

Builds a catalog from a fixture of tiny STLs in a temp folder and checks the outputs are valid, that no project is ever skipped without a recorded reason, that an interrupted write cannot damage `catalog.json`, that online-only files are never handed to the renderer, and that relocate and prune do what they claim. No third-party packages and no Windows needed — the renderer is not exercised, only everything around it.

Set `LIBRARY_DIR` to keep the catalog, thumbnails and backups somewhere other than next to the script; that is how the tests stay clear of a real library.

---

## Privacy

**Never commit your catalog.** `catalog.json` and `catalog.csv` contain absolute paths to everything you own. `gallery.html` is worse — it embeds the *entire* catalog inline as JSON, so a single file gives away your whole collection and folder structure. And `thumbnails/` holds rendered images of models you may have licensed for personal use only.

The included `.gitignore` excludes all of it, plus `sources.txt` and `backups/`. Publish the tool, not your library. If you fork this and add features, check `git status` before your first commit.

## Known limitations

- Killing a stuck render means killing the worker pool, so anything else being rendered at that moment is restarted from scratch. Rare, and the alternative was one bad model stalling the whole run.
- `.step`/`.stp` thumbnails come from `cascadio`, which `requirements.txt` installs. CAD files are boundary representation rather than triangles, so something has to tessellate them; that package is OpenCascade's reader as a ~0.5 MB wheel, not a full CAD install, and it converts a 22 KB STEP in about 0.01s. On the rare platform with no wheel for it (32-bit, older ARM) pip skips it, and `.step` files then report themselves as `unsupported` with `pip install cascadio` in the message.
- `.rar` and `.7z` archives are indexed by name only — nothing here can read them.
- Classification is keyword-based, so it is approximate. It reads the folder path first and the filenames inside as a fallback; it never opens a file to see what the model actually is. Edit `rules.json`, check with `--unmatched`, and apply with `--reclassify`.
- The two menus are kept in step by a test that checks both against the real argument parser: neither can call a flag that does not exist, and neither can gain an option the other lacks.

## License

MIT — see [LICENSE](LICENSE).
