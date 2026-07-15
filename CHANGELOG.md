# Changelog

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
