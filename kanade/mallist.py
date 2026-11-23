"""The user's own MyAnimeList anime list (watching, completed, on hold, dropped, plan to watch), for filtering.

The profile name comes from Settings (a profile / list link or just the name). A public list is read the way
myanimelist.net's own list page reads it (no client ID needed); the official API with the user's client ID is the
fallback. It is stored locally in the mal_list table and refreshed when it is a few hours old.
"""
from __future__ import annotations

import logging
import re
import time

from . import db
from .config import settings
from .sources import mal

log = logging.getLogger("kanade.mallist")
STATUSES = ["watching", "completed", "on_hold", "dropped", "plan_to_watch"]
MAX_AGE = 3 * 3600              # refreshed at the next library check once it is older than this


def parse_user(text: str) -> str:
    """'https://myanimelist.net/profile/Name', '.../animelist/Name?status=2' or 'Name' -> 'Name' ('' if unusable)."""
    text = (text or "").strip()
    m = re.search(r"myanimelist\.net/(?:profile|animelist|mangalist)/([A-Za-z0-9_-]{2,32})", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9_-]{2,32}", text) else ""


def status() -> dict:
    r = db.q1("SELECT COUNT(*) n FROM mal_list")
    lib = db.q1("SELECT COUNT(DISTINCT a.id) n FROM anime a JOIN mal_list m ON m.mal_id=a.mal_id "
                "WHERE a.id IN (SELECT anime_id FROM v_video WHERE missing=0)")
    return {"user": settings().get("mal_user") or "", "entries": r["n"], "in_library": lib["n"],
            "at": float(db.meta_get("mal_list_at") or 0), "error": db.meta_get("mal_list_error") or ""}


def refresh(force: bool = False) -> int:
    """Read the list again if it is older than MAX_AGE (or force). Returns the number of entries (-1: not refreshed).
    Raises with a readable message when the list can't be read."""
    user = settings().get("mal_user") or ""
    cid = (settings().get("mal_client_id") or "").strip()
    if not user:
        with db.tx() as c:
            c.execute("DELETE FROM mal_list")
        return 0
    fresh = time.time() - float(db.meta_get("mal_list_at") or 0) < MAX_AGE
    if not force and fresh and db.meta_get("mal_list_user") == user:
        return -1
    try:
        entries = mal.user_list_public(user)            # raises on a private list / unknown name / no network
    except Exception:
        if not cid:
            raise
        entries = mal.user_list(user, cid)              # the official API, when a client ID is set
    with db.tx() as c:                                  # one transaction: the old list stays if anything fails
        c.execute("DELETE FROM mal_list")
        c.executemany("INSERT OR REPLACE INTO mal_list(mal_id, status, score) VALUES(?,?,?)",
                      [(k, v["status"], v.get("score") or 0) for k, v in entries.items()])
    db.meta_set("mal_list_at", time.time())
    db.meta_set("mal_list_user", user)
    db.meta_set("mal_list_error", "")
    db.bump_library_version()
    log.info("MyAnimeList list read: %s entries", len(entries))
    return len(entries)
