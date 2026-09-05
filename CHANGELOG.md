# Changelog

## 1.15.0 — 2026-09-05
- **New:** Plugin architecture. Drop a single `.py` file into
  `~/Library/Application Support/DupeDemon/plugins/` that exports a
  `register(app)` function and it loads automatically on next launch.
  **Plugins → Manage Plugins…** lists every installed plugin (name,
  description, enable/disable toggle) without ever executing a disabled
  plugin's code — metadata is read via static parsing, not import. Core
  app behavior is untouched by this; it's purely additive.

## 1.14.2 — 2026-09-01
- **Fix:** Move to Trash never showed a permission prompt at all, and
  Dupe Demon never appeared in System Settings → Privacy & Security →
  Automation to grant it manually. Two causes:
  - The app's Info.plist was missing `NSAppleEventsUsageDescription`.
    Without that key macOS won't prompt for Apple Events at all (added
    via the PyInstaller spec's `info_plist`) — necessary but not
    sufficient on its own.
  - The build is ad-hoc signed (no paid Developer ID), and that
    signature's hash changes on every rebuild. macOS TCC ties Automation
    grants to that hash, and after enough rebuilds during active
    development it stopped even offering the prompt for this bundle ID —
    not denied, just never asked. `build.sh` now strips the signature
    after PyInstaller applies it, matching how the app already needs
    "right-click → Open" to launch unnotarized — no change in what
    Gatekeeper already required.
  - Removed the app's own explanatory permission dialog — it's no longer
    needed now that the real fix makes the native macOS prompt appear.

## 1.14.1 — 2026-09-01
- **Fix:** the progress pie kept spinning forever after Stop (mid-render)
  and after a failed/partial Trash operation. Its spin animation only
  ever stopped when a caller explicitly set value to 0 — neither of
  those two paths did that, so it just kept rotating indefinitely with
  no operation actually running behind it. Added a proper
  `stop_spinning()` that freezes the pie at its current value (instead
  of misleadingly resetting it to empty) and wired it into both paths.

## 1.14.0 — 2026-09-01
- **Fix:** Move to Trash ran synchronously on the main thread. Finder's
  AppleScript delete is genuinely slow at scale (~1s per 30 files) —
  for a large selection (thousands of files, chunked 100 at a time)
  this blocked the UI for minutes with a single static "Moving files…"
  message the whole time. Looked exactly like a hang. Now runs in the
  background with live "Moving X of Y files…" progress, matching how
  scanning already works; a failure partway through still correctly
  keeps whatever succeeded before it.
- "Move Selected to Trash" renamed to **Delete** (button and Edit menu).

## 1.13.9 — 2026-09-01
- Add Folder's Finder window no longer tries to force a size via
  AppleScript `set bounds` (it didn't reliably stick — Finder kept
  using its own last-remembered window size regardless). Simplified
  to the same plain `open` call used elsewhere in the app; whatever
  size Finder opens at is just normal Finder behavior.

## 1.13.8 — 2026-09-01
- Add Folder's Finder window is now medium-sized (600×450) instead of
  900×700 — the oversized window looked like something had crashed
  rather than a window quietly waiting for a drag.

## 1.13.7 — 2026-09-01
- **Fix:** a large scan (thousands of duplicate groups) rendered every
  page of results immediately instead of only the ones you'd scrolled
  to — a regression from 1.13.5's page-boundary fix that made rendering
  slow again exactly for the libraries that most needed it fast. Pages
  beyond the first now load only when you actually scroll near the
  bottom, like before.
- **Fix:** "Move Selected to Trash" showed a raw Finder-automation
  error when macOS blocked the request (permission not granted, or
  denied earlier). Now detects that specific failure and shows an
  actionable dialog with a button straight to System Settings →
  Privacy & Security → Automation.
- **Add Folder** now opens a large Finder window instead of Tk's
  picker (which can only select one folder per dialog on any
  platform). Select multiple folders in Finder with ⌘-click and drag
  them onto the folder list — status bar shows the instructions.

## 1.13.6 — 2026-09-01
- **Fix:** progress pie rendered blank/gray at exactly 100% instead of
  full green — Tk's `create_arc` silently draws nothing for an exact
  360° extent. Now draws a full circle instead of a degenerate arc.
