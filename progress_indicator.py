"""Reusable busy spinner for Tkinter apps — playback-only slim build.

`ProgressIndicator` plays back a list of same-size RGBA frames on a loop; it
has no opinion about how those frames were made. This is the trimmed variant
bundled into consumer apps that only play a pre-rendered frame set (the
cooliosis spinner) rather than synthesizing frames from a source image, so
it drops the numpy-dependent generators (rotate/explode/pulse) that no
consumer currently calls — no new dependency for something never used.
The full module with all four frame generators lives at
Python-Projects/ProgressIndicator/progress_indicator.py and
Python-Projects/cc/progress_indicator.py.

    from progress_indicator import ProgressIndicator, sequence_frames

    frames = sequence_frames("assets/progress_frames")
    spinner = ProgressIndicator(parent, frames, fps=50)
    spinner.pack()
    spinner.start()
    ...
    spinner.stop()
"""

from pathlib import Path

import tkinter as tk
from PIL import Image, ImageTk


class ProgressIndicator(tk.Label):
    """A tk.Label that loops through a precomputed list of RGBA frames."""

    def __init__(self, parent, frames, fps=24, **label_kwargs):
        if not frames:
            raise ValueError("frames must be a non-empty list of PIL images")

        self._photo_frames = [ImageTk.PhotoImage(f) for f in frames]
        self._interval_ms = max(1, round(1000 / fps))
        self._index = 0
        self._running = False
        self._after_id = None

        label_kwargs.setdefault("borderwidth", 0)
        label_kwargs.setdefault("highlightthickness", 0)
        super().__init__(parent, image=self._photo_frames[0], **label_kwargs)
        self.image = self._photo_frames[0]  # keep a live ref, Tk drops GC'd images

    def start(self):
        if self._running:
            return
        self._running = True
        self._tick()

    def stop(self, reset=True):
        self._running = False
        if self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None
        if reset:
            self._index = 0
            self._set_frame(0)

    @property
    def running(self):
        return self._running

    def _tick(self):
        if not self._running:
            return
        self._index = (self._index + 1) % len(self._photo_frames)
        self._set_frame(self._index)
        self._after_id = self.after(self._interval_ms, self._tick)

    def _set_frame(self, index):
        frame = self._photo_frames[index]
        self.configure(image=frame)
        self.image = frame


def sequence_frames(frames_dir, size=None):
    """Load an already-rendered frame sequence (numbered PNGs, e.g.
    frame_000.png, frame_001.png, ...) in sorted order."""
    frames_dir = Path(frames_dir)
    paths = sorted(frames_dir.glob("frame_*.png"))
    if not paths:
        raise ValueError(f"no frame_*.png files found in {frames_dir}")

    frames = []
    for p in paths:
        img = Image.open(p).convert("RGBA")
        if size is not None:
            img = img.resize((size, size), Image.LANCZOS)
        frames.append(img)
    return frames
