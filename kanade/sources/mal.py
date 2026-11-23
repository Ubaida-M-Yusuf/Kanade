"""MyAnimeList API v2: genres (including themes such as Ecchi, Gore, Psychological) and the age rating.

Needs the user's own client ID (Settings > Metadata sources); it is kept only in the local settings."""
from __future__ import annotations

from ..net import http

BASE = "https://api.myanimelist.net/v2/anime/"


class Rejected(Exception):
    """MyAnimeList refused the client ID (wrong, deleted or not yet active)."""


def anime_info(mal_id: int, client_id: str) -> dict | None:
    """{"genres": [...], "rating": "pg_13" / "r" / "r+" / "rx" / ...}; None when MyAnimeList has no such anime."""
    try:
        d = http().request_json("GET", BASE + str(int(mal_id)), params={"fields": "genres,rating"},
                                headers={"X-MAL-CLIENT-ID": client_id}, max_age=0, timeout=20, retries=2)
    except Exception as e:
        if "HTTP 401" in str(e) or "HTTP 403" in str(e):
            raise Rejected(str(e)) from e
        raise
    if d is None:
        return None
    return {"genres": [g["name"] for g in d.get("genres") or [] if g.get("name")], "rating": d.get("rating") or ""}
