# Dupe Demon 😈

<img src="assets/icon.png" width="128" align="right" alt="Dupe Demon icon">

**A duplicate photo finder for macOS.** Dupe Demon hunts down exact and
near-duplicate photos — resized, re-saved, re-compressed, even rotated copies —
shows them side by side, marks the junk automatically, and keeps the best one.

Version **1.3.0** · Python 3 / Tkinter / Pillow · macOS

## Install

Grab `Dupe Demon <version>.dmg` from [Releases](../../releases), open it, and drag
**Dupe Demon** onto the **Applications** shortcut. First launch: right-click →
**Open** (the app is not notarized).

Or run from source — only Pillow is required:

```bash
python3 -m pip install Pillow
python3 dupe_demon.py
```

Optional HEIC/HEIF support for iPhone photos: `python3 -m pip install pillow-heif`

## Features

- **Two scan engines**
  - *Similar images* — perceptual difference-hash; catches resized, re-encoded,
    edited, and (optionally) rotated copies
  - *Exact duplicates* — byte-for-byte content match (size → partial SHA-256 →
    full SHA-256), fast on large libraries
- **Automatic marking** — after every scan, duplicates are pre-selected, keeping
  the **best copy** of each group (highest resolution → largest file → oldest
  original). Other keep rules: newest, oldest, largest, smallest, first.
- **Visual review** — thumbnail groups sorted by reclaimable space, with
  dimensions, size, and date. Double-click to open, click the path to reveal
  in Finder.
- **Safe deletion** — files go to the macOS Trash (recoverable), and the app
  never lets you trash *every* copy in a group.
- **Progress for everything** — the bar tracks image analysis and results
  processing, with live status text.
- **Built-in shell box** — a `$` field next to the Trash button for quick
  commands like `rm -rf ~/.Trash/*` after a cleanup. Multi-line output opens
  in its own window. ⚠️ It runs real terminal commands (a one-time warning
  explains this before your first command — `rm` does not use the Trash).
- **CSV export** of scan results.

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
| Worker threads / thumbnail size | Performance and display tuning |

> First time you move files to the Trash, macOS asks permission for the app to
> control Finder — click **OK**.

## Development

```bash
python3 dupe_demon.py --selftest    # engine test suite (no GUI needed)
```

Build the app and DMG (PyInstaller):

```bash
pyinstaller --windowed --name "Dupe Demon" --icon app_icon.icns \
            --osx-bundle-identifier com.saltz.dupedemon dupe_demon.py
```

Then stage `dist/Dupe Demon.app` with an `/Applications` symlink and wrap it
with `hdiutil create`.

## License

[MIT](LICENSE)
