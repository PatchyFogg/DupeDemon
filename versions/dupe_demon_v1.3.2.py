#!/usr/bin/env python3
"""
Dupe Demon — duplicate photo finder for macOS
=============================================
A dupeGuru-style duplicate photo finder with a Tkinter GUI.

Features
--------
- Exact duplicate detection (file content hashing: size -> partial SHA-256 -> full SHA-256)
- Similar image detection (perceptual difference-hash with adjustable threshold)
- Preferences window (match mode, similarity threshold, hash size, min file size,
  extensions, subfolders, hidden files, thumbnail size, worker threads) persisted to
  ~/Library/Application Support/DuplicatePhotoFinder/preferences.json
- Thumbnail results grouped by duplicate set, with auto-select rules
  (keep newest / oldest / largest / smallest / highest resolution)
- Move selected files to the macOS Trash (via Finder, recoverable)
- Reveal in Finder, open files, export results to CSV

Requires: Python 3.9+, Pillow. HEIC support is enabled automatically if
pillow-heif is installed (pip install pillow-heif).

Run:  python3 dupe_demon.py
Test: python3 dupe_demon.py --selftest
"""

import csv
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from pathlib import Path

try:
    from PIL import Image, ImageTk, ImageOps
except ImportError:
    print("This app requires Pillow.  Install it with:  python3 -m pip install Pillow")
    sys.exit(1)

HEIC_SUPPORTED = False
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    pass

APP_NAME = "Dupe Demon"
__version__ = "1.3.2"
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "DupeDemon"
PREFS_FILE = APP_SUPPORT_DIR / "preferences.json"
_OLD_PREFS_FILE = (Path.home() / "Library" / "Application Support"
                   / "DuplicatePhotoFinder" / "preferences.json")

DEFAULT_EXTENSIONS = ["jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp"]
if HEIC_SUPPORTED:
    DEFAULT_EXTENSIONS += ["heic", "heif"]


# --------------------------------------------------------------------------- #
#  Preferences
# --------------------------------------------------------------------------- #

