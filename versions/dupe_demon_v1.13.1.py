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
- Reveal in Finder, open files

Requires: Python 3.9+, Pillow. HEIC support is enabled automatically if
pillow-heif is installed (pip install pillow-heif).

Run:  python3 dupe_demon.py
Test: python3 dupe_demon.py --selftest
"""

import hashlib
import json
import os
import queue
import shutil
import sqlite3
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
__version__ = "1.13.1"
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "DupeDemon"
PREFS_FILE = APP_SUPPORT_DIR / "preferences.json"
CACHE_FILE = APP_SUPPORT_DIR / "hash_cache.db"
_OLD_PREFS_FILE = (Path.home() / "Library" / "Application Support"
                   / "DuplicatePhotoFinder" / "preferences.json")

DEFAULT_EXTENSIONS = ["jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp"]
if HEIC_SUPPORTED:
    DEFAULT_EXTENSIONS += ["heic", "heif"]

PAIRWISE_LIMIT = 2500      # below this, compare every pair; above, use banding


class PieProgress(tk.Canvas):
    """Spinning pie-chart progress indicator."""

    def __init__(self, master, size=28, bg_color="#e0e0e0", fg_color="#3584e4",
                 **kw):
        kw.setdefault("width", size)
        kw.setdefault("height", size)
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("borderwidth", 0)
        super().__init__(master, **kw)
        self._size = size
        self._bg_color = bg_color
        self._fg_color = fg_color
        self._value = 0
        self._maximum = 100
        self._rotation = 0
        self._spinning = False
        self._draw()

    def config(self, **kw):
        changed = False
        if "value" in kw:
            self._value = kw.pop("value")
            changed = True
        if "maximum" in kw:
            self._maximum = max(kw.pop("maximum"), 1)
            changed = True
        if kw:
            super().config(**kw)
        if changed:
            if self._value > 0 and not self._spinning:
                self._spinning = True
                self._spin()
            elif self._value <= 0:
                self._spinning = False
                self._rotation = 0
            self._draw()

    configure = config

    def _draw(self):
        self.delete("all")
        pad = 2
        s = self._size - pad * 2
        self.create_oval(pad, pad, pad + s, pad + s, fill=self._bg_color,
                         outline="")
        if self._maximum > 0 and self._value > 0:
            frac = min(self._value / self._maximum, 1.0)
            extent = frac * 360
            start = 90 - self._rotation
            self.create_arc(pad, pad, pad + s, pad + s, start=start,
                            extent=-extent, fill=self._fg_color, outline="",
                            style="pieslice")

    def _spin(self):
        if not self._spinning:
            return
        self._rotation = (self._rotation + 6) % 360
        self._draw()
        self.after(30, self._spin)


class Group(list):
    """A duplicate group with .similarity (0-100) and .match_type ('exact'|'similar')."""
    similarity: float = 100.0
    match_type: str = "similar"


# Sort options for the results pane.
# Each value returns a key that sorted() will order ASCENDING; using -x flips it.
SORT_KEYS = {
    "Reclaimable size (largest)": lambda g: -sum(f.size for f in g[1:]),
    "Confidence (weakest first)": lambda g: (getattr(g, "similarity", 100.0),
                                             -sum(f.size for f in g[1:])),
    "Confidence (strongest first)": lambda g: (-getattr(g, "similarity", 100.0),
                                               -sum(f.size for f in g[1:])),
    "Group size (most files)": lambda g: (-len(g), -sum(f.size for f in g[1:])),
    "Total size (largest)": lambda g: -sum(f.size for f in g),
}
RESULTS_PAGE_SIZE = 40     # groups rendered per page in the results view


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
    shell_warning_accepted: bool = False   # one-time warning before first shell command
    ask_full_disk_access: bool = True      # offer to enable Full Disk Access at launch
    use_hash_cache: bool = True            # persist hashes so re-scans skip unchanged files

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
#  Hash cache — re-scans skip files whose path+size+mtime are unchanged
# --------------------------------------------------------------------------- #

class HashCache:
    """SQLite-backed cache of perceptual and SHA-256 hashes.

    An entry is valid only while the file's size and mtime match, so edited
    or replaced files are re-hashed automatically. The DB is local, owner-
    read/write only (0600), and removed by the built-in uninstaller.
    """

    def __init__(self, db_path=None):
        self.path = Path(db_path or CACHE_FILE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS phash ("
            " path TEXT NOT NULL, hash_size INTEGER NOT NULL,"
            " size INTEGER, mtime REAL, hashes TEXT,"
            " width INTEGER, height INTEGER,"
            " PRIMARY KEY (path, hash_size))")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS sha ("
            " path TEXT PRIMARY KEY, size INTEGER, mtime REAL, full TEXT)")
        self.conn.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get_phashes(self, files, hash_size):
        """{path: (hashes, width, height)} for files with valid cache entries."""
        rows = self.conn.execute(
            "SELECT path, size, mtime, hashes, width, height FROM phash"
            " WHERE hash_size=?", (hash_size,))
        by_path = {r[0]: r for r in rows}
        out = {}
        for fi in files:
            row = by_path.get(fi.path)
            if row and row[1] == fi.size and row[2] == fi.mtime:
                out[fi.path] = ([int(h) for h in row[3].split(",")],
                                row[4], row[5])
        return out

    def put_phashes(self, entries, hash_size):
        """entries: iterable of (FileInfo, hashes)."""
        self.conn.executemany(
            "INSERT OR REPLACE INTO phash"
            " (path, hash_size, size, mtime, hashes, width, height)"
            " VALUES (?,?,?,?,?,?,?)",
            [(fi.path, hash_size, fi.size, fi.mtime,
              ",".join(str(h) for h in hashes), fi.width, fi.height)
             for fi, hashes in entries])
        self.conn.commit()

    def get_shas(self, files):
        """{path: full_sha} for files with valid cache entries."""
        rows = self.conn.execute("SELECT path, size, mtime, full FROM sha")
        by_path = {r[0]: r for r in rows}
        return {fi.path: by_path[fi.path][3] for fi in files
                if fi.path in by_path
                and by_path[fi.path][1] == fi.size
                and by_path[fi.path][2] == fi.mtime}

    def put_shas(self, entries):
        """entries: iterable of (FileInfo, full_sha)."""
        self.conn.executemany(
            "INSERT OR REPLACE INTO sha (path, size, mtime, full) VALUES (?,?,?,?)",
            [(fi.path, fi.size, fi.mtime, sha) for fi, sha in entries])
        self.conn.commit()

    def clear(self):
        self.conn.execute("DELETE FROM phash")
        self.conn.execute("DELETE FROM sha")
        self.conn.commit()
        self.conn.execute("VACUUM")

    def size_bytes(self):
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def open_cache(prefs):
    """HashCache per preferences; None if disabled or unavailable."""
    if not prefs.use_hash_cache:
        return None
    try:
        return HashCache()
    except Exception:
        return None


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
    is_reference: bool = False   # from a Reference folder (never deleted)


class ScanCancelled(Exception):
    pass


def collect_files(folders, prefs: Preferences, cancel_event=None):
    """Walk the folders and return a list of candidate FileInfo.

    `folders` may be a list of path strings (all treated as Source) or a list
    of dicts {"path": ..., "kind": "source"|"reference"}. Files under a
    Reference folder are marked is_reference=True and will never be deleted."""
    exts = {"." + e.lower().lstrip(".") for e in prefs.extensions}
    min_bytes = prefs.min_file_size_kb * 1024
    seen = set()
    files = []
    for entry in folders:
        if isinstance(entry, str):
            folder, kind = entry, "source"
        else:
            folder = entry.get("path", "")
            kind = entry.get("kind", "source")
        if kind == "off":
            continue
        is_ref = (kind == "reference")
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
                    files.append(FileInfo(full, stat.st_size, stat.st_mtime,
                                          is_reference=is_ref))
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


def find_exact_duplicates(files, prefs, progress=None, cancel_event=None, cache=None):
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

    if cache is not None:
        # Cached path: full hashes only (cache hits need no file reads at all).
        cached = cache.get_shas([fi for g in candidates for fi in g])
        new_entries = []
        with ThreadPoolExecutor(max_workers=prefs.worker_threads * 10) as pool:
            for same_size in candidates:
                check_cancel()
                full = {}
                to_compute = []
                for fi in same_size:
                    if fi.path in cached:
                        full.setdefault(cached[fi.path], []).append(fi)
                        done += 1
                        if progress:
                            progress(done, total)
                    else:
                        to_compute.append(fi)
                futures = {pool.submit(_sha256, fi.path): fi for fi in to_compute}
                for fut in as_completed(futures):
                    check_cancel()
                    fi = futures[fut]
                    done += 1
                    if progress:
                        progress(done, total)
                    try:
                        sha = fut.result()
                    except OSError:
                        continue
                    full.setdefault(sha, []).append(fi)
                    new_entries.append((fi, sha))
                for dupes in full.values():
                    if len(dupes) > 1:
                        gg = Group(sorted(dupes, key=lambda f: f.path))
                        gg.match_type = "exact"
                        gg.similarity = 100.0
                        groups.append(gg)
        if new_entries:
            cache.put_shas(new_entries)
        return groups

    with ThreadPoolExecutor(max_workers=prefs.worker_threads * 10) as pool:
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
                        gg = Group(sorted(dupes, key=lambda f: f.path))
                        gg.match_type = "exact"
                        gg.similarity = 100.0
                        groups.append(gg)
    return groups


def find_similar_images(files, prefs, progress=None, cancel_event=None, cache=None,
                        status=None):
    """Group perceptually similar images. Returns list of lists of FileInfo."""
    hash_bits = prefs.hash_size * prefs.hash_size
    max_distance = round(hash_bits * (100 - prefs.similarity_threshold) / 100)
    rotations_needed = 4 if prefs.match_rotated else 1

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()

    entries = []  # (FileInfo, [hashes])
    total = len(files)
    done = 0

    cached = cache.get_phashes(files, prefs.hash_size) if cache else {}
    to_compute = []
    for fi in files:
        hit = cached.get(fi.path)
        if hit and len(hit[0]) >= rotations_needed:
            hashes, fi.width, fi.height = hit
            entries.append((fi, hashes[:rotations_needed]
                            if not prefs.match_rotated else hashes))
            done += 1
            if progress:
                progress(done, total)
        else:
            to_compute.append(fi)

    new_entries = []
    with ThreadPoolExecutor(max_workers=prefs.worker_threads * 10) as pool:
        futures = {
            pool.submit(compute_perceptual_hashes, fi.path, prefs.hash_size,
                        prefs.match_rotated): fi
            for fi in to_compute
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
            new_entries.append((fi, hashes))

    if cache is not None and new_entries:
        cache.put_phashes(new_entries, prefs.hash_size)

    check_cancel()
    if status:
        status(f"Matching {len(entries)} images — this can take several minutes…")

    uf = _UnionFind()

    # Collapse identical hash signatures first — exact/near-exact copies are
    # the common case, and matching then only runs on unique signatures.
    by_sig = {}
    for idx, (_fi, hashes) in enumerate(entries):
        by_sig.setdefault(tuple(hashes), []).append(idx)
    reps = []
    for sig, idxs in by_sig.items():
        rep = idxs[0]
        for other in idxs[1:]:
            uf.union(rep, other)
        reps.append((rep, sig))

    m = len(reps)
    if m <= PAIRWISE_LIMIT:
        for a in range(m):
            if a % 64 == 0:
                check_cancel()
                if status and m > 500:
                    status(f"Matching similar images… {a * 100 // m}% "
                           "(this can take several minutes)")
            ia, sig_a = reps[a]
            for b in range(a + 1, m):
                ib, sig_b = reps[b]
                base_b = sig_b[0]
                for h in sig_a:
                    if (h ^ base_b).bit_count() <= max_distance:
                        uf.union(ia, ib)
                        break
    else:
        # Pigeonhole banding: two hashes within `max_distance` bits must share
        # at least one of (max_distance + 1) equal-width bit chunks.
        num_bands = max_distance + 1
        band_width = max(1, hash_bits // num_bands)
        buckets = {}
        for pos, (_idx, sig) in enumerate(reps):
            h = sig[0]
            for band in range(num_bands):
                chunk = (h >> (band * band_width)) & ((1 << band_width) - 1)
                buckets.setdefault((band, chunk), []).append(pos)
        bucket_list = [b for b in buckets.values() if len(b) > 1]
        for bnum, bucket in enumerate(bucket_list):
            check_cancel()
            if status and bnum % 32 == 0 and bucket_list:
                status(f"Matching similar images… "
                       f"{bnum * 100 // len(bucket_list)}% "
                       "(this can take several minutes)")
            for x in range(len(bucket)):
                if x % 128 == 0:
                    check_cancel()
                ix, sig_x = reps[bucket[x]]
                for y in range(x + 1, len(bucket)):
                    iy, sig_y = reps[bucket[y]]
                    if uf.find(ix) == uf.find(iy):
                        continue
                    base_y = sig_y[0]
                    for h in sig_x:
                        if (h ^ base_y).bit_count() <= max_distance:
                            uf.union(ix, iy)
                            break

    groups_map = {}
    for idx in list(uf.parent):
        groups_map.setdefault(uf.find(idx), []).append(entries[idx][0])

    hash_by_path = {fi.path: sig for (fi, sig) in entries}

    def _group_similarity(files):
        if len(files) < 2:
            return 100.0
        worst = 0
        for i in range(len(files)):
            sig_a = hash_by_path.get(files[i].path) or ()
            for j in range(i + 1, len(files)):
                sig_b = hash_by_path.get(files[j].path) or ()
                if not sig_a or not sig_b:
                    continue
                best_pair = min((a ^ b).bit_count() for a in sig_a for b in sig_b)
                if best_pair > worst:
                    worst = best_pair
        return round(100.0 * (1.0 - worst / hash_bits), 1)

    result = []
    for g in groups_map.values():
        if len(g) < 2:
            continue
        gg = Group(sorted(g, key=lambda f: f.path))
        gg.match_type = "similar"
        gg.similarity = _group_similarity(gg)
        result.append(gg)
    return result


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
    """Choose the file to keep in a group.

    Reference files (from Reference folders) always outrank Source files —
    dupeGuru-style. If the group has any reference files, the keeper is
    picked from among them; otherwise the rule applies to the full group."""
    refs = [f for f in group if getattr(f, "is_reference", False)]
    pool = refs if refs else list(group)
    key = KEEP_RULES.get(rule)
    if key is None:
        return pool[0]
    return max(pool, key=key)


def compute_auto_selection(groups, rule):
    """Return the set of paths to mark for removal.

    Reference files are never marked. If a group's keeper is a reference
    file, every non-reference file in the group is marked."""
    marked = set()
    for group in groups:
        keeper = pick_keeper(group, rule)
        for f in group:
            if getattr(f, "is_reference", False):
                continue
            if f.path != keeper.path:
                marked.add(f.path)
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
    with ThreadPoolExecutor(max_workers=prefs.worker_threads * 10) as pool:
        list(pool.map(read_dims, [fi for g in groups for fi in g]))


def run_scan(folders, prefs, progress=None, status=None, cancel_event=None):
    """Full scan pipeline. Returns list of duplicate groups."""
    if status:
        status("Collecting files…")
    files = collect_files(folders, prefs, cancel_event)
    if status:
        status(f"Analyzing {len(files)} images…")
    cache = open_cache(prefs)
    try:
        if prefs.match_mode == "exact":
            groups = find_exact_duplicates(files, prefs, progress, cancel_event,
                                           cache=cache)
            _fill_dimensions(groups, prefs)
        else:
            groups = find_similar_images(files, prefs, progress, cancel_event,
                                         cache=cache, status=status)
    finally:
        if cache is not None:
            cache.close()
    # Drop groups where every file is from a Reference folder — nothing to do.
    groups = [g for g in groups
              if any(not getattr(f, "is_reference", False) for f in g)]
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


def quick_look(path):
    # `qlmanage -p` opens the macOS Quick Look panel and stays until dismissed;
    # spawn detached so it doesn't block the Tk event loop.
    subprocess.Popen(
        ["qlmanage", "-p", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def cache_size_bytes():
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(CACHE_FILE) + suffix)
        if p.exists():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def clear_hash_cache():
    """Delete the SQLite cache and its WAL/SHM sidecars. Returns True on success."""
    try:
        cache = HashCache()
        cache.clear()
        cache.close()
    except Exception:
        pass
    ok = True
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(CACHE_FILE) + suffix)
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            ok = False
    return ok


def _unique_destination(directory, filename):
    """Return an absolute path in `directory` that doesn't collide."""
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    i = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base} ({i}){ext}")
        i += 1
    return candidate


