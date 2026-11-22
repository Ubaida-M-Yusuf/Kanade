"""AnimeThemes.moe client (GraphQL API at https://graphql.animethemes.moe/).

The archive's filenames are AnimeThemes video basenames, so the best strategy is to
mirror the whole catalogue (~5,000 anime, ~51 requests of 100) into the local DB
and match every file offline by exact basename.
"""
from __future__ import annotations

import logging
import re

from ..net import http

log = logging.getLogger("kanade.animethemes")

GRAPHQL_URL = "https://graphql.animethemes.moe/"
REST_URL = "https://api.animethemes.moe"

ANIME_FIELDS = """
  id slug year season format
  title { romaji english native }
  synonyms { text }
  images { nodes { facet link } }
  resources { nodes { site externalId } }
  animethemes {
    type sequence slug
    song { title { romaji native } performances { as artist { name { main } } } }
    animethemeentries {
      version episodes nsfw spoiler
      videos { nodes { basename resolution source tags overlap } }
    }
  }
"""

CATALOG_QUERY = "query($page:Int!,$first:Int!){ animePagination(first:$first, page:$page) { " \
                "paginatorInfo { total lastPage currentPage hasMorePages } data {" + ANIME_FIELDS + "} } }"

VIDEO_QUERY = "query($b:String!){ videoPagination(basename:$b, first: 5) { data { basename " \
              "animethemeentries { nodes { version episodes animetheme { type sequence slug " \
              "song { title { romaji native } performances { as artist { name { main } } } } " \
              "anime {" + ANIME_FIELDS + "} } } } } } }"

SEARCH_QUERY = "query($s:String!){ search(search:$s, first: 12, page: 1) { anime {" + ANIME_FIELDS + "} } }"


def _gql(query: str, variables: dict, max_age=7 * 86400, timeout=90):
    data = http().request_json("POST", GRAPHQL_URL, json_body={"query": query, "variables": variables},
                               max_age=max_age, timeout=timeout)
    if data is None:
        return None
    if data.get("errors"):
        raise RuntimeError(f"AnimeThemes GraphQL error: {data['errors'][0].get('message')}")
    return data.get("data")


def fetch_catalog_page(page: int, first: int = 100, max_age=0):
    d = _gql(CATALOG_QUERY, {"page": page, "first": first}, max_age=max_age)
    pag = d["animePagination"]
    return pag["paginatorInfo"], pag["data"]


def lookup_video(basename: str):
    """Look up one video by its basename (with .webm). Returns list of (entry, theme, anime)."""
    if not basename.lower().endswith(".webm"):
        basename += ".webm"
    d = _gql(VIDEO_QUERY, {"b": basename}, max_age=30 * 86400)
    out = []
    for v in (d or {}).get("videoPagination", {}).get("data", []):
        if v["basename"].lower() != basename.lower():
            continue
        for ent in (v.get("animethemeentries") or {}).get("nodes", []):
            th = ent.get("animetheme") or {}
            out.append((ent, th, th.get("anime")))
    return out


def search_anime(text: str):
    d = _gql(SEARCH_QUERY, {"s": text}, max_age=30 * 86400)
    anime = ((d or {}).get("search") or {}).get("anime") or []
    if isinstance(anime, dict):  # tolerate a paginated shape
        anime = anime.get("data") or anime.get("nodes") or []
    return anime


# --- helpers to flatten the catalogue structures -------------------------

def theme_number(slug: str | None, sequence) -> tuple[int, str]:
    m = re.match(r"(?:OP|ED)(\d*)([a-z]?)", slug or "", re.IGNORECASE)
    num = int(m.group(1)) if m and m.group(1) else (sequence or 1)
    part = (m.group(2) if m else "") or ""
    return int(num), part.lower()


def artists_text(song: dict | None) -> str:
    if not song:
        return ""
    names = []
    for p in song.get("performances") or []:
        name = ((p.get("artist") or {}).get("name") or {}).get("main")
        if name:
            if p.get("as"):
                name = f"{name} (as {p['as']})"
            if name not in names:
                names.append(name)
    return ", ".join(names)


def song_title(song: dict | None) -> str:
    if not song:
        return ""
    t = song.get("title") or {}
    return t.get("romaji") or t.get("native") or ""


def anime_summary(a: dict) -> dict:
    """Flatten an AnimeThemes anime node into the fields our `anime` table stores."""
    res = {n["site"]: n.get("externalId") for n in (a.get("resources") or {}).get("nodes", []) if n}
    imgs = {n["facet"]: n.get("link") for n in (a.get("images") or {}).get("nodes", []) if n}
    title = a.get("title") or {}
    return {
        "at_id": a.get("id"),
        "at_slug": a.get("slug"),
        "title_romaji": title.get("romaji") or a.get("slug") or "",
        "title_english": title.get("english"),
        "title_japanese": title.get("native"),
        "synonyms": [s["text"] for s in a.get("synonyms") or [] if s and s.get("text")],
        "year": a.get("year"),
        "season": (a.get("season") or "").title() or None,
        "format": a.get("format"),
        "mal_id": res.get("MAL"),
        "anilist_id": res.get("ANILIST"),
        "cover_url": imgs.get("LARGE_COVER") or imgs.get("SMALL_COVER"),
    }


def iter_videos(a: dict):
    """Yield one flat dict per video file in an AnimeThemes anime node."""
    for th in a.get("animethemes") or []:
        num, part = theme_number(th.get("slug"), th.get("sequence"))
        song = th.get("song")
        for ent in th.get("animethemeentries") or []:
            for v in (ent.get("videos") or {}).get("nodes", []):
                if not v or not v.get("basename"):
                    continue
                base = v["basename"]
                stem = base.rsplit(".", 1)[0]
                prefix = re.split(r"-(?:OP|ED)", stem, maxsplit=1, flags=re.IGNORECASE)[0]
                yield {
                    "basename": base,
                    "basename_l": base.lower(),
                    "prefix_l": prefix.lower(),
                    "type": th.get("type") if th.get("type") in ("OP", "ED") else ("OP" if "OP" in (th.get("slug") or "") else "ED"),
                    "number": num,
                    "part": part,
                    "version": ent.get("version") or 1,
                    "slug": th.get("slug"),
                    "song_title": song_title(song),
                    "artists": artists_text(song),
                    "episodes": ent.get("episodes"),
                    "resolution": v.get("resolution"),
                    "source": v.get("source"),
                    "tags": v.get("tags"),
                }