@dataclass
class Preferences:
    match_mode: str = "similar"            # "exact" or "similar"
    similarity_threshold: int = 92         # percent, 70-100 (100 = identical perceptual hash)
    hash_size: int = 8                     # 8 = fast (64-bit), 16 = precise (256-bit)
    include_subfolders: bool = True
    skip_hidden: bool = True
    follow_symlinks: bool = False
    min_file_size_kb: int = 1
    extensions: list = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    thumbnail_size: int = 128
    worker_threads: int = max(2, (os.cpu_count() or 4) // 2)
    match_rotated: bool = False            # also match 90/180/270-degree rotations
    auto_select_after_scan: bool = True    # mark duplicates automatically, keep the best
    keep_rule: str = "Best quality"

    @classmethod
    def load(cls) -> "Preferences":
        try:
            if not PREFS_FILE.exists() and _OLD_PREFS_FILE.exists():
                # migrate settings from the pre-rename app
                APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
                PREFS_FILE.write_text(_OLD_PREFS_FILE.read_text())
            data = json.loads(PREFS_FILE.read_text())
            prefs = cls()
            for key, value in data.items():
                if hasattr(prefs, key):
                    setattr(prefs, key, value)
            return prefs
        except Exception:
            return cls()

    def save(self) -> None:
        try:
            APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
            PREFS_FILE.write_text(json.dumps(asdict(self), indent=2))
        except OSError as exc:
            print(f"Could not save preferences: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
#  Scan engine (GUI-independent, testable)
# --------------------------------------------------------------------------- #

@dataclass
class FileInfo:
    path: str
    size: int
    mtime: float
    width: int = 0
    height: int = 0


class ScanCancelled(Exception):
    pass


def collect_files(folders, prefs: Preferences, cancel_event=None):
    """Walk the folders and return a list of candidate image Paths."""
    exts = {"." + e.lower().lstrip(".") for e in prefs.extensions}
    min_bytes = prefs.min_file_size_kb * 1024
    seen = set()
    files = []
    for folder in folders:
        root_path = Path(folder)
        if not root_path.is_dir():
            continue
        if prefs.include_subfolders:
            walker = os.walk(root_path, followlinks=prefs.follow_symlinks)
        else:
            walker = [(str(root_path), [], [p.name for p in root_path.iterdir() if p.is_file()])]
        for dirpath, dirnames, filenames in walker:
            if cancel_event is not None and cancel_event.is_set():
                raise ScanCancelled()
            if prefs.skip_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if prefs.skip_hidden and name.startswith("."):
                    continue
                if os.path.splitext(name)[1].lower() not in exts:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    real = os.path.realpath(full)
                    if real in seen:
                        continue
                    stat = os.stat(full)
                    if stat.st_size < min_bytes:
                        continue
                    seen.add(real)
                    files.append(FileInfo(full, stat.st_size, stat.st_mtime))
                except OSError:
                    continue
    return files


def _sha256(path, limit=None, chunk=1 << 20):
    h = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as fh:
        while True:
            size = chunk if remaining is None else min(chunk, remaining)
            if size == 0:
                break
            data = fh.read(size)
            if not data:
                break
            h.update(data)
            if remaining is not None:
                remaining -= len(data)
    return h.hexdigest()


def _dhash(image: Image.Image, hash_size: int) -> int:
    """Difference hash: robust to resizing, format changes, mild edits."""
    img = ImageOps.exif_transpose(image).convert("L").resize(
        (hash_size + 1, hash_size), Image.LANCZOS)
    pixels = img.tobytes()  # mode "L": one byte per pixel
    bits = 0
    idx = 0
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        for col in range(hash_size):
            bits = (bits << 1) | (1 if pixels[offset + col] < pixels[offset + col + 1] else 0)
            idx += 1
    return bits


def compute_perceptual_hashes(path, hash_size, match_rotated=False):
    """Return (hashes, width, height). hashes has 1 entry, or 4 with rotations."""
    with Image.open(path) as img:
        width, height = img.size
        hashes = [_dhash(img, hash_size)]
        if match_rotated:
            for angle in (90, 180, 270):
                hashes.append(_dhash(img.rotate(angle, expand=True), hash_size))
    return hashes, width, height


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def find_exact_duplicates(files, prefs, progress=None, cancel_event=None):
    """Group files with identical content. Returns list of lists of FileInfo."""
    by_size = {}
    for fi in files:
        by_size.setdefault(fi.size, []).append(fi)
    candidates = [group for group in by_size.values() if len(group) > 1]

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()

    total = sum(len(g) for g in candidates)
    done = 0
    groups = []
    with ThreadPoolExecutor(max_workers=prefs.worker_threads) as pool:
        for same_size in candidates:
            check_cancel()
            # Partial hash pre-filter (first 64 KB), then full hash.
            partial = {}
            futures = {pool.submit(_sha256, fi.path, 65536): fi for fi in same_size}
            for fut in as_completed(futures):
                check_cancel()
                fi = futures[fut]
                try:
                    partial.setdefault(fut.result(), []).append(fi)
                except OSError:
                    pass
            for bucket in partial.values():
                if len(bucket) < 2:
                    done += len(bucket)
                    if progress:
                        progress(done, total)
                    continue
                full = {}
                futures = {pool.submit(_sha256, fi.path): fi for fi in bucket}
                for fut in as_completed(futures):
                    check_cancel()
                    fi = futures[fut]
                    done += 1
                    if progress:
                        progress(done, total)
                    try:
                        full.setdefault(fut.result(), []).append(fi)
                    except OSError:
                        pass
                for dupes in full.values():
                    if len(dupes) > 1:
                        groups.append(sorted(dupes, key=lambda f: f.path))
    return groups


def find_similar_images(files, prefs, progress=None, cancel_event=None):
    """Group perceptually similar images. Returns list of lists of FileInfo."""
    hash_bits = prefs.hash_size * prefs.hash_size
    max_distance = round(hash_bits * (100 - prefs.similarity_threshold) / 100)

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()

    entries = []  # (FileInfo, [hashes])
    total = len(files)
    done = 0
    with ThreadPoolExecutor(max_workers=prefs.worker_threads) as pool:
        futures = {
            pool.submit(compute_perceptual_hashes, fi.path, prefs.hash_size,
                        prefs.match_rotated): fi
            for fi in files
        }
        for fut in as_completed(futures):
            check_cancel()
            fi = futures[fut]
            done += 1
            if progress:
                progress(done, total)
            try:
                hashes, width, height = fut.result()
            except Exception:
                continue  # unreadable / corrupt image
            fi.width, fi.height = width, height
            entries.append((fi, hashes))

    check_cancel()

    # Candidate pairing. For small sets compare everything; for large sets use
    # pigeonhole banding: two hashes within `max_distance` bits must share at
    # least one of (max_distance + 1) equal-width bit chunks.
    uf = _UnionFind()
    n = len(entries)
    matched = set()

    def try_match(i, j):
        fi_a, hashes_a = entries[i]
        fi_b, hashes_b = entries[j]
        base = hashes_b[0]
        for h in hashes_a:
            if _hamming(h, base) <= max_distance:
                uf.union(i, j)
                matched.add(i)
                matched.add(j)
                return

    if n <= 2500:
        for i in range(n):
            check_cancel()
            for j in range(i + 1, n):
                try_match(i, j)
    else:
        num_bands = max_distance + 1
        band_width = max(1, hash_bits // num_bands)
        buckets = {}
        for idx, (_fi, hashes) in enumerate(entries):
            h = hashes[0]
            for band in range(num_bands):
                chunk = (h >> (band * band_width)) & ((1 << band_width) - 1)
                buckets.setdefault((band, chunk), []).append(idx)
        seen_pairs = set()
        for bucket in buckets.values():
            check_cancel()
            if len(bucket) < 2:
                continue
            for x in range(len(bucket)):
                for y in range(x + 1, len(bucket)):
                    pair = (bucket[x], bucket[y])
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        try_match(*pair)

    groups_map = {}
    for idx in matched:
        groups_map.setdefault(uf.find(idx), []).append(entries[idx][0])
    return [sorted(g, key=lambda f: f.path) for g in groups_map.values() if len(g) > 1]


# Which file to KEEP in each group; everything else gets marked for removal.
# "Best quality" = highest resolution, then largest file, then oldest (likely
# the original).
KEEP_RULES = {
    "Best quality": lambda f: (f.width * f.height, f.size, -f.mtime),
    "Newest file": lambda f: f.mtime,
    "Oldest file": lambda f: -f.mtime,
    "Largest file": lambda f: f.size,
    "Smallest file": lambda f: -f.size,
    "Highest resolution": lambda f: f.width * f.height,
    "First in group": None,
}


def pick_keeper(group, rule):
    key = KEEP_RULES.get(rule)
    if key is None:
        return group[0]
    return max(group, key=key)


def compute_auto_selection(groups, rule):
    """Return the set of paths to mark for removal (all but the keeper per group)."""
    marked = set()
    for group in groups:
        keeper = pick_keeper(group, rule)
        marked.update(f.path for f in group if f.path != keeper.path)
    return marked


def _fill_dimensions(groups, prefs):
    """Read image dimensions for grouped files (exact mode skips decoding)."""
    def read_dims(fi):
        try:
            with Image.open(fi.path) as img:
                w, h = ImageOps.exif_transpose(img).size
            fi.width, fi.height = w, h
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=prefs.worker_threads) as pool:
        list(pool.map(read_dims, [fi for g in groups for fi in g]))


def run_scan(folders, prefs, progress=None, status=None, cancel_event=None):
    """Full scan pipeline. Returns list of duplicate groups."""
    if status:
        status("Collecting files…")
    files = collect_files(folders, prefs, cancel_event)
    if status:
        status(f"Analyzing {len(files)} images…")
    if prefs.match_mode == "exact":
        groups = find_exact_duplicates(files, prefs, progress, cancel_event)
        _fill_dimensions(groups, prefs)
    else:
        groups = find_similar_images(files, prefs, progress, cancel_event)
    groups.sort(key=lambda g: -sum(f.size for f in g))
    return groups, len(files)


# --------------------------------------------------------------------------- #
#  macOS helpers
# --------------------------------------------------------------------------- #

def move_to_trash(paths):
    """Move files to the macOS Trash via Finder (recoverable). Returns error or None."""
    if not paths:
        return None
    items = ", ".join(
        'POSIX file "{}"'.format(p.replace("\\", "\\\\").replace('"', '\\"'))
        for p in paths
    )
    script = f'tell application "Finder" to delete {{{items}}}'
    result = subprocess.run(["osascript", "-e", script],
                            capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        return result.stderr.strip() or "Finder could not move the files to the Trash."
    return None


def reveal_in_finder(path):
    subprocess.run(["open", "-R", path])


def open_file(path):
    subprocess.run(["open", path])


def human_size(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} TB"


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #

def build_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from datetime import datetime

    class PreferencesWindow(tk.Toplevel):
        def __init__(self, master, prefs: Preferences, on_save):
            super().__init__(master)
            self.title("Preferences")
            self.resizable(False, False)
            self.prefs = prefs
            self.on_save = on_save
            self.transient(master)
            self.grab_set()

            body = ttk.Frame(self, padding=16)
            body.pack(fill="both", expand=True)
            row = 0

            # --- Matching -------------------------------------------------
            ttk.Label(body, text="Matching", font=("", 13, "bold")).grid(
                row=row, column=0, columnspan=3, sticky="w"); row += 1

            self.mode_var = tk.StringVar(value=prefs.match_mode)
            ttk.Label(body, text="Scan type:").grid(row=row, column=0, sticky="w", pady=4)
            mode_frame = ttk.Frame(body)
            mode_frame.grid(row=row, column=1, columnspan=2, sticky="w")
            ttk.Radiobutton(mode_frame, text="Similar images (perceptual)",
                            variable=self.mode_var, value="similar",
                            command=self._update_state).pack(side="left")
            ttk.Radiobutton(mode_frame, text="Exact duplicates (byte-for-byte)",
                            variable=self.mode_var, value="exact",
                            command=self._update_state).pack(side="left", padx=(12, 0))
            row += 1

            self.threshold_var = tk.IntVar(value=prefs.similarity_threshold)
            ttk.Label(body, text="Similarity threshold:").grid(row=row, column=0, sticky="w", pady=4)
            self.threshold_scale = ttk.Scale(
                body, from_=70, to=100, orient="horizontal", length=220,
                command=lambda v: self.threshold_var.set(round(float(v))))
            self.threshold_scale.set(prefs.similarity_threshold)
            self.threshold_scale.grid(row=row, column=1, sticky="we", pady=4)
            self.threshold_label = ttk.Label(body, width=5)
            self.threshold_label.grid(row=row, column=2, sticky="w", padx=(8, 0))
            self.threshold_var.trace_add(
                "write", lambda *_: self.threshold_label.config(
                    text=f"{self.threshold_var.get()}%"))
            self.threshold_label.config(text=f"{prefs.similarity_threshold}%")
            row += 1
            ttk.Label(body, foreground="gray",
                      text="Higher = stricter. 100% only matches near-identical images;\n"
                           "~90% also catches resized/re-saved copies.").grid(
                row=row, column=1, columnspan=2, sticky="w"); row += 1

            self.hash_var = tk.IntVar(value=prefs.hash_size)
            ttk.Label(body, text="Hash precision:").grid(row=row, column=0, sticky="w", pady=4)
            hash_frame = ttk.Frame(body)
            hash_frame.grid(row=row, column=1, columnspan=2, sticky="w")
            self.hash_fast = ttk.Radiobutton(hash_frame, text="Fast (64-bit)",
                                             variable=self.hash_var, value=8)
            self.hash_fast.pack(side="left")
            self.hash_precise = ttk.Radiobutton(hash_frame, text="Precise (256-bit)",
                                                variable=self.hash_var, value=16)
            self.hash_precise.pack(side="left", padx=(12, 0))
            row += 1

            self.rotated_var = tk.BooleanVar(value=prefs.match_rotated)
            self.rotated_check = ttk.Checkbutton(
                body, text="Also match rotated copies (slower)", variable=self.rotated_var)
            self.rotated_check.grid(row=row, column=0, columnspan=3, sticky="w", pady=4)
            row += 1

            ttk.Separator(body).grid(row=row, column=0, columnspan=3, sticky="we", pady=10)
            row += 1

            # --- Scanning -------------------------------------------------
            ttk.Label(body, text="Scanning", font=("", 13, "bold")).grid(
                row=row, column=0, columnspan=3, sticky="w"); row += 1

            self.subfolders_var = tk.BooleanVar(value=prefs.include_subfolders)
            ttk.Checkbutton(body, text="Include subfolders",
                            variable=self.subfolders_var).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=2); row += 1

            self.hidden_var = tk.BooleanVar(value=prefs.skip_hidden)
            ttk.Checkbutton(body, text="Skip hidden files and folders",
                            variable=self.hidden_var).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=2); row += 1

            self.symlink_var = tk.BooleanVar(value=prefs.follow_symlinks)
            ttk.Checkbutton(body, text="Follow symbolic links",
                            variable=self.symlink_var).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=2); row += 1

            ttk.Label(body, text="Minimum file size (KB):").grid(
                row=row, column=0, sticky="w", pady=4)
            self.minsize_var = tk.StringVar(value=str(prefs.min_file_size_kb))
            ttk.Spinbox(body, from_=0, to=1_000_000, textvariable=self.minsize_var,
                        width=10).grid(row=row, column=1, sticky="w"); row += 1

            ttk.Label(body, text="File types:").grid(row=row, column=0, sticky="w", pady=4)
            self.ext_var = tk.StringVar(value=", ".join(prefs.extensions))
            ttk.Entry(body, textvariable=self.ext_var, width=42).grid(
                row=row, column=1, columnspan=2, sticky="we"); row += 1
            heic_note = ("HEIC supported." if HEIC_SUPPORTED else
                         "For HEIC support:  python3 -m pip install pillow-heif")
            ttk.Label(body, text=heic_note, foreground="gray").grid(
                row=row, column=1, columnspan=2, sticky="w"); row += 1

            ttk.Separator(body).grid(row=row, column=0, columnspan=3, sticky="we", pady=10)
            row += 1

            # --- Performance / display ------------------------------------
            ttk.Label(body, text="Results", font=("", 13, "bold")).grid(
                row=row, column=0, columnspan=3, sticky="w"); row += 1

            self.autoselect_var = tk.BooleanVar(value=prefs.auto_select_after_scan)
            ttk.Checkbutton(
                body, text="Automatically mark duplicates after scan (keeps the best copy)",
                variable=self.autoselect_var).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=2); row += 1

            ttk.Separator(body).grid(row=row, column=0, columnspan=3, sticky="we", pady=10)
            row += 1

            ttk.Label(body, text="Performance & Display", font=("", 13, "bold")).grid(
                row=row, column=0, columnspan=3, sticky="w"); row += 1

            ttk.Label(body, text="Worker threads:").grid(row=row, column=0, sticky="w", pady=4)
            self.threads_var = tk.StringVar(value=str(prefs.worker_threads))
            ttk.Spinbox(body, from_=1, to=32, textvariable=self.threads_var,
                        width=10).grid(row=row, column=1, sticky="w"); row += 1

            ttk.Label(body, text="Thumbnail size:").grid(row=row, column=0, sticky="w", pady=4)
            self.thumb_var = tk.IntVar(value=prefs.thumbnail_size)
            thumb_frame = ttk.Frame(body)
            thumb_frame.grid(row=row, column=1, columnspan=2, sticky="w")
            for label, size in (("Small", 96), ("Medium", 128), ("Large", 176)):
                ttk.Radiobutton(thumb_frame, text=label, variable=self.thumb_var,
                                value=size).pack(side="left", padx=(0, 10))
            row += 1

            # --- Buttons ---------------------------------------------------
            buttons = ttk.Frame(body)
            buttons.grid(row=row, column=0, columnspan=3, sticky="e", pady=(16, 0))
            ttk.Button(buttons, text="Restore Defaults",
                       command=self._restore_defaults).pack(side="left", padx=(0, 20))
            ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left")
            save_btn = ttk.Button(buttons, text="Save", command=self._save)
            save_btn.pack(side="left", padx=(8, 0))
            self.bind("<Return>", lambda e: self._save())
            self.bind("<Escape>", lambda e: self.destroy())
            self._update_state()

        def _update_state(self):
            similar = self.mode_var.get() == "similar"
            state = "normal" if similar else "disabled"
            self.threshold_scale.state(["!disabled"] if similar else ["disabled"])
            for widget in (self.hash_fast, self.hash_precise, self.rotated_check):
                widget.configure(state=state)

        def _restore_defaults(self):
            defaults = Preferences()
            self.mode_var.set(defaults.match_mode)
            self.threshold_scale.set(defaults.similarity_threshold)
            self.hash_var.set(defaults.hash_size)
            self.rotated_var.set(defaults.match_rotated)
            self.subfolders_var.set(defaults.include_subfolders)
            self.hidden_var.set(defaults.skip_hidden)
            self.symlink_var.set(defaults.follow_symlinks)
            self.autoselect_var.set(defaults.auto_select_after_scan)
            self.minsize_var.set(str(defaults.min_file_size_kb))
            self.ext_var.set(", ".join(defaults.extensions))
            self.threads_var.set(str(defaults.worker_threads))
            self.thumb_var.set(defaults.thumbnail_size)
            self._update_state()

        def _save(self):
            p = self.prefs
            p.match_mode = self.mode_var.get()
            p.similarity_threshold = self.threshold_var.get()
            p.hash_size = self.hash_var.get()
            p.match_rotated = self.rotated_var.get()
            p.include_subfolders = self.subfolders_var.get()
            p.skip_hidden = self.hidden_var.get()
            p.follow_symlinks = self.symlink_var.get()
            p.auto_select_after_scan = self.autoselect_var.get()
            try:
                p.min_file_size_kb = max(0, int(self.minsize_var.get()))
            except ValueError:
                pass
            try:
                p.worker_threads = min(32, max(1, int(self.threads_var.get())))
            except ValueError:
                pass
            exts = [e.strip().lstrip(".").lower()
                    for e in self.ext_var.get().replace(";", ",").split(",")]
            p.extensions = [e for e in exts if e] or list(DEFAULT_EXTENSIONS)
            p.thumbnail_size = self.thumb_var.get()
            p.save()
            self.on_save()
            self.destroy()

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(f"{APP_NAME}  v{__version__}")
            # open at ~90% of the screen, centered
            screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
            win_w, win_h = int(screen_w * 0.9), int(screen_h * 0.88)
            pos_x = (screen_w - win_w) // 2
            pos_y = max(25, (screen_h - win_h) // 2 - 15)
            self.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
            self.minsize(860, 560)
            self.prefs = Preferences.load()
            self.folders = []
            self.groups = []
            self.check_vars = {}       # path -> BooleanVar
            self.file_by_path = {}
            self._preselect = set()    # paths to mark as groups render
            self._thumb_refs = []
            self._scan_thread = None
            self._cancel_event = threading.Event()
            self._msg_queue = queue.Queue()
            self._render_queue = []
            self._render_total = 0
            self._summary_text = ""

            self._build_widgets()
            self._poll_queue()
            self.createcommand("tk::mac::Quit", self._quit)
            self.bind("<Command-comma>", lambda e: self.open_preferences())
            self.bind("<Command-o>", lambda e: self.add_folder())

        # ---------------- layout ----------------
        def _build_widgets(self):
            toolbar = ttk.Frame(self, padding=(12, 10, 12, 6))
            toolbar.pack(fill="x")
            ttk.Button(toolbar, text="＋ Add Folder…", command=self.add_folder).pack(side="left")
            ttk.Button(toolbar, text="－ Remove", command=self.remove_folder).pack(
                side="left", padx=(6, 0))
            self.scan_btn = ttk.Button(toolbar, text="▶ Scan", command=self.start_scan)
            self.scan_btn.pack(side="left", padx=(18, 0))
            self.stop_btn = ttk.Button(toolbar, text="■ Stop", command=self.stop_scan,
                                       state="disabled")
            self.stop_btn.pack(side="left", padx=(6, 0))
            ttk.Button(toolbar, text="Preferences…  (⌘,)",
                       command=self.open_preferences).pack(side="right")
            self.mode_label = ttk.Label(toolbar, foreground="gray")
            self.mode_label.pack(side="right", padx=(0, 14))
            self._refresh_mode_label()

            folder_frame = ttk.LabelFrame(self, text="Folders to scan", padding=6)
            folder_frame.pack(fill="x", padx=12, pady=(4, 6))
            self.folder_list = tk.Listbox(folder_frame, height=3, activestyle="none")
            self.folder_list.pack(fill="x")

            progress_frame = ttk.Frame(self, padding=(12, 0))
            progress_frame.pack(fill="x")
            self.progress = ttk.Progressbar(progress_frame, mode="determinate")
            self.progress.pack(fill="x", side="left", expand=True)
            self.status_label = ttk.Label(progress_frame, text="Ready.", width=46, anchor="w")
            self.status_label.pack(side="right", padx=(10, 0))

            # results area: scrollable canvas
            results_outer = ttk.LabelFrame(self, text="Results", padding=4)
            results_outer.pack(fill="both", expand=True, padx=12, pady=6)
            self.canvas = tk.Canvas(results_outer, highlightthickness=0)
            scrollbar = ttk.Scrollbar(results_outer, orient="vertical",
                                      command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            self.canvas.pack(side="left", fill="both", expand=True)
            self.results_frame = ttk.Frame(self.canvas)
            self._canvas_window = self.canvas.create_window(
                (0, 0), window=self.results_frame, anchor="nw")
            self.results_frame.bind(
                "<Configure>",
                lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
            self.canvas.bind(
                "<Configure>",
                lambda e: self.canvas.itemconfigure(self._canvas_window, width=e.width))
            self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

            # bottom action bar
            actions = ttk.Frame(self, padding=(12, 4, 12, 12))
            actions.pack(fill="x")
            ttk.Label(actions, text="Auto-select all except:").pack(side="left")
            initial_rule = (self.prefs.keep_rule
                            if self.prefs.keep_rule in KEEP_RULES else "Best quality")
            self.keep_var = tk.StringVar(value=initial_rule)
            keep_menu = ttk.Combobox(
                actions, textvariable=self.keep_var, state="readonly", width=18,
                values=list(KEEP_RULES))
            keep_menu.pack(side="left", padx=(6, 4))
            keep_menu.bind("<<ComboboxSelected>>", self._keep_rule_changed)
            ttk.Button(actions, text="Auto-Select", command=self.auto_select).pack(side="left")
            ttk.Button(actions, text="Clear Selection", command=self.clear_selection).pack(
                side="left", padx=(6, 0))
            ttk.Button(actions, text="Export CSV…", command=self.export_csv).pack(
                side="right")
            self.trash_btn = ttk.Button(actions, text="🗑 Move Selected to Trash",
                                        command=self.trash_selected)
            self.trash_btn.pack(side="right", padx=(0, 10))
            shell_frame = ttk.Frame(actions)
            shell_frame.pack(side="right", padx=(0, 12))
            ttk.Label(shell_frame, text="$").pack(side="left")
            self.shell_var = tk.StringVar()
            self.shell_entry = ttk.Entry(shell_frame, textvariable=self.shell_var,
                                         width=26)
            self.shell_entry.pack(side="left", padx=(4, 0))
            self.shell_entry.bind("<Return>", self.run_shell_command)
            self._shell_placeholder = "enter shell commands…"
            self._shell_placeholder_on = False
            self.shell_entry.bind("<FocusIn>", self._shell_focus_in)
            self.shell_entry.bind("<FocusOut>", self._shell_focus_out)
            self._show_shell_placeholder()
            self.selection_label = ttk.Label(actions, foreground="gray")
            self.selection_label.pack(side="right", padx=(0, 14))

        def _refresh_mode_label(self):
            if self.prefs.match_mode == "exact":
                self.mode_label.config(text="Mode: Exact duplicates")
            else:
                self.mode_label.config(
                    text=f"Mode: Similar images ≥ {self.prefs.similarity_threshold}%")

        def _on_mousewheel(self, event):
            self.canvas.yview_scroll(-1 * event.delta, "units")

        # ---------------- folder management ----------------
        def add_folder(self):
            folder = filedialog.askdirectory(title="Choose a folder to scan")
            if folder and folder not in self.folders:
                self.folders.append(folder)
                self.folder_list.insert("end", folder)

        def remove_folder(self):
            selection = self.folder_list.curselection()
            for index in reversed(selection):
                self.folders.pop(index)
                self.folder_list.delete(index)

        # ---------------- preferences ----------------
        def open_preferences(self):
            PreferencesWindow(self, self.prefs, on_save=self._refresh_mode_label)

        # ---------------- scanning ----------------
        def start_scan(self):
            if self._scan_thread and self._scan_thread.is_alive():
                return
            if not self.folders:
                messagebox.showinfo(APP_NAME, "Add at least one folder to scan.")
                return
            self._clear_results()
            self._cancel_event.clear()
            self.scan_btn.state(["disabled"])
            self.stop_btn.state(["!disabled"])
            self.progress.config(value=0, maximum=100)
            self.status_label.config(text="Starting…")
            prefs_snapshot = Preferences(**asdict(self.prefs))
            folders = list(self.folders)

            def worker():
                try:
                    groups, n_files = run_scan(
                        folders, prefs_snapshot,
                        progress=lambda done, total: self._msg_queue.put(
                            ("progress", done, total)),
                        status=lambda text: self._msg_queue.put(("status", text)),
                        cancel_event=self._cancel_event)
                    self._msg_queue.put(("done", groups, n_files))
                except ScanCancelled:
                    self._msg_queue.put(("cancelled",))
                except Exception as exc:
                    self._msg_queue.put(("error", str(exc)))

            self._scan_thread = threading.Thread(target=worker, daemon=True)
            self._scan_thread.start()

        def stop_scan(self):
            self._cancel_event.set()

        def _poll_queue(self):
            try:
                while True:
                    msg = self._msg_queue.get_nowait()
                    kind = msg[0]
                    if kind == "progress":
                        done, total = msg[1], msg[2]
                        self.progress.config(maximum=max(total, 1), value=done)
                        self.status_label.config(text=f"Analyzed {done} of {total} images…")
                    elif kind == "status":
                        self.status_label.config(text=msg[1])
                    elif kind == "done":
                        self._scan_finished(msg[1], msg[2])
                    elif kind == "shell":
                        self._shell_finished(msg[1], msg[2], msg[3])
                    elif kind == "cancelled":
                        self._scan_reset("Scan stopped.")
                    elif kind == "error":
                        self._scan_reset("Scan failed.")
                        messagebox.showerror(APP_NAME, f"Scan failed:\n{msg[1]}")
            except queue.Empty:
                pass
            if self._render_queue:
                self._render_next_group()
            self.after(60, self._poll_queue)

        def _scan_reset(self, text):
            self.scan_btn.state(["!disabled"])
            self.stop_btn.state(["disabled"])
            self.progress.config(value=0)
            self.status_label.config(text=text)

        def _scan_finished(self, groups, n_files):
            self.groups = groups
            dupe_count = sum(len(g) - 1 for g in groups)
            wasted = sum(sum(f.size for f in g[1:]) for g in groups)
            status = (f"{n_files} images scanned — {len(groups)} groups, "
                      f"{dupe_count} duplicates ({human_size(wasted)} reclaimable)")
            if not groups:
                self._scan_reset(status)
                ttk.Label(self.results_frame,
                          text="No duplicates found. 🎉",
                          font=("", 16), padding=30).pack()
                return
            if self.prefs.auto_select_after_scan:
                self._preselect = compute_auto_selection(groups, self.keep_var.get())
                status += " — duplicates marked"
            self.scan_btn.state(["!disabled"])
            self.stop_btn.state(["disabled"])
            self._start_rendering(groups, status)

        def _start_rendering(self, groups, summary_text):
            """Queue groups for display; the progress bar tracks the rendering."""
            self._summary_text = summary_text
            self._render_total = len(groups)
            self.progress.config(maximum=len(groups), value=0)
            self.status_label.config(text=f"Processing 0 of {len(groups)} groups…")
            self._render_queue = list(enumerate(groups, start=1))

        # ---------------- results rendering ----------------
        def _clear_results(self):
            self._render_queue = []
            for child in self.results_frame.winfo_children():
                child.destroy()
            self._thumb_refs.clear()
            self.check_vars.clear()
            self.file_by_path.clear()
            self.groups = []
            self._preselect = set()
            self.canvas.yview_moveto(0)
            self._update_selection_label()

        def _render_next_group(self):
            # Render a few groups per tick so the UI stays responsive.
            from datetime import datetime
            for _ in range(3):
                if not self._render_queue:
                    break
                index, group = self._render_queue.pop(0)
                box = ttk.LabelFrame(
                    self.results_frame,
                    text=f"Group {index} — {len(group)} files, "
                         f"{human_size(sum(f.size for f in group))}",
                    padding=8)
                box.pack(fill="x", padx=6, pady=5)
                inner = ttk.Frame(box)
                inner.pack(fill="x")
                thumb_size = self.prefs.thumbnail_size
                for fi in group:
                    cell = ttk.Frame(inner, padding=4)
                    cell.pack(side="left", anchor="n")
                    photo = self._make_thumbnail(fi.path, thumb_size)
                    if photo is not None:
                        img_label = tk.Label(cell, image=photo, cursor="hand2")
                        img_label.pack()
                        img_label.bind("<Double-Button-1>",
                                       lambda e, p=fi.path: open_file(p))
                        img_label.bind("<Button-2>",
                                       lambda e, p=fi.path: reveal_in_finder(p))
                    var = tk.BooleanVar(value=fi.path in self._preselect)
                    var.trace_add("write", lambda *_: self._update_selection_label())
                    self.check_vars[fi.path] = var
                    self.file_by_path[fi.path] = fi
                    name = os.path.basename(fi.path)
                    if len(name) > 24:
                        name = name[:21] + "…"
                    ttk.Checkbutton(cell, text=name, variable=var).pack(anchor="w")
                    dims = f"{fi.width}×{fi.height} · " if fi.width else ""
                    when = datetime.fromtimestamp(fi.mtime).strftime("%Y-%m-%d")
                    ttk.Label(cell, text=f"{dims}{human_size(fi.size)} · {when}",
                              foreground="gray").pack(anchor="w")
                    parent = os.path.dirname(fi.path)
                    short_parent = parent
                    home = str(Path.home())
                    if short_parent.startswith(home):
                        short_parent = "~" + short_parent[len(home):]
                    if len(short_parent) > 28:
                        short_parent = "…" + short_parent[-27:]
                    path_label = ttk.Label(cell, text=short_parent, foreground="#4a78c4",
                                           cursor="hand2")
                    path_label.pack(anchor="w")
                    path_label.bind("<Button-1>",
                                    lambda e, p=fi.path: reveal_in_finder(p))
            done = self._render_total - len(self._render_queue)
            self.progress.config(maximum=max(self._render_total, 1), value=done)
            if self._render_queue:
                self.status_label.config(
                    text=f"Processing {done} of {self._render_total} groups…")
            else:
                self.status_label.config(text=self._summary_text)

        def _make_thumbnail(self, path, size):
            try:
                with Image.open(path) as img:
                    img = ImageOps.exif_transpose(img)
                    img.thumbnail((size, size))
                    photo = ImageTk.PhotoImage(img.convert("RGB"))
                self._thumb_refs.append(photo)
                return photo
            except Exception:
                return None

        # ---------------- selection & actions ----------------
        def _update_selection_label(self):
            selected = [p for p, v in self.check_vars.items() if v.get()]
            total = sum(self.file_by_path[p].size for p in selected)
            self.selection_label.config(
                text=f"{len(selected)} selected ({human_size(total)})"
                if selected else "")

        def _keep_rule_changed(self, _event=None):
            self.prefs.keep_rule = self.keep_var.get()
            self.prefs.save()

        def auto_select(self):
            # Marks rendered groups immediately; groups still rendering pick up
            # the selection from _preselect as they appear.
            self._preselect = compute_auto_selection(self.groups, self.keep_var.get())
            for path, var in self.check_vars.items():
                var.set(path in self._preselect)

        def clear_selection(self):
            self._preselect = set()
            for var in self.check_vars.values():
                var.set(False)

        def trash_selected(self):
            selected = [p for p, v in self.check_vars.items() if v.get()]
            if not selected:
                messagebox.showinfo(APP_NAME, "Nothing selected.")
                return
            # Never allow deleting an entire group.
            fully_selected = [
                i + 1 for i, g in enumerate(self.groups)
                if all(self.check_vars.get(f.path) and self.check_vars[f.path].get()
                       for f in g)]
            if fully_selected:
                messagebox.showwarning(
                    APP_NAME,
                    "Every file is selected in group(s) "
                    f"{', '.join(map(str, fully_selected))}.\n\n"
                    "Deselect at least one file per group so a copy is kept.")
                return
            self.status_label.config(text="Moving files to Trash…")
            self.update_idletasks()
            error = None
            for start in range(0, len(selected), 100):
                error = move_to_trash(selected[start:start + 100])
                if error:
                    break
            if error:
                messagebox.showerror(APP_NAME, f"Could not move files to Trash:\n{error}")
                self.status_label.config(text="Trash operation failed.")
                return
            trashed = set(selected)
            total = sum(self.file_by_path[p].size for p in selected)
            self._remove_paths_from_results(
                trashed,
                f"Moved {len(selected)} files ({human_size(total)}) to the Trash.")

        def _remove_paths_from_results(self, removed, summary):
            new_groups = []
            for group in self.groups:
                remaining = [f for f in group if f.path not in removed]
                if len(remaining) > 1:
                    new_groups.append(remaining)
            self.groups = new_groups
            self._preselect -= removed
            for path in removed:
                self.check_vars.pop(path, None)
                self.file_by_path.pop(path, None)
            for child in self.results_frame.winfo_children():
                child.destroy()
            self._thumb_refs.clear()
            self.check_vars.clear()
            self.file_by_path.clear()
            if new_groups:
                self._start_rendering(new_groups, summary)
            else:
                self.status_label.config(text=summary)
                self.progress.config(value=0)
                ttk.Label(self.results_frame, text="All duplicates handled. 🎉",
                          font=("", 16), padding=30).pack()
            self._update_selection_label()

        def _show_shell_placeholder(self):
            self._shell_placeholder_on = True
            self.shell_var.set(self._shell_placeholder)
            self.shell_entry.configure(foreground="gray55")

        def _shell_focus_in(self, _event=None):
            if self._shell_placeholder_on:
                self._shell_placeholder_on = False
                self.shell_var.set("")
                self.shell_entry.configure(foreground="")

        def _shell_focus_out(self, _event=None):
            if not self.shell_var.get().strip():
                self._show_shell_placeholder()

        def run_shell_command(self, _event=None):
            if self._shell_placeholder_on:
                return
            command = self.shell_var.get().strip()
            if not command:
                return
            self.shell_var.set("")
            self.shell_entry.state(["disabled"])
            self.status_label.config(text=f"$ {command}")

            def worker():
                try:
                    result = subprocess.run(
                        command, shell=True, capture_output=True, text=True,
                        timeout=300, cwd=str(Path.home()))
                    output = (result.stdout + result.stderr).strip()
                    self._msg_queue.put(("shell", command, result.returncode, output))
                except subprocess.TimeoutExpired:
                    self._msg_queue.put(("shell", command, -1,
                                         "Command timed out after 5 minutes."))
                except Exception as exc:
                    self._msg_queue.put(("shell", command, -1, str(exc)))

            threading.Thread(target=worker, daemon=True).start()

        def _shell_finished(self, command, returncode, output):
            self.shell_entry.state(["!disabled"])
            self.shell_entry.focus_set()
            first_line = output.splitlines()[0] if output else ""
            suffix = "" if returncode == 0 else f"  (exit {returncode})"
            if first_line:
                self.status_label.config(text=f"$ {command} → {first_line}{suffix}")
            else:
                self.status_label.config(text=f"$ {command} → done{suffix}")
            if output.count("\n") >= 1:
                win = tk.Toplevel(self)
                win.title(f"$ {command}")
                win.geometry("560x320")
                text = tk.Text(win, wrap="word", font=("Menlo", 11))
                text.insert("1.0", output)
                text.configure(state="disabled")
                text.pack(fill="both", expand=True)
                win.bind("<Escape>", lambda e: win.destroy())

        def export_csv(self):
            if not self.groups:
                messagebox.showinfo(APP_NAME, "Nothing to export — run a scan first.")
                return
            target = filedialog.asksaveasfilename(
                title="Export results", defaultextension=".csv",
                initialfile="duplicates.csv")
            if not target:
                return
            from datetime import datetime
            with open(target, "w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["group", "path", "size_bytes", "width", "height",
                                 "modified"])
                for i, group in enumerate(self.groups, start=1):
                    for fi in group:
                        writer.writerow([
                            i, fi.path, fi.size, fi.width, fi.height,
                            datetime.fromtimestamp(fi.mtime).isoformat(sep=" ",
                                                                       timespec="seconds")])
            self.status_label.config(text=f"Exported to {os.path.basename(target)}")

        def _quit(self):
            self._cancel_event.set()
            self.destroy()

    return App


# --------------------------------------------------------------------------- #
#  Self-test
# --------------------------------------------------------------------------- #

def selftest():
    import random
    import tempfile

    print("Running self-test…")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "sub").mkdir()
        random.seed(7)

        def noise_image(seed, size=(320, 240)):
            rng = random.Random(seed)
            img = Image.new("RGB", size)
            img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                         for _ in range(size[0] * size[1])])
            return img

        def gradient_image(shift=0, size=(320, 240)):
            img = Image.new("RGB", size)
            img.putdata([((x + shift) % 256, (y * 2) % 256, ((x + y) // 2) % 256)
                         for y in range(size[1]) for x in range(size[0])])
            return img

        # exact duplicates: same bytes, different names/folders
        a = noise_image(1)
        a.save(tmpdir / "original.png")
        data = (tmpdir / "original.png").read_bytes()
        (tmpdir / "copy.png").write_bytes(data)
        (tmpdir / "sub" / "copy2.png").write_bytes(data)

        # similar (resized + re-encoded) pair
        b = gradient_image()
        b.save(tmpdir / "photo.png")
        b.resize((240, 180)).save(tmpdir / "photo_small.jpg", quality=85)

        # distinct images
        noise_image(2).save(tmpdir / "unique1.png")
        gradient_image(shift=128).save(tmpdir / "unique2.png")

        prefs = Preferences()
        prefs.min_file_size_kb = 0

        prefs.match_mode = "exact"
        groups, n = run_scan([str(tmpdir)], prefs)
        assert n == 7, f"expected 7 files, saw {n}"
        assert len(groups) == 1 and len(groups[0]) == 3, \
            f"exact mode: expected one group of 3, got {[len(g) for g in groups]}"
        print(f"  exact mode OK   — {n} files, 1 group of 3 identical copies")

        prefs.match_mode = "similar"
        prefs.similarity_threshold = 90
        groups, n = run_scan([str(tmpdir)], prefs)
        sets = sorted(sorted(os.path.basename(f.path) for f in g) for g in groups)
        assert ["copy.png", "copy2.png", "original.png"] in sets, sets
        assert ["photo.png", "photo_small.jpg"] in sets, sets
        assert len(groups) == 2, f"similar mode: expected 2 groups, got {sets}"
        print(f"  similar mode OK — resized/re-encoded pair matched, uniques untouched")

        # auto-selection: "Best quality" keeps the higher-resolution original
        marked = compute_auto_selection(groups, "Best quality")
        marked_names = {os.path.basename(p) for p in marked}
        assert "photo_small.jpg" in marked_names, marked_names
        assert "photo.png" not in marked_names, marked_names
        for group in groups:  # exactly one keeper per group
            kept = [f for f in group if f.path not in marked]
            assert len(kept) == 1, [os.path.basename(f.path) for f in kept]
        print("  auto-select OK  — best-quality copy kept, rest marked")

        prefs.match_mode = "exact"
        groups, _ = run_scan([str(tmpdir)], prefs)
        assert all(f.width == 320 and f.height == 240 for f in groups[0]), \
            [(f.width, f.height) for f in groups[0]]
        print("  exact-mode dimension fill OK")
        prefs.match_mode = "similar"

        prefs.include_subfolders = False
        groups, n = run_scan([str(tmpdir)], prefs)
        assert n == 6, f"subfolder exclusion failed, saw {n} files"
        print("  subfolder preference OK")

        prefs.include_subfolders = True
        prefs.match_rotated = True
        rotated = gradient_image()
        rotated.rotate(90, expand=True).save(tmpdir / "photo_rotated.png")
        groups, n = run_scan([str(tmpdir)], prefs)
        sets = sorted(sorted(os.path.basename(f.path) for f in g) for g in groups)
        assert any("photo_rotated.png" in s and "photo.png" in s for s in sets), sets
        print("  rotation matching OK")

    print("All self-tests passed.")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    App = build_gui()
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
