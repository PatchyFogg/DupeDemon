# Changelog

## 1.10.0 — 2026-08-02
- **Split layout**: main window is now a resizable 30/70 split — folders
  list on the left (finally sized to show more than 3 rows), photos on
  the right. Drag the sash to adjust

## 1.9.1 — 2026-08-01
- Main window now opens larger by default (98% × 96% of screen)
- **Clear Cache** button on the main toolbar (shows current cache size).
  Preferences still has one too; both stay in sync

## 1.9.0 — 2026-07-30
- **Confidence % per group** — each group header now shows how similar its
  members are (100% for exact/byte-identical matches; the actual worst-case
  pairwise similarity for perceptual matches). Also shown in the Compare
  window title and footer
- **Move Selected to…** — new button beside Trash: pick any folder and
  the selected files are moved (not trashed), with collision-safe renaming
  (`name (2).jpg`, etc.). A safer alternative for "review pile" workflows
- **Compare window sizing fixed** — every column is now a uniform
  fixed-size cell, so mixed portrait/landscape images no longer make the
  columns ragged; window is capped to screen and centered
- **Removed**: Export CSV (unused)

## 1.8.0 — 2026-07-27
- **Quick Look**: click any result thumbnail to preview it in the macOS
  Quick Look panel (double-click still opens in the default app)
- **Compare…** button on every group — opens a side-by-side window with
  larger images, dimensions, size, date, and path per file; the keeper
  (per the current rule) is highlighted in green; per-image Quick Look /
  Open / Reveal buttons

## 1.7.0 — 2026-07-25
- **Drag & drop**: drop folders (or files — their parent folder is added)
  from Finder anywhere on the window. Folder list highlights while dragging;
  status bar reports what was added or skipped

## 1.6.2 — 2026-07-15
- Matching status now warns "this can take several minutes" so long matches
  on large libraries don't read as a freeze

## 1.6.1 — 2026-07-15
- Fixed similar-image scans appearing frozen after "Analyzed…": the matching
  phase now reports live status ("Matching similar images… X%"), collapses
  identical hash signatures before comparing (dupe-heavy libraries match in
  milliseconds instead of minutes), and honors Stop throughout

## 1.6.0 — 2026-07-15
- Fixed progressive slowdown/memory growth on large scans (app previously got
  slower the more results it processed, until unusable):
  - Results are paged — 40 groups at a time with a "Show More Groups" button,
    so widget and thumbnail memory stays bounded no matter the library size
  - Scroll-region updates are coalesced instead of recalculated per widget
  - Thumbnails decode JPEGs at reduced resolution (much faster, less RAM)
  - Removed the matcher's seen-pair set, which could balloon to GBs on
    similar-heavy libraries
- Selection now covers all groups, rendered or not: Auto-Select + Trash works
  on the entire result set without paging through it; the keep-one-per-group
  guard applies to unrendered groups too

## 1.5.0 — 2026-07-15
- Persistent hash cache (SQLite, Application Support): re-scans skip files
  whose size and modification time are unchanged — warm re-scans of big
  libraries read almost nothing. Edited/replaced files re-hash automatically.
- Cache file is owner-only (0600); Preferences has a "Use hash cache" toggle
  and a "Clear Cache" button showing its size; the uninstaller removes it

## 1.4.1 — 2026-07-15
- Clicking the Dock icon now restores the window after minimizing
- Stop button also stops results processing (keeps the groups already shown,
  status reports "Stopped — showing first N of M groups")
- Progress bar shows overall progress: it no longer resets while re-rendering
  results after a delete

## 1.4.0 — 2026-07-15
- Built-in uninstaller: Preferences → "Uninstall Dupe Demon…" lists every file
  the app put on your system (the .app, Application Support, preferences,
  saved state, caches, pre-rename leftovers), moves it all to the Trash
  (restorable), and quits

## 1.3.5 — 2026-07-15
- Full Disk Access prompt and Preferences window now open centered over the
  main window

## 1.3.4 — 2026-07-15
- Launch check for Full Disk Access: if missing, a dialog explains which
  features need it (shell box, protected folders) and offers to open the
  System Settings pane — with Later / Don't Ask Again options

## 1.3.3 — 2026-07-15
- One-time warning before the first shell-box command: it runs real terminal
  commands with no undo (rm doesn't use the Trash); Cancel blocks the command

## 1.3.2 — 2026-07-15
- Shell box now shows a greyed "enter shell commands…" placeholder so its
  purpose is obvious; clears on click, never executes as a command

## 1.3.1 — 2026-07-15
- Window now opens at ~90% of the screen, centered (was a fixed 1080×760)

## 1.3.0 — 2026-07-15
- Renamed to **Dupe Demon** (was Duplicate Photo Finder), new icon, versioned
  app bundle; existing preferences migrate automatically
- Progress bar now tracks results processing ("Processing X of Y groups…"),
  not just image analysis — including after trashing files
- Status text left-justified next to the progress bar
- Removed the confirmation dialog on Move to Trash (selection is the confirmation;
  the keep-one-copy-per-group guard remains)
- Added shell box next to the Trash button for quick commands
  (e.g. `rm -rf ~/.Trash/*`); multi-line output opens in a window

## 1.1 — 2026-07-14
- Automatic duplicate marking after every scan, keeping the best copy
  (new "Best quality" rule: resolution → file size → oldest original)
- Keep-rule choice persists across launches; preference toggle for auto-marking
- Exact-duplicate scans now show image dimensions too

## 1.0 — 2026-07-14
- Initial release: similar-image (perceptual dHash) and exact (SHA-256) scans,
  thumbnail group review, auto-select rules, Trash via Finder, CSV export,
  preferences window, self-test suite, DMG packaging