def app_bundle_path():
    """Path of the .app bundle when running frozen, else None (source mode)."""
    if getattr(sys, "frozen", False):
        p = Path(sys.executable)
        for parent in p.parents:
            if parent.suffix == ".app":
                return parent
    return None


def uninstall_paths():
    """Everything Dupe Demon leaves on the system, in Trash-able order."""
    bundle_id = "com.saltz.dupedemon"
    lib = Path.home() / "Library"
    candidates = [
        app_bundle_path(),
        APP_SUPPORT_DIR,
        _OLD_PREFS_FILE.parent,                                   # pre-rename leftovers
        lib / "Preferences" / f"{bundle_id}.plist",
        lib / "Saved Application State" / f"{bundle_id}.savedState",
        lib / "Caches" / bundle_id,
    ]
    return [p for p in candidates if p is not None and p.exists()]


def has_full_disk_access():
    """Probe a TCC-protected folder; PermissionError means no Full Disk Access."""
    try:
        os.listdir(Path.home() / ".Trash")
        return True
    except PermissionError:
        return False
    except OSError:
        return True  # can't probe — don't nag


def open_full_disk_access_settings():
    subprocess.run(["open",
                    "x-apple.systempreferences:com.apple.preference.security"
                    "?Privacy_AllFiles"])


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

    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        _TkBase = TkinterDnD.Tk
        _DND_AVAILABLE = True
    except Exception:
        _TkBase = tk.Tk
        _DND_AVAILABLE = False
        DND_FILES = None

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
            ttk.Spinbox(body, from_=1, to=128, textvariable=self.threads_var,
                        width=10).grid(row=row, column=1, sticky="w"); row += 1

            self.cache_var = tk.BooleanVar(value=prefs.use_hash_cache)
            ttk.Checkbutton(body, text="Use hash cache (re-scans skip unchanged files)",
                            variable=self.cache_var).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=2)
            self.clear_cache_btn = ttk.Button(body, command=self._clear_cache)
            self.clear_cache_btn.grid(row=row, column=2, sticky="e")
            self._refresh_cache_button()
            row += 1

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
            buttons.grid(row=row, column=0, columnspan=3, sticky="we", pady=(16, 0))
            ttk.Button(buttons, text="Uninstall Dupe Demon…",
                       command=lambda: (self.destroy(), master.uninstall_app())
                       ).pack(side="left")
            ttk.Button(buttons, text="Restore Defaults",
                       command=self._restore_defaults).pack(side="left", padx=(20, 20))
            save_btn = ttk.Button(buttons, text="Save", command=self._save)
            save_btn.pack(side="right")
            ttk.Button(buttons, text="Cancel", command=self.destroy).pack(
                side="right", padx=(0, 8))
            self.bind("<Return>", lambda e: self._save())
            self.bind("<Escape>", lambda e: self.destroy())
            self._update_state()

        def _refresh_cache_button(self):
            size = cache_size_bytes()
            self.clear_cache_btn.config(
                text=f"Clear Cache ({human_size(size)})" if size else "Clear Cache",
                state="normal" if size else "disabled")

        def _clear_cache(self):
            clear_hash_cache()
            self._refresh_cache_button()
            # Also refresh the main-window button label.
            try:
                self.master.refresh_cache_button()
            except Exception:
                pass

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
            self.cache_var.set(defaults.use_hash_cache)
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
                p.worker_threads = min(128, max(1, int(self.threads_var.get())))
            except ValueError:
                pass
            exts = [e.strip().lstrip(".").lower()
                    for e in self.ext_var.get().replace(";", ",").split(",")]
            p.extensions = [e for e in exts if e] or list(DEFAULT_EXTENSIONS)
            p.thumbnail_size = self.thumb_var.get()
            p.use_hash_cache = self.cache_var.get()
            p.save()
            self.on_save()
            self.destroy()

    class App(_TkBase):
        def __init__(self):
            super().__init__()
            self.title(f"{APP_NAME}  v{__version__}")
            # open at ~95% of the screen, centered
            screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
            win_w, win_h = int(screen_w * 0.98), int(screen_h * 0.96)
            pos_x = (screen_w - win_w) // 2
            pos_y = max(25, (screen_h - win_h) // 2 - 15)
            self.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
            self.minsize(1000, 680)
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
            self._render_track_progress = True
            self._summary_text = ""
            self._pending_groups = []
            self._groups_total = 0
            self._more_btn = None
            self._scroll_update_pending = False

            self._build_widgets()
            self._register_drop_targets()
            self._poll_queue()
            self.createcommand("tk::mac::Quit", self._quit)
            # restore the window when the Dock icon is clicked while minimized
            self.createcommand("tk::mac::ReopenApplication", self._reopen)
            self.bind("<Command-comma>", lambda e: self.open_preferences())
            self.bind("<Command-o>", lambda e: self.add_folder())
            self.after(700, self._maybe_prompt_full_disk_access)

        # ---------------- drag & drop ----------------
        def _register_drop_targets(self):
            if not _DND_AVAILABLE:
                return
            self._drop_overlays = {}
            targets = [
                (self.folder_list, self.folder_frame),
                (self.canvas, self.results_outer),
            ]
            for w, overlay_parent in targets:
                try:
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", self._on_drop)
                    w.dnd_bind("<<DropEnter>>",
                               lambda e, p=overlay_parent: self._on_drop_enter(e, p))
                    w.dnd_bind("<<DropLeave>>",
                               lambda e, p=overlay_parent: self._on_drop_leave(e, p))
                except Exception:
                    pass

        def _on_drop_enter(self, event, parent):
            try:
                self.status_label.configure(text="Drop to add folders…")
            except Exception:
                pass
            if parent not in self._drop_overlays:
                overlay = tk.Frame(parent, background="white")
                overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
                self._drop_overlays[parent] = overlay
            return event.action

        def _on_drop_leave(self, event, parent=None):
            if parent is not None:
                overlay = self._drop_overlays.pop(parent, None)
                if overlay is not None:
                    overlay.place_forget()
                    overlay.destroy()
            else:
                for p, overlay in list(self._drop_overlays.items()):
                    overlay.place_forget()
                    overlay.destroy()
                self._drop_overlays.clear()
            return event.action

        def _on_drop(self, event):
            self._on_drop_leave(event)
            paths = self.tk.splitlist(event.data)  # handles {braced paths with spaces}
            added, added_parents, skipped = [], [], []
            existing = self._folder_paths_set()
            for raw in paths:
                p = os.path.abspath(os.path.expanduser(raw))
                if os.path.isdir(p):
                    if p in existing:
                        continue
                    self._add_folder_entry(p, "source")
                    existing.add(p)
                    added.append(p)
                elif os.path.isfile(p):
                    parent = os.path.dirname(p)
                    if parent and parent not in existing:
                        self._add_folder_entry(parent, "source")
                        existing.add(parent)
                        added_parents.append(parent)
                else:
                    skipped.append(raw)
            parts = []
            if added:
                parts.append(f"Added {len(added)} folder{'s' if len(added) != 1 else ''}.")
            if added_parents:
                parts.append(f"Added {len(added_parents)} parent folder"
                             f"{'s' if len(added_parents) != 1 else ''} from dropped files.")
            if skipped:
                parts.append(f"Skipped {len(skipped)} (not found).")
            if not parts:
                parts.append("Already in list.")
            self.status_label.configure(text=" ".join(parts))
            return event.action

        # ---------------- folder model helpers ----------------
        def _folder_paths_set(self):
            return {e["path"] for e in self.folders}

        def _kind_display(self, kind):
            if kind == "reference":
                return "🔒 Reference"
            if kind == "off":
                return "⏸ Off"
            return "Source"

        def _add_folder_entry(self, path, kind="source"):
            entry = {"path": path, "kind": kind}
            self.folders.append(entry)
            self.folder_list.insert(
                "", "end", iid=path,
                values=(self._kind_display(kind), path),
                tags=(kind,) if kind in ("reference", "off") else ())

        def _refresh_folder_row(self, path):
            for e in self.folders:
                if e["path"] == path:
                    self.folder_list.item(
                        path,
                        values=(self._kind_display(e["kind"]), path),
                        tags=(e["kind"],) if e["kind"] in ("reference", "off") else ())
                    return

        def toggle_folder_kind(self):
            sel = list(self.folder_list.selection())
            if not sel:
                return
            for path in sel:
                for e in self.folders:
                    if e["path"] == path:
                        _CYCLE = {"source": "reference", "reference": "off", "off": "source"}
                        e["kind"] = _CYCLE.get(e["kind"], "source")
                        self._refresh_folder_row(path)
                        break

        def _maybe_prompt_full_disk_access(self):
            if not self.prefs.ask_full_disk_access or has_full_disk_access():
                return
            win = tk.Toplevel(self)
            win.title("Full Disk Access")
            win.resizable(False, False)
            win.transient(self)
            body = ttk.Frame(win, padding=20)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text="Some features need Full Disk Access",
                      font=("", 14, "bold")).pack(anchor="w")
            ttk.Label(body, justify="left", wraplength=430, text=(
                "The shell box (and scans of protected folders like the Trash, "
                "Desktop, or Documents on some systems) won't see everything "
                "until Dupe Demon has Full Disk Access.\n\n"
                "To enable: System Settings → Privacy & Security → "
                "Full Disk Access → add Dupe Demon, then relaunch the app."
            )).pack(anchor="w", pady=(8, 14))
            buttons = ttk.Frame(body)
            buttons.pack(fill="x")

            def dont_ask():
                self.prefs.ask_full_disk_access = False
                self.prefs.save()
                win.destroy()

            def enable_now():
                open_full_disk_access_settings()
                win.destroy()

            ttk.Button(buttons, text="Don't Ask Again",
                       command=dont_ask).pack(side="left")
            ttk.Button(buttons, text="Later",
                       command=win.destroy).pack(side="right")
            open_btn = ttk.Button(buttons, text="Open System Settings…",
                                  command=enable_now)
            open_btn.pack(side="right", padx=(0, 8))
            open_btn.focus_set()
            win.bind("<Return>", lambda e: enable_now())
            win.bind("<Escape>", lambda e: win.destroy())
            self._center_over_main(win)

        def _center_over_main(self, win):
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            x = self.winfo_x() + (self.winfo_width() - w) // 2
            y = self.winfo_y() + (self.winfo_height() - h) // 3
            win.geometry(f"+{max(0, x)}+{max(25, y)}")

        # ---------------- layout ----------------
        def _build_widgets(self):
            toolbar = ttk.Frame(self, padding=(12, 10, 12, 6))
            toolbar.pack(fill="x")
            ttk.Button(toolbar, text="＋ Add Folder…", command=self.add_folder).pack(side="left")
            ttk.Button(toolbar, text="－ Remove from List", command=self.remove_folder).pack(
                side="left", padx=(6, 0))
            self.scan_btn = ttk.Button(toolbar, text="▶ Scan", command=self.start_scan)
            self.scan_btn.pack(side="left", padx=(18, 0))
            self.stop_btn = ttk.Button(toolbar, text="■ Stop", command=self.stop_scan,
                                       state="disabled")
            self.stop_btn.pack(side="left", padx=(6, 0))
            ttk.Button(toolbar, text="Preferences…  (⌘,)",
                       command=self.open_preferences).pack(side="right")
            self.cache_btn = ttk.Button(toolbar, command=self.clear_cache_action)
            self.cache_btn.pack(side="right", padx=(0, 8))
            self.refresh_cache_button()
            self.mode_label = ttk.Label(toolbar, foreground="gray")
            self.mode_label.pack(side="right", padx=(0, 14))
            self._refresh_mode_label()

            progress_frame = ttk.Frame(self, padding=(12, 4))
            progress_frame.pack(fill="x")
            self.progress = PieProgress(progress_frame, size=28)
            self.progress.pack(side="left", padx=(0, 6))
            self.status_label = ttk.Label(
                progress_frame,
                text="Ready. Drag folders here." if _DND_AVAILABLE else "Ready.",
                width=46, anchor="w")
            self.status_label.pack(side="right", padx=(10, 0))

            # 30/70 split: folders on the left, results on the right
            self.split = ttk.PanedWindow(self, orient="horizontal")
            self.split.pack(fill="both", expand=True, padx=12, pady=(4, 6))

            self.folder_frame = ttk.LabelFrame(self.split, text="Folders to scan", padding=6)
            folder_frame = self.folder_frame

            tree_wrap = ttk.Frame(folder_frame)
            tree_wrap.pack(fill="both", expand=True)
            self.folder_list = ttk.Treeview(
                tree_wrap, columns=("kind", "path"), show="headings",
                selectmode="extended")
            self.folder_list.heading("kind", text="Type")
            self.folder_list.heading("path", text="Folder")
            self.folder_list.column("kind", width=110, minwidth=90, stretch=False,
                                    anchor="w")
            self.folder_list.column("path", width=260, minwidth=140, stretch=True,
                                    anchor="w")
            self.folder_list.tag_configure(
                "reference", foreground="#1a5fb4", background="#e8f0fe")
            self.folder_list.tag_configure(
                "off", foreground="#999999")
            fscroll = ttk.Scrollbar(tree_wrap, orient="vertical",
                                    command=self.folder_list.yview)
            self.folder_list.configure(yscrollcommand=fscroll.set)
            fscroll.pack(side="right", fill="y")
            self.folder_list.pack(side="left", fill="both", expand=True)
            self.folder_list.bind("<Double-Button-1>",
                                  lambda e: self.toggle_folder_kind())

            ftools = ttk.Frame(folder_frame)
            ftools.pack(fill="x", pady=(6, 0))
            ttk.Button(ftools, text="Toggle Source / Reference / Off",
                       command=self.toggle_folder_kind).pack(side="left")
            ttk.Label(
                ftools, foreground="gray",
                text="Reference = protected (never deleted). Double-click a row to toggle."
            ).pack(side="left", padx=(10, 0))

            self.split.add(folder_frame, weight=30)

            # results area: scrollable canvas
            self.results_outer = ttk.LabelFrame(self.split, text="Results", padding=4)
            results_outer = self.results_outer

            sort_header = ttk.Frame(results_outer)
            sort_header.pack(fill="x", pady=(0, 4))
            ttk.Label(sort_header, text="Sort by:").pack(side="left")
            self.sort_var = tk.StringVar(value="Reclaimable size (largest)")
            sort_menu = ttk.Combobox(
                sort_header, textvariable=self.sort_var, state="readonly",
                values=list(SORT_KEYS), width=30)
            sort_menu.pack(side="left", padx=(6, 0))
            sort_menu.bind("<<ComboboxSelected>>",
                           lambda e: self._resort_and_render())

            body = ttk.Frame(results_outer)
            body.pack(fill="both", expand=True)
            self.canvas = tk.Canvas(body, highlightthickness=0)
            def _scroll_cmd(*args):
                self.canvas.yview(*args)
                self._check_scroll_load()
            scrollbar = ttk.Scrollbar(body, orient="vertical",
                                      command=_scroll_cmd)
            self.canvas.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            self.canvas.pack(side="left", fill="both", expand=True)
            self.results_frame = ttk.Frame(self.canvas)
            self._canvas_window = self.canvas.create_window(
                (0, 0), window=self.results_frame, anchor="nw")
            self.results_frame.bind("<Configure>", self._schedule_scroll_update)
            self.canvas.bind(
                "<Configure>",
                lambda e: self.canvas.itemconfigure(self._canvas_window, width=e.width))
            self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
            self.split.add(results_outer, weight=70)
            # position the sash at 30% once the window has a real width
            self.after(0, self._set_initial_sash)

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
            self.trash_btn = ttk.Button(actions, text="🗑 Move Selected to Trash",
                                        command=self.trash_selected)
            self.trash_btn.pack(side="right", padx=(0, 10))
            self.move_btn = ttk.Button(actions, text="📦 Move Selected to…",
                                       command=self.move_selected_to_folder)
            self.move_btn.pack(side="right", padx=(0, 8))
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
            self._check_scroll_load()

        def _schedule_scroll_update(self, _event=None):
            # coalesce per-widget <Configure> storms into one update per idle
            if not self._scroll_update_pending:
                self._scroll_update_pending = True
                self.after_idle(self._do_scroll_update)

        def _do_scroll_update(self):
            self._scroll_update_pending = False
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        def _check_scroll_load(self):
            if not self._pending_groups or self._render_queue:
                return
            _, y_end = self.canvas.yview()
            if y_end > 0.85:
                self._render_more()

        # ---------------- folder management ----------------
        def add_folder(self):
            folder = filedialog.askdirectory(title="Choose a folder to scan")
            if folder and folder not in self._folder_paths_set():
                self._add_folder_entry(folder, "source")

        def remove_folder(self):
            selected = list(self.folder_list.selection())
            if not selected:
                return
            self.folders = [e for e in self.folders if e["path"] not in selected]
            for path in selected:
                if self.folder_list.exists(path):
                    self.folder_list.delete(path)

        def _set_initial_sash(self):
            try:
                self.update_idletasks()
                total = self.split.winfo_width()
                if total > 300:
                    self.split.sashpos(0, int(total * 0.30))
            except Exception:
                pass

        # ---------------- cache ----------------
        def refresh_cache_button(self):
            if not hasattr(self, "cache_btn"):
                return
            size = cache_size_bytes()
            self.cache_btn.config(
                text=f"Clear Cache ({human_size(size)})" if size else "Clear Cache",
                state="normal" if size else "disabled")

        def clear_cache_action(self):
            size = cache_size_bytes()
            if not size:
                self.status_label.config(text="Cache is already empty.")
                self.refresh_cache_button()
                return
            if not messagebox.askokcancel(
                    APP_NAME,
                    f"Clear the hash cache ({human_size(size)})?\n\n"
                    "Next scan will re-hash every file (still safe — nothing "
                    "in your library is touched)."):
                return
            ok = clear_hash_cache()
            self.refresh_cache_button()
            self.status_label.config(
                text="Cache cleared." if ok else "Cache partially cleared (see log).")

        # ---------------- preferences ----------------
        def open_preferences(self):
            win = PreferencesWindow(self, self.prefs, on_save=self._refresh_mode_label)
            self._center_over_main(win)

        # ---------------- scanning ----------------
        def start_scan(self):
            if self._scan_thread and self._scan_thread.is_alive():
                return
            if not self.folders:
                self.status_label.config(text="Add at least one folder to scan.")
                return
            self._clear_results()
            self._cancel_event.clear()
            self.scan_btn.state(["disabled"])
            self.stop_btn.state(["!disabled"])
            self.progress.config(value=0, maximum=100)
            self.status_label.config(text="Starting…")
            prefs_snapshot = Preferences(**asdict(self.prefs))
            folders = [dict(e) for e in self.folders]

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

        def _reopen(self):
            self.deiconify()
            self.lift()
            self.focus_force()

        def stop_scan(self):
            self._cancel_event.set()
            if self._render_queue or self._pending_groups:
                shown = (self._groups_total - len(self._pending_groups)
                         - len(self._render_queue))
                self._render_queue = []
                self._pending_groups = []
                self._remove_more_button()
                self.groups = self.groups[:shown]
                # keep selection/trash consistent with what is actually shown
                self.file_by_path = {f.path: f for g in self.groups for f in g}
                self._preselect &= set(self.file_by_path)
                self.stop_btn.state(["disabled"])
                self.status_label.config(
                    text=f"Stopped — showing first {shown} of "
                         f"{self._groups_total} groups.")
                self._update_selection_label()

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
            self._sort_groups_inplace(groups)
            self.groups = groups
            self.refresh_cache_button()
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

        def _sort_groups_inplace(self, groups):
            key = SORT_KEYS.get(self.sort_var.get())
            if key is not None:
                groups.sort(key=key)

        def _resort_and_render(self):
            if not self.groups:
                return
            self._sort_groups_inplace(self.groups)
            for child in self.results_frame.winfo_children():
                child.destroy()
            self._thumb_refs.clear()
            self.check_vars.clear()
            self._render_queue = []
            self._pending_groups = []
            self._remove_more_button()
            self._start_rendering(list(self.groups), self._summary_text,
                                  track_progress=False)

        def _start_rendering(self, groups, summary_text, track_progress=True):
            """Queue groups for display, one page at a time. track_progress=False
            keeps the bar as-is (after deletes, overall progress stays put)."""
            self._summary_text = summary_text
            self._groups_total = len(groups)
            # selection works across ALL groups, rendered or not
            self.file_by_path = {f.path: f for g in groups for f in g}
            self._remove_more_button()
            all_items = list(enumerate(groups, start=1))
            page = all_items[:RESULTS_PAGE_SIZE]
            self._pending_groups = all_items[RESULTS_PAGE_SIZE:]
            self._render_total = len(page)
            self._render_track_progress = track_progress
            if track_progress:
                self.progress.config(maximum=max(len(page), 1), value=0)
            self.status_label.config(text=f"Processing 0 of {len(page)} groups…")
            self.stop_btn.state(["!disabled"])
            self._render_queue = page
            self._update_selection_label()

        def _show_more_button(self):
            remaining = sum(1 for _ in self._pending_groups)
            self._more_btn = ttk.Button(
                self.results_frame,
                text=f"Show More Groups ({remaining} remaining)",
                command=self._render_more)
            self._more_btn.pack(pady=12)

        def _remove_more_button(self):
            if self._more_btn is not None:
                self._more_btn.destroy()
                self._more_btn = None

        def _render_more(self):
            self._remove_more_button()
            page = self._pending_groups[:RESULTS_PAGE_SIZE]
            self._pending_groups = self._pending_groups[RESULTS_PAGE_SIZE:]
            self._render_total = len(page)
            self._render_track_progress = False
            self.status_label.config(text=f"Processing 0 of {len(page)} groups…")
            self.stop_btn.state(["!disabled"])
            self._render_queue = page

        # ---------------- results rendering ----------------
        def _clear_results(self):
            self._render_queue = []
            self._pending_groups = []
            self._groups_total = 0
            self._more_btn = None
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
                sim = getattr(group, "similarity", 100.0)
                mtype = getattr(group, "match_type", "similar")
                if mtype == "exact":
                    conf = "100% (byte-identical)"
                else:
                    conf = f"{sim:g}% match"
                box = ttk.LabelFrame(
                    self.results_frame,
                    text=f"Group {index} — {len(group)} files, "
                         f"{human_size(sum(f.size for f in group))} · {conf}",
                    padding=8)
                box.pack(fill="x", padx=6, pady=5)
                header = ttk.Frame(box)
                header.pack(fill="x")
                ttk.Button(
                    header, text="Compare…",
                    command=lambda g=group, i=index: self._open_compare_window(g, i),
                ).pack(side="right")
                ttk.Button(
                    header, text="Skip",
                    command=lambda g=group: self._dismiss_group(g),
                ).pack(side="right", padx=(0, 4))
                ttk.Button(
                    header, text="Deselect All",
                    command=lambda g=group: self._set_group_selection(g, False),
                ).pack(side="right", padx=(0, 4))
                ttk.Button(
                    header, text="Select All",
                    command=lambda g=group: self._set_group_selection(g, True),
                ).pack(side="right", padx=(0, 4))
                ttk.Label(
                    header,
                    text="Click a thumbnail for Quick Look · double-click to open",
                    foreground="gray",
                ).pack(side="left")
                inner = ttk.Frame(box)
                inner.pack(fill="x", pady=(4, 0))
                thumb_size = self.prefs.thumbnail_size
                for fi in group:
                    cell = ttk.Frame(inner, padding=4)
                    cell.pack(side="left", anchor="n")
                    photo = self._make_thumbnail(fi.path, thumb_size)
                    if photo is not None:
                        img_label = tk.Label(cell, image=photo, cursor="hand2")
                        img_label.pack()
                        img_label.bind(
                            "<Button-1>",
                            lambda e, p=fi.path, w=img_label: self._thumb_click(w, p))
                        img_label.bind(
                            "<Double-Button-1>",
                            lambda e, p=fi.path, w=img_label: self._thumb_double_click(w, p))
                        img_label.bind("<Button-2>",
                                       lambda e, p=fi.path: reveal_in_finder(p))
                        img_label.bind("<Button-3>",
                                       lambda e, p=fi.path: reveal_in_finder(p))
                    is_ref = getattr(fi, "is_reference", False)
                    var = tk.BooleanVar(value=(not is_ref) and fi.path in self._preselect)
                    var.trace_add(
                        "write",
                        lambda *_, p=fi.path, v=var: self._on_check_toggled(p, v))
                    self.check_vars[fi.path] = var
                    name = os.path.basename(fi.path)
                    if len(name) > 24:
                        name = name[:21] + "…"
                    label_text = ("🔒 " + name) if is_ref else name
                    cb = ttk.Checkbutton(cell, text=label_text, variable=var)
                    if is_ref:
                        cb.state(["disabled"])
                    cb.pack(anchor="w")
                    if is_ref:
                        ttk.Label(cell, text="Reference — always kept",
                                  foreground="#1a5fb4").pack(anchor="w")
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
            if self._render_track_progress:
                self.progress.config(maximum=max(self._render_total, 1), value=done)
            if self._render_queue:
                self.status_label.config(
                    text=f"Processing {done} of {self._render_total} groups…")
            else:
                self.status_label.config(text=self._summary_text)
                self.stop_btn.state(["disabled"])
                if self._pending_groups:
                    self._render_more()

        def _make_thumbnail(self, path, size):
            try:
                with Image.open(path) as img:
                    # JPEG fast path: decode at reduced resolution
                    img.draft("RGB", (size * 2, size * 2))
                    img = ImageOps.exif_transpose(img)
                    img.thumbnail((size, size))
                    photo = ImageTk.PhotoImage(img.convert("RGB"))
                self._thumb_refs.append(photo)
                return photo
            except Exception:
                return None

        # ---------------- thumbnail click routing ----------------
        def _thumb_click(self, widget, path):
            # Debounce so a double-click (open) doesn't briefly fire Quick Look
            prior = getattr(widget, "_ql_after_id", None)
            if prior:
                try: self.after_cancel(prior)
                except Exception: pass
            widget._ql_after_id = self.after(
                260, lambda w=widget, p=path: self._thumb_deferred_ql(w, p))

        def _thumb_deferred_ql(self, widget, path):
            widget._ql_after_id = None
            quick_look(path)

        def _thumb_double_click(self, widget, path):
            prior = getattr(widget, "_ql_after_id", None)
            if prior:
                try: self.after_cancel(prior)
                except Exception: pass
                widget._ql_after_id = None
            open_file(path)

        # ---------------- compare window ----------------
        def _open_compare_window(self, group, group_index):
            from datetime import datetime
            win = tk.Toplevel(self)
            sim = getattr(group, "similarity", 100.0)
            mtype = getattr(group, "match_type", "similar")
            conf_txt = ("100% (byte-identical)"
                        if mtype == "exact" else f"{sim:g}% match")
            win.title(f"Compare — Group {group_index} · {len(group)} files · {conf_txt}")
            keeper = pick_keeper(group, self.keep_var.get())

            # per-image cell size: fit screen, cap to 460, floor 220
            screen_w = win.winfo_screenwidth()
            screen_h = win.winfo_screenheight()
            gap = 24                     # padding + border per column
            per_w = max(220, min(460, (screen_w - 140) // max(len(group), 1) - gap))
            cell_h = per_w              # square cell so aspect ratios line up

            outer = ttk.Frame(win, padding=14)
            outer.pack(fill="both", expand=True)

            canvas = tk.Canvas(outer, highlightthickness=0)
            hbar = ttk.Scrollbar(outer, orient="horizontal", command=canvas.xview)
            canvas.configure(xscrollcommand=hbar.set)
            hbar.pack(side="bottom", fill="x")
            canvas.pack(side="top", fill="both", expand=True)
            strip = ttk.Frame(canvas)
            canvas.create_window((0, 0), window=strip, anchor="nw")
            strip.bind(
                "<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

            # keep photo refs pinned to the window
            win._photo_refs = []

            for fi in group:
                col = ttk.Frame(strip, padding=(0, 0, 14, 0))
                col.pack(side="left", anchor="n")

                is_keeper = fi.path == keeper.path
                is_ref = getattr(fi, "is_reference", False)
                if is_ref:
                    border_color = "#1a5fb4"  # blue for Reference
                elif is_keeper:
                    border_color = "#2ea043"  # green for keeper
                else:
                    border_color = "#c8c8c8"

                # Fixed-size cell so mixed portrait/landscape columns stay uniform.
                # Image is centered inside via place().
                cell = tk.Frame(col, background=border_color,
                                width=per_w + 6, height=cell_h + 6)
                cell.pack()
                cell.pack_propagate(False)
                inner_cell = tk.Frame(cell, background="#111111",
                                      width=per_w, height=cell_h)
                inner_cell.place(x=3, y=3)
                inner_cell.pack_propagate(False)

                photo = self._make_compare_image(fi.path, per_w, cell_h)
                if photo is not None:
                    img_lbl = tk.Label(inner_cell, image=photo,
                                       background="#111111", cursor="hand2")
                    img_lbl.place(relx=0.5, rely=0.5, anchor="center")
                    img_lbl.bind("<Button-1>",
                                 lambda e, p=fi.path: quick_look(p))
                    win._photo_refs.append(photo)
                else:
                    tk.Label(inner_cell, text="(preview failed)",
                             background="#111111", foreground="#dddddd"
                             ).place(relx=0.5, rely=0.5, anchor="center")

                name = os.path.basename(fi.path)
                if is_ref:
                    name = "🔒 " + name
                ttk.Label(col, text=name, font=("", 12, "bold"),
                          wraplength=per_w).pack(anchor="w", pady=(8, 0))
                if is_ref:
                    ttk.Label(col, text="Reference — always kept",
                              foreground="#1a5fb4").pack(anchor="w")
                elif is_keeper:
                    ttk.Label(col, text="★ Best per current rule — will be kept",
                              foreground="#2ea043").pack(anchor="w")
                dims = f"{fi.width}×{fi.height}" if fi.width else "—"
                when = datetime.fromtimestamp(fi.mtime).strftime("%Y-%m-%d %H:%M")
                meta = (f"Dims:  {dims}\n"
                        f"Size:  {human_size(fi.size)}\n"
                        f"Date:  {when}")
                ttk.Label(col, text=meta, justify="left",
                          foreground="gray30").pack(anchor="w", pady=(4, 2))

                home = str(Path.home())
                parent = os.path.dirname(fi.path)
                short_parent = ("~" + parent[len(home):]) if parent.startswith(home) else parent
                ttk.Label(col, text=short_parent, foreground="#4a78c4",
                          wraplength=per_w).pack(anchor="w")

                btnrow = ttk.Frame(col)
                btnrow.pack(anchor="w", pady=(6, 0))
                ttk.Button(btnrow, text="Quick Look",
                           command=lambda p=fi.path: quick_look(p)).pack(side="left")
                ttk.Button(btnrow, text="Open",
                           command=lambda p=fi.path: open_file(p)).pack(side="left", padx=(6, 0))
                ttk.Button(btnrow, text="Reveal",
                           command=lambda p=fi.path: reveal_in_finder(p)).pack(side="left", padx=(6, 0))

            footer = ttk.Frame(win, padding=(14, 4, 14, 12))
            footer.pack(fill="x")
            ttk.Label(footer,
                      text=(f"Keep rule: {self.keep_var.get()}   ·   "
                            f"Confidence: {conf_txt}   ·   "
                            f"Click thumbnail for Quick Look"),
                      foreground="gray").pack(side="left")
            ttk.Button(footer, text="Close", command=win.destroy).pack(side="right")
            win.bind("<Escape>", lambda e: win.destroy())

            # size window: fit content but cap to screen
            win.update_idletasks()
            req_w = min(win.winfo_reqwidth() + 24, screen_w - 80)
            req_h = min(win.winfo_reqheight() + 24, screen_h - 120)
            x = max(20, (screen_w - req_w) // 2)
            y = max(30, (screen_h - req_h) // 2 - 20)
            win.geometry(f"{req_w}x{req_h}+{x}+{y}")
            win.transient(self)
            win.lift()
            win.focus_set()

        def _make_compare_image(self, path, max_w, max_h):
            try:
                with Image.open(path) as img:
                    img.draft("RGB", (max_w * 2, max_h * 2))
                    img = ImageOps.exif_transpose(img)
                    img.thumbnail((max_w, max_h))
                    return ImageTk.PhotoImage(img.convert("RGB"))
            except Exception:
                return None

        # ---------------- selection & actions ----------------
        def _set_group_selection(self, group, selected):
            for fi in group:
                if getattr(fi, "is_reference", False):
                    continue
                var = self.check_vars.get(fi.path)
                if var is not None:
                    var.set(selected)

        def _on_check_toggled(self, path, var):
            fi = self.file_by_path.get(path)
            if var.get():
                if fi is None or not getattr(fi, "is_reference", False):
                    self._preselect.add(path)
                else:
                    var.set(False)  # never mark a Reference file
                    return
            else:
                self._preselect.discard(path)
            self._update_selection_label()

        def _update_selection_label(self):
            selected = [p for p in self._preselect if p in self.file_by_path]
            total = sum(self.file_by_path[p].size for p in selected)
            self.selection_label.config(
                text=f"{len(selected)} selected ({human_size(total)})"
                if selected else "")

        def _keep_rule_changed(self, _event=None):
            self.prefs.keep_rule = self.keep_var.get()
            self.prefs.save()

        def auto_select(self):
            # Selection covers every group; unrendered pages pick it up from
            # _preselect when (and if) they are shown.
            marked = compute_auto_selection(self.groups, self.keep_var.get())
            self._preselect = marked
            for path, var in self.check_vars.items():
                var.set(path in marked)
            self._update_selection_label()

        def clear_selection(self):
            self._preselect = set()
            for var in self.check_vars.values():
                var.set(False)
            self._update_selection_label()

        def trash_selected(self):
            selected = sorted(p for p in self._preselect if p in self.file_by_path)
            if not selected:
                messagebox.showinfo(APP_NAME, "Nothing selected.")
                return
            # Never allow deleting an entire group (rendered or not).
            fully_selected = [
                i + 1 for i, g in enumerate(self.groups)
                if all(f.path in self._preselect for f in g)]
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

        def move_selected_to_folder(self):
            selected = sorted(p for p in self._preselect if p in self.file_by_path)
            if not selected:
                messagebox.showinfo(APP_NAME, "Nothing selected.")
                return
            fully_selected = [
                i + 1 for i, g in enumerate(self.groups)
                if all(f.path in self._preselect for f in g)]
            if fully_selected:
                messagebox.showwarning(
                    APP_NAME,
                    "Every file is selected in group(s) "
                    f"{', '.join(map(str, fully_selected))}.\n\n"
                    "Deselect at least one file per group so a copy is kept.")
                return
            target_dir = filedialog.askdirectory(
                title="Move selected files to…", mustexist=True)
            if not target_dir:
                return
            target_dir = os.path.abspath(target_dir)
            if not os.path.isdir(target_dir):
                messagebox.showerror(APP_NAME, "That folder no longer exists.")
                return

            self.status_label.config(text=f"Moving {len(selected)} files…")
            self.update_idletasks()

            moved, failures = [], []
            for src in selected:
                try:
                    dest = _unique_destination(target_dir, os.path.basename(src))
                    shutil.move(src, dest)
                    moved.append(src)
                except Exception as exc:
                    failures.append((src, str(exc)))

            if failures:
                first = failures[0]
                messagebox.showerror(
                    APP_NAME,
                    f"Moved {len(moved)} of {len(selected)} files.\n\n"
                    f"First failure:\n{first[0]}\n→ {first[1]}")
            if not moved:
                self.status_label.config(text="Nothing moved.")
                return
            total = sum(self.file_by_path[p].size for p in moved)
            short = target_dir
            home = str(Path.home())
            if short.startswith(home):
                short = "~" + short[len(home):]
            self._remove_paths_from_results(
                set(moved),
                f"Moved {len(moved)} files ({human_size(total)}) to {short}.")

        def _dismiss_group(self, group):
            paths = {f.path for f in group}
            self._remove_paths_from_results(
                paths,
                f"Dismissed group · {len(self.groups)} groups remaining")

        def _remove_paths_from_results(self, removed, summary):
            new_groups = []
            for group in self.groups:
                remaining_files = [f for f in group if f.path not in removed]
                if len(remaining_files) <= 1:
                    continue
                # Drop groups that are now all-Reference (nothing actionable left).
                if not any(not getattr(f, "is_reference", False)
                           for f in remaining_files):
                    continue
                # Preserve Group attrs (similarity, match_type) if the original was one.
                if isinstance(group, Group):
                    rg = Group(remaining_files)
                    rg.similarity = getattr(group, "similarity", 100.0)
                    rg.match_type = getattr(group, "match_type", "similar")
                    new_groups.append(rg)
                else:
                    new_groups.append(remaining_files)
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
                self._start_rendering(new_groups, summary, track_progress=False)
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
            if not self.prefs.shell_warning_accepted:
                if not messagebox.askokcancel(
                        "Shell — with great power…",
                        "This box runs REAL terminal commands with the same "
                        "power as the Terminal app.\n\n"
                        "There is no undo: commands like rm delete files "
                        "permanently — they do NOT go to the Trash.\n\n"
                        "Only run commands you understand. Run this one?",
                        icon="warning", default="cancel"):
                    return
                self.prefs.shell_warning_accepted = True
                self.prefs.save()
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

        def uninstall_app(self):
            paths = uninstall_paths()
            if not paths:
                messagebox.showinfo(APP_NAME, "Nothing to uninstall — no app "
                                    "files found on this system.")
                return
            listing = "\n".join(f"  •  {p}" for p in paths)
            if not messagebox.askokcancel(
                    "Uninstall Dupe Demon",
                    "This moves everything Dupe Demon put on your system "
                    "to the Trash (restorable until you empty it):\n\n"
                    f"{listing}\n\nUninstall and quit?",
                    icon="warning", default="cancel"):
                return
            error = move_to_trash([str(p) for p in paths])
            if error:
                messagebox.showerror(APP_NAME,
                                     f"Could not complete uninstall:\n{error}")
                return
            messagebox.showinfo(
                "Uninstall complete",
                f"{APP_NAME} and its files are in the Trash. Goodbye! 😈")
            self._quit()

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
        # keep the test cache away from the real one
        global CACHE_FILE
        real_cache_file = CACHE_FILE
        CACHE_FILE = tmpdir / "hash_cache.db"
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

        # ---- banding matcher (large-library path) ---------------------------
        global PAIRWISE_LIMIT
        prefs.match_rotated = False
        prefs.match_mode = "similar"
        baseline, _ = run_scan([str(tmpdir)], prefs)
        PAIRWISE_LIMIT = 0        # force the banding path on this small set
        banded, _ = run_scan([str(tmpdir)], prefs)
        PAIRWISE_LIMIT = 2500
        canon = lambda gs: sorted(sorted(f.path for f in g) for g in gs)
        assert canon(baseline) == canon(banded), \
            (canon(baseline), canon(banded))
        print("  banding matcher OK — same groups as pairwise comparison")

        # ---- hash cache ----------------------------------------------------
        global compute_perceptual_hashes, _sha256
        prefs.match_rotated = False
        CACHE_FILE.unlink(missing_ok=True)

        real_phash = compute_perceptual_hashes
        calls = {"phash": 0, "sha": 0}

        def counting_phash(*a, **k):
            calls["phash"] += 1
            return real_phash(*a, **k)
        compute_perceptual_hashes = counting_phash

        groups_first, n = run_scan([str(tmpdir)], prefs)
        assert calls["phash"] == n, (calls, n)          # cold cache: hash everything
        assert CACHE_FILE.exists()
        assert (CACHE_FILE.stat().st_mode & 0o777) == 0o600, \
            oct(CACHE_FILE.stat().st_mode)
        calls["phash"] = 0
        groups_second, _ = run_scan([str(tmpdir)], prefs)
        assert calls["phash"] == 0, calls               # warm cache: hash nothing
        key = lambda gs: sorted(sorted(f.path for f in g) for g in gs)
        assert key(groups_first) == key(groups_second)
        print("  similar-mode cache OK — warm re-scan hashed 0 files, same results")

        # editing a file invalidates only that entry
        img = gradient_image(shift=64)
        img.save(tmpdir / "unique2.png")                # new content, new mtime
        os.utime(tmpdir / "unique2.png", (time_now := __import__("time").time(),
                                          time_now))
        calls["phash"] = 0
        run_scan([str(tmpdir)], prefs)
        assert calls["phash"] == 1, calls
        print("  cache invalidation OK — only the edited file was re-hashed")

        real_sha = _sha256

        def counting_sha(*a, **k):
            calls["sha"] += 1
            return real_sha(*a, **k)
        _sha256 = counting_sha

        prefs.match_mode = "exact"
        groups_first, _ = run_scan([str(tmpdir)], prefs)
        assert calls["sha"] > 0
        calls["sha"] = 0
        groups_second, _ = run_scan([str(tmpdir)], prefs)
        assert calls["sha"] == 0, calls
        assert key(groups_first) == key(groups_second)
        print("  exact-mode cache OK — warm re-scan read 0 files, same results")

        compute_perceptual_hashes = real_phash
        _sha256 = real_sha
        CACHE_FILE = real_cache_file

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
