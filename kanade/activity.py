"""Tracks what the user is doing so background work can step aside.

* Browsing (clicks, searches, scrolling) does NOT pause anything: pages and thumbnails come from the app's own
  disk, so background work on the archive disk doesn't slow the UI down.
* A video starting (hover preview, in-app player): background readers wait a few seconds so the video's first
  seconds load instantly. Opening a video in VLC: they wait longer (VLC reads the start of the file).
"""
from __future__ import annotations

import sys
import threading
import time

MEDIA_QUIET_SECONDS = 4.0
PLAY_QUIET_SECONDS = 15.0

_last_ui = 0.0
_last_media = 0.0
_last_play = 0.0


_stamp_written = {}


def _stamp(name: str):
    """Let the separate For You engine process know too (it checks these files' timestamps)."""
    now = time.monotonic()
    if now - _stamp_written.get(name, 0) < 1.0:
        return
    _stamp_written[name] = now
    try:
        from .config import data_dir
        p = data_dir() / "engine" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as fh:
            fh.write("1")
    except OSError:
        pass


def touch_ui():
    global _last_ui
    _last_ui = time.monotonic()


def touch_media():
    """A video is being streamed to the app (preview or player)."""
    global _last_media
    _last_media = time.monotonic()
    _stamp("media.stamp")


def touch_play():
    """A video was opened in VLC."""
    global _last_play
    _last_play = time.monotonic()
    _stamp("play.stamp")


def busy() -> bool:
    now = time.monotonic()
    return now - _last_media < MEDIA_QUIET_SECONDS or now - _last_play < PLAY_QUIET_SECONDS


def yield_to_user(stop_event: threading.Event | None = None, max_wait: float = 120.0):
    """Block a background worker while the user is interacting (bounded, so work always progresses)."""
    waited = 0.0
    while busy() and waited < max_wait:
        if stop_event is not None and stop_event.is_set():
            return
        time.sleep(0.25)
        waited += 0.25


def lower_thread_priority():
    """Run the calling thread below normal priority."""
    from .osutil import lower_thread_priority as lower
    lower()
