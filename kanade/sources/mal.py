"""MyAnimeList API v2: genres (including themes such as Ecchi, Gore, Psychological) and the age rating.

Needs the user's own client ID (Settings > Metadata sources); it is kept only in the local settings."""
from __future__ import annotations

from ..net import http

BASE = "https://api.myanimelist.net/v2/anime/"


class Rejected(Exception):
    """MyAnimeList refused the client ID (wrong, deleted or not yet active)."""


class ListError(Exception):
    """The user's anime list can't be read (private, unknown name...)."""


PUBLIC_STATUS = {1: "watching", 2: "completed", 3: "on_hold", 4: "dropped", 6: "plan_to_watch"}


def user_list_public(user: str) -> dict[int, dict]:
    """A public anime list as myanimelist.net's own list page loads it (no client ID needed), 300 per page."""
    import requests
    out, offset = {}, 0
    for _ in range(100):
        r = requests.get(f"https://myanimelist.net/animelist/{user}/load.json", params={"status": 7, "offset": offset},
                         timeout=30)
        if r.status_code in (400, 401, 403, 404):
            raise ListError(f"the anime list of '{user}' can't be read (is the name right, and is the list public?)")
        r.raise_for_status()
        rows = r.json()
        for e in rows:
            st = PUBLIC_STATUS.get(e.get("status"))
            if e.get("anime_id") and st:
                out[int(e["anime_id"])] = {"status": st, "score": e.get("score") or 0}
        if len(rows) < 300:
            break
        offset += 300
    return out


def user_list(user: str, client_id: str) -> dict[int, dict]:
    """Same through the official API (needs the client ID).
    {mal_id: {"status": "completed" / "watching" / "on_hold" / "dropped" / "plan_to_watch", "score": 0-10}}."""
    import requests
    out, url = {}, f"https://api.myanimelist.net/v2/users/{user}/animelist"
    params = {"fields": "list_status", "limit": 1000, "nsfw": "true"}
    for _ in range(50):                                  # 1,000 per page
        r = requests.get(url, params=params, headers={"X-MAL-CLIENT-ID": client_id}, timeout=30)
        if r.status_code == 404:
            raise ListError(f"MyAnimeList has no user called '{user}'")
        if r.status_code == 403:
            raise ListError(f"the anime list of '{user}' is private (or the client ID was refused)")
        r.raise_for_status()
        d = r.json()
        for e in d.get("data") or []:
            node, st = e.get("node") or {}, e.get("list_status") or {}
            if node.get("id") and st.get("status"):
                out[int(node["id"])] = {"status": st["status"], "score": st.get("score") or 0}
        url, params = (d.get("paging") or {}).get("next"), None
        if not url:
            break
    return out


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
