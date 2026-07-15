# Changelog

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