- **Fix:** exact-duplicate mode had no status message between "Analyzed
  N images…" finishing and results rendering starting — on a large
  library this was a silent gap (worse combined with the pie bug above).
  Added "Comparing N files…" and "Reading image details…" status updates.
- **Fix:** Clear Cache (toolbar) required confirming a modal dialog with
  no strong visual cue, which read as the click doing nothing. Cache
  clearing is fully safe (just triggers a re-hash later), so it now
  clears instantly like the Preferences window's Clear Cache already did.
  Both buttons already disable themselves when the cache is empty.
- Add Folder now loops the picker after each pick instead of closing
  after one — Tk's directory chooser has no native multi-select on any
  platform, so this is the standard workaround. Cancel ends the run.

## 1.13.5 — 2026-09-01
- **Fix:** on libraries with more than 40 groups (results are paginated
  40 at a time), the progress pie and "Processing X of Y groups…" text
  reset at every page boundary instead of tracking the true total. On a
  library with hundreds/thousands of groups this meant the pie appeared
  to freeze dozens of times per scan while the count kept climbing —
  looked exactly like a hang. Progress now tracks the real total across
  every page.

## 1.13.4 — 2026-09-01
- **Fix:** scans could appear to hang partway through and stop responding
  to clicks on real photo libraries. Two causes removed:
  - The "Worker threads" preference is gone. Thread count is now sized
    automatically from CPU cores and system memory (capped at 64) — the
    old slider could reach 1,280 real OS threads at its max setting
    (128 × the 10x multiplier added in 1.13.1), and that many threads is
    pure scheduling overhead for CPU-bound hashing work
  - The progress bar/status label update was replaying every queued
    "processed 1 file" message as its own widget redraw. A big library
    could queue thousands of these between UI ticks, so the main thread
    spent long stretches redrawing instead of handling clicks. Now the
    whole queue drains per tick and only the latest value is applied
- Pie progress indicator is green instead of blue, and stays full/green
  when a scan finishes (including "no duplicates found") instead of
  resetting to empty
- README: macOS 10.13+ requirement, Homebrew-not-required clarification

## 1.13.3 — 2026-08-18
- Native macOS menu bar (File/Edit/Help) with in-app Help window
- Uninstall wired into the File menu
- build.sh stamps version into Info.plist; DMG name reads __version__
- Matte black "Add folders here" placeholder in the folder list when empty
- D&D white overlay now shows "Drop folders here" text
- Placeholder auto-hides when folders are added, reappears when all removed

## 1.13.2 — 2026-08-18
- "Clear cache on exit" option in Preferences (on by default)

## 1.13.1 — 2026-08-18
- Worker threads now scale 10× (1 worker = 10 threads)
- D&D white overlay scoped to receiving panel only (Folders or Results)

## 1.13.0 — 2026-08-18
- Spinning pie chart replaces progress bar
- Results auto-load on scroll (no more "Show More" button)
- Folder toggle now cycles Source → Reference → Off; Off folders
  are skipped entirely during scans
- Per-group Select All / Deselect All buttons
- Per-group Skip button removes group from results without touching files
- "Remove" button renamed to "Remove from List" for clarity
- Scan-with-no-folders warning moved from modal dialog to status bar
- Drag-and-drop visual feedback: window shows a white overlay while
  files are being dragged over it, reverts to normal on drop

## 1.12.1 — 2026-08-04
- Worker thread cap raised from 32 to 128 (Preferences → Performance).
  Handy on machines with plenty of RAM

## 1.12.0 — 2026-08-04
- **Source / Reference folders** (dupeGuru-style):
  - Each folder in the sidebar has a Type — **Source** (files can be
    deleted) or **Reference** (files are protected, never deleted)
  - New "Toggle Source ↔ Reference" button; double-click any folder row
    to flip its type
  - Files in Reference folders always outrank Source files for "keeper"
    selection; their checkbox is disabled and shows a 🔒 badge
  - Groups where every file is in a Reference folder are hidden
    (nothing actionable)
  - Compare window frames Reference files in blue with a
    "Reference — always kept" label
  - Drag-and-dropped folders default to Source; toggle after if needed

## 1.11.0 — 2026-08-03
- **Sort By** dropdown at the top of the results pane:
  - Reclaimable size (largest) — default
  - Confidence (weakest first) — review shaky matches first
  - Confidence (strongest first)
  - Group size (most files)
  - Total size (largest)
  Re-sorting re-renders in place and preserves your current selection

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
