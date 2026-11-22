"""AniList GraphQL client (secondary source / cross-check / search)."""
from __future__ import annotations

from ..net import http

URL = "https://graphql.anilist.co"

MEDIA_FIELDS = """
  id idMal format season seasonYear startDate { year }
  title { romaji english native userPreferred }
  synonyms
  coverImage { large extraLarge }
"""

SEARCH_Q = "query($s:String,$p:Int){ Page(perPage:$p){ media(search:$s, type:ANIME){" + MEDIA_FIELDS + "} } }"
BY_MAL_Q = "query($ids:[Int]){ Page(perPage:50){ media(idMal_in:$ids, type:ANIME){" + MEDIA_FIELDS + "} } }"


def _post(query, variables, max_age=30 * 86400):
    d = http().request_json("POST", URL, json_body={"query": query, "variables": variables},
                            max_age=max_age, timeout=25, retries=3)
    if not d:
        return []
    if d.get("errors") and not d.get("data"):
        raise RuntimeError(d["errors"][0].get("message"))
    return ((d.get("data") or {}).get("Page") or {}).get("media") or []


def search(text: str, per_page: int = 10):
    return _post(SEARCH_Q, {"s": text, "p": per_page})


def by_mal_ids(ids: list[int]):
    return _post(BY_MAL_Q, {"ids": ids})


GENRE_Q = ("query($ids:[Int]){ Page(perPage:50){ media(%s:$ids, type:ANIME){ id idMal genres "
           "tags{ name rank isMediaSpoiler isGeneralSpoiler } } } }")


def genres_batch(ids: list[int], by: str = "id") -> list[dict]:
    """Genres and tags for up to 50 anime, by AniList id (by='id') or MAL id (by='idMal')."""
    field = "id_in" if by == "id" else "idMal_in"
    return _post(GENRE_Q % field, {"ids": ids}, max_age=60 * 86400)


CONTENT_Q = ("query($ids:[Int]){ Page(perPage:50){ media(%s:$ids, type:ANIME){ id idMal isAdult genres "
             "tags{ name rank category isAdult } } } }")


def content_batch(ids: list[int], by: str = "id") -> list[dict]:
    """isAdult, genres and categorised tags for up to 50 anime (for content ratings)."""
    field = "id_in" if by == "id" else "idMal_in"
    return _post(CONTENT_Q % field, {"ids": ids}, max_age=30 * 86400)


RELATIONS_Q = ("query($ids:[Int]){ Page(perPage:50){ media(%s:$ids, type:ANIME){ id idMal relations { edges { "
               "relationType(version:2) node { id idMal type format season seasonYear startDate { year month } "
               "title { romaji english } } } } } } }")


def relations_batch(ids: list[int], by: str = "id") -> list[dict]:
    """Sequels, prequels, side stories... for up to 50 anime (by AniList id or MAL id)."""
    field = "id_in" if by == "id" else "idMal_in"
    return _post(RELATIONS_Q % field, {"ids": ids}, max_age=30 * 86400)


def clean_relations(m: dict) -> dict:
    """{"al": own AniList id, "edges": [...]} with only other anime (no manga / novels / character cameos)."""
    edges = []
    for e in (m.get("relations") or {}).get("edges") or []:
        n = e.get("node") or {}
        rel = e.get("relationType")
        if n.get("type") != "ANIME" or not n.get("id") or rel in (None, "CHARACTER"):
            continue
        sd = n.get("startDate") or {}
        t = n.get("title") or {}
        edges.append({"rel": rel, "al": n["id"], "mal": n.get("idMal"), "format": n.get("format"),
                      "season": n.get("season"), "year": n.get("seasonYear") or sd.get("year"),
                      "month": sd.get("month"), "title": t.get("romaji") or t.get("english"), "en": t.get("english")})
    return {"al": m.get("id"), "edges": edges}


def clean_tags(m: dict, min_rank: int = 60) -> list[str]:
    """Keep strong, non-spoiler tags (AniList ranks how well a tag fits, 0-100)."""
    out = []
    for t in m.get("tags") or []:
        if t.get("isMediaSpoiler") or t.get("isGeneralSpoiler"):
            continue
        if (t.get("rank") or 0) >= min_rank and t.get("name"):
            out.append(t["name"])
    return out


def to_candidate(m: dict) -> dict:
    t = m.get("title") or {}
    year = m.get("seasonYear") or (m.get("startDate") or {}).get("year")
    return {
        "source": "anilist",
        "anilist_id": m.get("id"),
        "mal_id": m.get("idMal"),
        "title_romaji": t.get("romaji") or t.get("userPreferred") or "",
        "title_english": t.get("english"),
        "title_japanese": t.get("native"),
        "synonyms": m.get("synonyms") or [],
        "year": year,
        "season": (m.get("season") or "").title() or None,
        "format": m.get("format"),
        "cover_url": (m.get("coverImage") or {}).get("extraLarge") or (m.get("coverImage") or {}).get("large"),
    }
