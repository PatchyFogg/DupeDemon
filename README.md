# Dupe Demon

<img src="assets/icon.png" width="128" align="right" alt="Dupe Demon icon">

**A duplicate photo finder for macOS.** Dupe Demon hunts down exact and
near-duplicate photos — resized, re-saved, re-compressed, even rotated copies —
shows them side by side, marks the junk automatically, and keeps the best one.

Version **1.13.3** · Python 3 / Tkinter / Pillow · macOS 10.13+

## Install

Grab `Dupe Demon <version>.dmg` from [Releases](../../releases), open it, and drag
**Dupe Demon** onto the **Applications** shortcut. First launch: right-click →
**Open** (the app is not notarized).

**Requires macOS 10.13 (High Sierra) or later.** The release build is x86_64;
on Apple Silicon Macs it runs under Rosetta 2 (installed automatically on
first launch of an Intel app).

Or run from source — only Pillow is required:

```bash
python3 -m pip install Pillow
python3 dupe_demon.py
```

Optional HEIC/HEIF support for iPhone photos: `python3 -m pip install pillow-heif`

Optional drag-and-drop from Finder: `python3 -m pip install tkinterdnd2`
(already bundled in the release DMG; the app still runs without it, minus DnD)

## Features

- **Two scan engines**
  - *Similar images* — perceptual difference-hash; catches resized, re-encoded,
    edited, and (optionally) rotated copies
  - *Exact duplicates* — byte-for-byte content match (size → partial SHA-256 →
    full SHA-256), fast on large libraries
- **Source / Reference / Off folders** — mark each folder as **Source** (files
  can be deleted), **Reference** (files are protected, never deleted), or
  **Off** (excluded from scans entirely). Double-click a row to cycle.
- **Drag & drop** — drop folders (or files, whose parent folder is added)
  from Finder onto the folder list. "+ Add Folder…" opens a Finder window
  for this too — select several folders at once with ⌘-click and drag them
  all in (Tk's picker can only choose one folder per dialog)
- **Quick Look & Compare** — click any thumbnail for macOS Quick Look
  preview; each group's *Compare* button opens a side-by-side window with
  larger images, metadata, and the keeper highlighted
- **Automatic marking** — after every scan, duplicates are pre-selected, keeping
  the **best copy** of each group (highest resolution → largest file → oldest
  original). Other keep rules: newest, oldest, largest, smallest, first.
- **Per-group controls** — Select All, Deselect All, and Skip per group.
  Skip removes a group from results without touching the files.
- **Sort results** — by reclaimable size, confidence (weakest/strongest first),
  group size, or total size
- **Confidence per group** — each group's header shows how similar its
  members are (100 % for exact matches, actual pairwise % for perceptual
  matches — the worst-case among all pairs)
- **Auto-loading results** — results load automatically as you scroll, no
  paging buttons needed. Memory stays bounded regardless of library size.
- **Safe deletion** — files go to the macOS Trash (recoverable), and the app
  never lets you trash *every* copy in a group.
- **Move Selected to…** — safer alternative to Trash: pick any folder and
  selected files are moved there (with collision-safe renaming) instead of
  trashed. Handy for "review pile" workflows.
- **Persistent hash cache** — (SQLite, Application Support) re-scans skip
  files whose size and modification time are unchanged. Optional auto-clear
  on exit (default on). Clear Cache button on the toolbar shows current size.
- **Built-in shell box** — a `$` field next to the Trash button for quick
  commands. A one-time warning explains this before your first command.
- **Built-in uninstaller** — Preferences → *Uninstall Dupe Demon…* shows every
  file the app owns, moves it all to the Trash, and quits. No orphaned files.

## Preferences (⌘,)

Stored in `~/Library/Application Support/DupeDemon/preferences.json`.

| Setting | What it does |
|---|---|
| Scan type | Similar images (perceptual) or Exact duplicates (byte-for-byte) |
| Similarity threshold | 70–100 %. Higher = stricter; ~90 % catches resized/re-compressed copies |
| Hash precision | Fast 64-bit vs. Precise 256-bit perceptual hash |
| Match rotated copies | Also detects 90°/180°/270° rotations |
| Auto-mark after scan | Pre-selects everything except the best copy per group (default on) |
| Subfolders / hidden files / symlinks | Controls the folder walk |
| Minimum file size | Skip icons and thumbnails |
| File types | Comma-separated extension list |
| Thumbnail size | Display tuning |
| Use hash cache | Persists hashes so re-scans skip unchanged files; Clear Cache button included |
| Clear cache on exit | Wipes the hash cache when the app closes (default on) |

> First time you move files to the Trash, macOS asks permission for the app to
> control Finder — click **Allow**. If that permission was ever denied, fix
> it in System Settings → Privacy & Security → Automation → Dupe Demon →
> Finder.
>
> At launch, Dupe Demon checks for **Full Disk Access** — without it, the shell
> box can't see protected folders like `~/.Trash` (commands there fail
> silently). The prompt can open the right System Settings pane for you.

## Plugins

Dupe Demon supports drop-in plugins without touching core functionality.
A plugin is a single `.py` file placed in
`~/Library/Application Support/DupeDemon/plugins/` that exports a
`register(app)` function; it loads automatically the next time you launch.

**Plugins → Manage Plugins…** lists every installed plugin with a
description and an enable/disable toggle. Disabling a plugin persists
immediately and takes effect on the next launch — its code is never even
imported while disabled. Official plugins ship through pull requests to
this repo rather than being written ad-hoc.

## Privacy

Dupe Demon is **100 % offline**. It makes no network requests, sends no
telemetry, and uploads nothing. All data stays on your Mac:

| What | Where | How to wipe |
|---|---|---|
| Preferences | `~/Library/Application Support/DupeDemon/preferences.json` | Delete the file, or use the built-in uninstaller |
| Hash cache | `~/Library/Application Support/DupeDemon/hash_cache.db` | Clear Cache button, or enable "Clear cache on exit" |
| Installed plugins | `~/Library/Application Support/DupeDemon/plugins/` | Delete individual files, or the folder |
| Plugin enable/disable state | `~/Library/Application Support/DupeDemon/plugin_state.json` | Delete the file, or use the built-in uninstaller |
| Window state | macOS Saved Application State | Reset via System Settings or the uninstaller |

The built-in uninstaller (Preferences → *Uninstall Dupe Demon…*) removes everything in one step.

## Development

```bash
python3 dupe_demon.py --selftest    # engine test suite (no GUI needed)
```

Build the app and DMG:

```bash
chmod +x build.sh && ./build.sh
```

**Homebrew is not required.** The build script looks for a
[python.org](https://www.python.org/downloads/macos/) framework install of
Python 3.9+ first (Homebrew is only used as a fallback if no python.org
install is found), creates a venv, runs PyInstaller, and produces a
drag-install DMG. It also checks the finished bundle with `otool` to confirm
nothing inside it accidentally links back to `/opt/homebrew` or
`/usr/local` — this is a portability check on the *output*, not a build
requirement, and it fails loudly if the check finds a leak.

## License

[MIT](LICENSE)
