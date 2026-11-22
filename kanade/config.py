"""Paths, settings (config.json) and logging."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from . import APP_NAME

_lock = threading.RLock()


def data_dir() -> Path:
    """%LOCALAPPDATA%\\Kanade on Windows, ~/.local/share/Kanade on Linux, ~/Library/Application Support/Kanade on
    macOS (override with the KANADE_HOME environment variable)."""
    override = os.environ.get("KANADE_HOME")
    if override:
        base = Path(override)
    else:
        from .osutil import user_data_root
        base = user_data_root() / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def resource_dir() -> Path:
    """Folder holding bundled resources (ui/, ffmpeg/). Works from source and from PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


DEFAULTS = {
    "archive_root": "",
    "vlc_path": "",
    "ffmpeg_path": "",
    "ffprobe_path": "",
    "ollama_enabled": True,
    "ollama_url": "http://localhost:11434",
    "ollama_model": "",
    "workers": 4,
    "thumb_width": 480,
    "accept_confidence": 0.85,
    "hover_preview": True,
    "use_jikan": True,
    "use_anilist": True,
    "catalog_max_age_days": 14,
    "card_size": "m",
    "auto_rescan_minutes": 30,   # re-check the archive for new/changed files while running (0 = off)
    "taste_folders": [],          # folders of liked videos for "For You" (read-only)
    "taste_use_favorites": True,  # favourites count as likes too
    "taste_group_names": {},      # user's own names for taste groups (key: the group's 3 most typical videos)
}


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self._data = dict(DEFAULTS)
        self.load()

    def load(self):
        with _lock:
            if self.path.exists():
                try:
                    stored = json.loads(self.path.read_text(encoding="utf-8-sig"))
                    if isinstance(stored, dict):
                        self._data.update(stored)
                except Exception:  # corrupt config -> keep defaults, keep a backup
                    logging.getLogger("kanade").exception("config.json unreadable, using defaults")
                    try:
                        self.path.replace(self.path.with_suffix(".json.bad"))
                    except OSError:
                        pass
            self.save()

    def save(self):
        with _lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def get(self, key, default=None):
        with _lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def __getitem__(self, key):
        return self.get(key)

    def update(self, values: dict):
        with _lock:
            for k, v in values.items():
                if k in DEFAULTS:
                    expected = type(DEFAULTS[k])
                    if expected is dict:
                        v = {str(a): str(b) for a, b in (v or {}).items() if str(b).strip()}
                    elif expected is list:
                        v = [str(x).strip() for x in (v or []) if str(x).strip()]
                    elif expected is bool:
                        v = bool(v)
                    elif expected is int:
                        v = int(v)
                    elif expected is float:
                        v = float(v)
                    else:
                        v = "" if v is None else str(v)
                    self._data[k] = v
            self.save()

    def as_dict(self):
        with _lock:
            return dict(self._data)


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings(data_dir() / "config.json")
    return _settings


def setup_logging(console: bool = True) -> logging.Logger:
    log_dir = data_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    logger = logging.getLogger("kanade")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)-12s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(log_dir / "kanade.log", maxBytes=4_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if console and sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        logger.addHandler(sh)
    return logger
