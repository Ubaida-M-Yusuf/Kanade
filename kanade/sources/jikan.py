"""Jikan (unofficial MyAnimeList API, https://api.jikan.moe/v4) client."""
from __future__ import annotations

import re

from ..net import http

BASE = "https://api.jikan.moe/v4"


def get_anime(mal_id: int):
    d = http().request_json("GET", f"{BASE}/anime/{int(mal_id)}", max_age=60 * 86400, timeout=15, retries=2)
    return (d or {}).get("data")


def search(text: str, limit: int = 10):
    d = http().request_json("GET", f"{BASE}/anime", params={"q": text, "limit": limit, "sfw": "false"},
                            max_age=30 * 86400, timeout=15, retries=2)
    return (d or {}).get("data") or []


_THEME_RE = re.compile(r'^\s*(?:#?(\d+)\s*:\s*)?"(.+?)"\s+by\s+(.+?)(?:\s*\((?:eps?|episodes?)\b.*\))?\s*$', re.I)


def theme_credits(mal_id: int) -> dict:
    """{('OP', 1): (song, artist), ...} parsed from MAL's theme list strings like '1: "Song" by Artist (eps 1-12)'."""
    d = http().request_json("GET", f"{BASE}/anime/{int(mal_id)}/themes", max_age=60 * 86400, timeout=15, retries=2)
    data = (d or {}).get("data") or {}
    out = {}
    for key, typ in (("openings", "OP"), ("endings", "ED")):
        for i, line in enumerate(data.get(key) or [], 1):
            m = _THEME_RE.match(line or "")
            if m:
                num = int(m.group(1)) if m.group(1) else i
                song = re.sub(r"\s*\(.*?\)\s*$", "", m.group(2)).strip()
                out[(typ, num)] = (song, m.group(3).strip())
    return out


def to_candidate(a: dict) -> dict:
    titles = a.get("titles") or []
    by_type = {}
    for t in titles:
        by_type.setdefault(t.get("type"), t.get("title"))
    synonyms = [t.get("title") for t in titles if t.get("type") == "Synonym"]
    img = ((a.get("images") or {}).get("webp") or {}).get("large_image_url") or \
          ((a.get("images") or {}).get("jpg") or {}).get("large_image_url")
    return {
        "source": "jikan",
        "mal_id": a.get("mal_id"),
        "title_romaji": a.get("title") or by_type.get("Default") or "",
        "title_english": a.get("title_english") or by_type.get("English"),
        "title_japanese": a.get("title_japanese") or by_type.get("Japanese"),
        "synonyms": synonyms + (a.get("title_synonyms") or []),
        "year": a.get("year") or ((a.get("aired") or {}).get("prop") or {}).get("from", {}).get("year"),
        "season": (a.get("season") or "").title() or None,
        "format": a.get("type"),
        "cover_url": img,
    }
