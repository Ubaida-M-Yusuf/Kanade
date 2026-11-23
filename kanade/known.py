"""Identifications shipped with Kanade (data/known_videos.json).

Some AnimeThemes videos (mostly game and visual-novel openings in the archive's misc folder) aren't linked to a series
in the AnimeThemes catalogue, so every user would otherwise have to identify them by hand. The shipped list holds
those identifications. One is used only for an exact copy of the file (same name AND byte size) that is still
unidentified; a video the user identified or corrected is never touched, and the result can be edited like any
other identification.
"""
from __future__ import annotations

import json
import logging

from . import db, library
from .config import resource_dir

log = logging.getLogger("kanade.known")
_cache: dict | None = None


def _data() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads((resource_dir() / "data" / "known_videos.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("shipped identifications not available: %s", e)
            _cache = {"series": [], "videos": []}
    return _cache


def apply() -> int:
    """Identify still-unidentified exact copies of the known videos. Returns how many were identified."""
    d = _data()
    if not d["videos"]:
        return 0
    known = {(v["file"].lower(), int(v["size"])): v["series"] for v in d["videos"]}
    names = sorted({k[0] for k in known})
    rows = []
    for i in range(0, len(names), 500):                 # only videos nobody has identified or corrected
        chunk = names[i:i + 500]
        rows += db.q("SELECT v.id, v.filename, v.size FROM video v LEFT JOIN user_override o ON o.video_id=v.id "
                     f"WHERE lower(v.filename) IN ({','.join('?' * len(chunk))}) AND v.missing=0 "
                     "AND v.status IN ('pending','review') AND o.anime_id IS NULL", chunk)
    by_series: dict[int, list[int]] = {}
    for r in rows:
        s = known.get((r["filename"].lower(), int(r["size"] or 0)))
        if s is not None:
            by_series.setdefault(s, []).append(r["id"])
    n = 0
    for s, ids in by_series.items():
        info = d["series"][s]
        # the same series may already exist (made by hand, or by an earlier run): reuse it instead of a duplicate
        row = db.q1("SELECT id FROM anime WHERE title_romaji=? AND COALESCE(format,'')=? ORDER BY id LIMIT 1",
                    (info["title_romaji"], info.get("format") or ""))
        target = {"anime_id": row["id"]} if row else {**info, "source": "known"}
        try:
            library.assign(ids, target)
            n += len(ids)
        except Exception as e:                           # one bad entry must not stop the others
            log.warning("shipped identification for %s failed: %s", info.get("title_romaji"), e)
    if n:
        log.info("identified %s video(s) from the shipped list", n)
        db.bump_library_version()
    return n
