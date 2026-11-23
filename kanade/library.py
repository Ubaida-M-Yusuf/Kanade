"""Read/write operations used by the UI: listings, details, favourites, corrections, playback."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time

from . import catalog, db, matcher, osutil, search
from .config import settings
from .parser import parse_filename
from .sources import anilist, animethemes as at, jikan

log = logging.getLogger("kanade.library")


# ------------------------------------------------------------- listings

def list_videos() -> list[list]:
    """Compact rows for the whole library (the UI filters/sorts client-side)."""
    rows = db.q("""SELECT id, anime_id, title, title_english, type, number, part, version, song_title, artists,
                   year, folder, folder_season, fav, thumb_state, date_added, status, confidence, duration,
                   missing, has_override, season, anime_year, cover_path IS NOT NULL AS has_cover, fav_at, p_tags,
                   width, height, media_at, nsfw, rating, theme_id, rating_at
                   FROM v_video WHERE missing=0""")
    return [list(r) for r in rows]


VIDEO_COLS = ["id", "anime_id", "title", "title_english", "type", "number", "part", "version", "song", "artists",
              "year", "folder", "folder_season", "fav", "thumb", "added", "status", "conf", "duration", "missing",
              "override", "season", "anime_year", "has_cover", "fav_at", "tags", "width", "height", "tv", "nsfw",
              "rating", "theme_id", "rating_at"]


def list_anime() -> list[dict]:
    rows = db.q("""WITH v AS (
                     SELECT anime_id, SUM(type='OP') AS ops, SUM(type='ED') AS eds, COUNT(*) AS videos,
                            SUM(fav) AS favs, MAX(date_added) AS added, GROUP_CONCAT(DISTINCT folder) AS folders
                     FROM v_video WHERE missing=0 AND anime_id IS NOT NULL GROUP BY anime_id)
                   SELECT a.id, COALESCE(ao.title_romaji, a.title_mal, a.title_romaji) AS title,
                   COALESCE(ao.title_english, a.title_english) AS title_english,
                   COALESCE(ao.title_japanese, a.title_japanese) AS title_japanese,
                   COALESCE(ao.year, a.year) AS year, COALESCE(ao.season, a.season) AS season, a.format,
                   a.cover_path IS NOT NULL AS has_cover, v.ops, v.eds, v.videos, v.favs, v.added, v.folders,
                   a.genres, a.tags, a.suggestive, a.explicit
                   FROM v JOIN anime a ON a.id = v.anime_id
                   LEFT JOIN anime_override ao ON ao.anime_id = a.id""")
    return [dict(r) for r in rows]


def anime_detail(anime_id: int) -> dict | None:
    a = db.q1("""SELECT a.*, ao.title_romaji AS o_romaji, ao.title_english AS o_english,
                 ao.title_japanese AS o_japanese, ao.year AS o_year, ao.season AS o_season
                 FROM anime a LEFT JOIN anime_override ao ON ao.anime_id=a.id WHERE a.id=?""", (anime_id,))
    if not a:
        return None
    d = dict(a)
    d["display_title"] = d["o_romaji"] or d["title_mal"] or d["title_romaji"]
    d["display_english"] = d["o_english"] or d["title_english"]
    d["display_japanese"] = d["o_japanese"] or d["title_japanese"]
    d["display_year"] = d["o_year"] or d["year"]
    d["display_season"] = d["o_season"] or d["season"]
    d["synonyms"] = json.loads(d["synonyms"] or "[]")
    d["genres"] = json.loads(d.get("genres") or "[]")
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["video_ids"] = [r["id"] for r in db.q(
        "SELECT id FROM v_video WHERE anime_id=? AND missing=0 ORDER BY type DESC, number, part, version", (anime_id,))]
    d.pop("relations", None)                # large; the UI gets the resolved version below
    d.update(related_series(anime_id))
    return d


# ------------------------------------------------------------- related series (sequels, prequels...)

REL_LABELS = {"PREQUEL": "Prequel", "SEQUEL": "Sequel", "PARENT": "Main story", "SIDE_STORY": "Side story",
              "SPIN_OFF": "Spin-off", "ALTERNATIVE": "Alternative version", "SUMMARY": "Recap",
              "COMPILATION": "Compilation", "CONTAINS": "Contains", "OTHER": "Related", "ADAPTATION": "Adaptation",
              "SOURCE": "Source"}
# relations that make two entries part of the same franchise (OTHER = loose links such as crossovers)
FRANCHISE_RELS = {"PREQUEL", "SEQUEL", "PARENT", "SIDE_STORY", "SPIN_OFF", "ALTERNATIVE", "SUMMARY",
                  "COMPILATION", "CONTAINS"}
FRANCHISE_MAX = 60
SEASON_MONTH = {"WINTER": 1, "SPRING": 4, "SUMMER": 7, "FALL": 10}
_graph_lock = threading.Lock()
_graph: dict = {"ver": None}


def _relation_graph() -> dict:
    """Franchise graph over AniList ids, built once per library change (a few ms for ~5,000 series)."""
    ver = db.version("library")
    with _graph_lock:
        if _graph.get("ver") == ver:
            return _graph
        rows = db.q("SELECT id, anilist_id, mal_id, relations FROM anime "
                    "WHERE id IN (SELECT anime_id FROM v_video WHERE missing=0)")
        local: dict[int, list[int]] = {}       # AniList id -> local series ids
        own: dict[int, int] = {}               # local series id -> AniList id
        edges: dict[int, list[dict]] = {}      # local series id -> its relations
        adj: dict[int, set[int]] = {}
        info: dict[int, dict] = {}             # AniList id -> what other entries say about it (title, date...)
        mal_only: dict[int, list[int]] = {}
        for r in rows:
            try:
                rel = json.loads(r["relations"] or "{}")
            except ValueError:
                rel = {}
            al = r["anilist_id"] or rel.get("al")
            if al:
                own[r["id"]] = al
                local.setdefault(al, []).append(r["id"])
            elif r["mal_id"]:
                mal_only.setdefault(r["mal_id"], []).append(r["id"])
            edges[r["id"]] = rel.get("edges") or []
            for e in edges[r["id"]]:
                info.setdefault(e["al"], e)
                if al and e["rel"] in FRANCHISE_RELS:
                    adj.setdefault(al, set()).add(e["al"])
                    adj.setdefault(e["al"], set()).add(al)
        for e in list(info.values()):                 # series known only by MAL id: recognise them in others' edges
            for lid in mal_only.get(e.get("mal") or -1, []):
                if lid not in own:
                    own[lid] = e["al"]
                    local.setdefault(e["al"], []).append(lid)
        _graph.clear()
        _graph.update(ver=ver, local=local, own=own, edges=edges, adj=adj, info=info)
        return _graph


def related_series(anime_id: int) -> dict:
    """Everything connected to a series that is in the library, in release order, plus the direct
    prequel/sequel for quick flipping and close relations that are not in the library."""
    g = _relation_graph()
    al = g["own"].get(anime_id)
    empty = {"franchise": [], "prev": None, "next": None, "related_missing": []}
    if not al:
        return empty
    depth = {al: 0}
    todo = [al]
    while todo and len(depth) < 2000:
        n = todo.pop(0)
        for m in g["adj"].get(n, ()):
            if m not in depth:
                depth[m] = depth[n] + 1
                todo.append(m)
    ids = {lid for n in depth for lid in g["local"].get(n, [])}
    ids.add(anime_id)
    if len(ids) > FRANCHISE_MAX:                       # very large franchises: the closest entries
        ids = set(sorted(ids, key=lambda i: depth.get(g["own"].get(i), 99))[:FRANCHISE_MAX]) | {anime_id}
    direct = {}                                         # AniList id -> relation as seen from this series
    for e in g["edges"].get(anime_id, []):
        direct.setdefault(e["al"], e["rel"])
    if len(ids) <= 1 and not direct:
        return empty
    rows = db.q(f"""SELECT a.id, COALESCE(ao.title_romaji, a.title_mal, a.title_romaji) AS title,
                    COALESCE(ao.title_english, a.title_english) AS title_english,
                    COALESCE(ao.year, a.year) AS year, COALESCE(ao.season, a.season) AS season, a.format,
                    a.cover_path IS NOT NULL AS has_cover
                    FROM anime a LEFT JOIN anime_override ao ON ao.anime_id=a.id
                    WHERE a.id IN ({",".join("?" * len(ids))})""", tuple(ids))

    def when(r):
        i = g["info"].get(g["own"].get(r["id"])) or {}
        year = r["year"] or i.get("year") or 9999
        month = i.get("month") or SEASON_MONTH.get((r["season"] or i.get("season") or "").upper(), 13)
        return (year, month, r["title"] or "")

    out = []
    for r in sorted(rows, key=when):
        rel = direct.get(g["own"].get(r["id"]))
        out.append({"id": r["id"], "title": r["title"], "title_english": r["title_english"], "year": r["year"],
                    "season": r["season"], "format": r["format"], "has_cover": bool(r["has_cover"]),
                    "current": r["id"] == anime_id,
                    "rel": REL_LABELS.get(rel) if rel and r["id"] != anime_id else None, "rel_type": rel})
    pos = next(i for i, x in enumerate(out) if x["current"])

    def pick(kind, step):
        for x in out:                                   # the direct prequel / sequel, when it is in the library
            if x["rel_type"] == kind:
                return {"id": x["id"], "title": x["title"], "label": REL_LABELS[kind]}
        j = pos + step                                  # otherwise the neighbour in release order
        if 0 <= j < len(out):
            return {"id": out[j]["id"], "title": out[j]["title"], "label": "Previous" if step < 0 else "Next"}
        return None

    have = set(g["local"])
    missing = []
    for e in g["edges"].get(anime_id, []):
        if e["rel"] in ("PREQUEL", "SEQUEL", "PARENT", "SIDE_STORY", "ALTERNATIVE", "SPIN_OFF") and e["al"] not in have \
                and e.get("format") not in ("MUSIC",):
            missing.append({"title": e.get("title"), "title_english": e.get("en"), "year": e.get("year"),
                            "format": e.get("format"), "rel": REL_LABELS.get(e["rel"], "Related"), "al": e["al"],
                            "order": ["PREQUEL", "SEQUEL", "PARENT", "ALTERNATIVE", "SIDE_STORY", "SPIN_OFF"].index(e["rel"])})
    missing.sort(key=lambda m: (m["order"], m["year"] or 9999))
    return {"franchise": out if len(out) > 1 else [], "prev": pick("PREQUEL", -1), "next": pick("SEQUEL", 1),
            "related_missing": missing[:8]}


def video_detail(video_id: int) -> dict | None:
    r = db.q1("SELECT * FROM v_video WHERE id=?", (video_id,))
    if not r:
        return None
    d = dict(r)
    d["synonyms"] = json.loads(d["synonyms"] or "[]")
    d["genres"] = json.loads(d.get("anime_genres") or "[]")
    try:
        d["content_reasons"] = json.loads(d.get("content") or "{}").get("reasons", [])
    except ValueError:
        d["content_reasons"] = []
    d["tags"] = json.loads(d.get("anime_tags") or "[]")
    d["suggestion"] = json.loads(d["suggestion"]) if d["suggestion"] else None
    o = db.q1("SELECT * FROM user_override WHERE video_id=?", (video_id,))
    d["override"] = dict(o) if o else None
    d["exists"] = not d["missing"]
    if d["duplicate_of"]:
        dup = db.q1("SELECT path FROM video WHERE id=?", (d["duplicate_of"],))
        d["duplicate_path"] = dup["path"] if dup else None
    d["duplicates"] = [x["path"] for x in db.q("SELECT path FROM video WHERE duplicate_of=?", (video_id,))]
    return d


def counts() -> dict:
    r = db.q1("""SELECT COUNT(*) total, SUM(type='OP') ops, SUM(type='ED') eds, SUM(fav) favs,
                 SUM(status='review') review, COUNT(DISTINCT anime_id) series FROM v_video WHERE missing=0""")
    m = db.q1("SELECT COUNT(*) n FROM video WHERE missing=1")
    d = dict(r)
    d["missing"] = m["n"]
    return {k: (v or 0) for k, v in d.items()}


# ------------------------------------------------------------- favourites

def set_favorite(ids: list[int], on: bool):
    """Persist favourites (callers bump the 'fav' change counter)."""
    with db.tx() as c:
        for i in ids:
            if on:
                c.execute("INSERT OR IGNORE INTO favorite(video_id, added_at) VALUES(?,?)", (i, time.time()))
            else:
                c.execute("DELETE FROM favorite WHERE video_id=?", (i,))


def set_rating(ids: list[int], value: int):
    """1 = like, -1 = dislike (hidden from For You), 0 = remove the rating. Callers bump 'fav'."""
    now = time.time()
    with db.tx() as c:
        for i in ids:
            if value:
                c.execute("INSERT INTO rating(video_id, value, at) VALUES(?,?,?) ON CONFLICT(video_id) "
                          "DO UPDATE SET value=excluded.value, at=excluded.at", (i, value, now))
            else:
                c.execute("DELETE FROM rating WHERE video_id=?", (i,))


# ------------------------------------------------------------- playback

def _video_path(video_id: int, check: bool = True) -> str:
    r = db.q1("SELECT path, missing FROM video WHERE id=?", (video_id,))
    if not r:
        raise FileNotFoundError("unknown video")
    if r["missing"] or (check and not os.path.exists(r["path"])):
        raise FileNotFoundError(f"File not found: {r['path']}")
    return r["path"]


def play(video_id: int):
    # no extra disk check here: VLC opens the file right away (a spinning-up disk would be hit twice)
    play_path(_video_path(video_id, check=False))


def play_path(path: str):
    vlc = settings().get("vlc_path")
    if not (vlc and os.path.isfile(vlc)) and not osutil.IS_WINDOWS:
        vlc = osutil.find_vlc()                  # Linux / macOS: VLC if installed, else the default player
    if vlc and os.path.isfile(vlc):
        osutil.launch_detached([vlc, path])
    else:
        osutil.open_path(path)                   # the system's default player
    log.info("play %s", path)


def reveal(video_id: int):
    path = _video_path(video_id)
    osutil.reveal_file(path)


# ------------------------------------------------------------- corrections

def _clean(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def save_video_override(ids: list[int], fields: dict):
    """Per-video corrections. Keys: type, number, version, year, song_title, artists. None clears a field."""
    allowed = {"type", "number", "version", "year", "song_title", "artists"}
    fields = {k: _clean(v) for k, v in fields.items() if k in allowed}
    for k in ("number", "version", "year"):
        if fields.get(k) is not None:
            fields[k] = int(fields[k])
    if fields.get("type"):
        fields["type"] = fields["type"].upper()
        if fields["type"] not in ("OP", "ED"):
            raise ValueError("type must be OP or ED")
    with db.tx() as c:
        for vid in ids:
            c.execute("INSERT OR IGNORE INTO user_override(video_id, updated_at) VALUES(?,?)", (vid, time.time()))
            for k, v in fields.items():
                c.execute(f"UPDATE user_override SET {k}=?, updated_at=? WHERE video_id=?", (v, time.time(), vid))
    search.reindex(ids)


def save_anime_override(anime_id: int, fields: dict):
    allowed = {"title_romaji", "title_english", "title_japanese", "year", "season"}
    fields = {k: _clean(v) for k, v in fields.items() if k in allowed}
    if fields.get("year") is not None:
        fields["year"] = int(fields["year"])
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO anime_override(anime_id, updated_at) VALUES(?,?)", (anime_id, time.time()))
        for k, v in fields.items():
            c.execute(f"UPDATE anime_override SET {k}=?, updated_at=? WHERE anime_id=?", (v, time.time(), anime_id))
    search.reindex_anime(anime_id)


def clear_overrides(ids: list[int]):
    with db.tx() as c:
        for vid in ids:
            c.execute("DELETE FROM user_override WHERE video_id=?", (vid,))
            c.execute("UPDATE video SET status='pending' WHERE id=?", (vid,))
    matcher.match_pending(only_ids=ids)


def assign(ids: list[int], target: dict) -> int:
    """Manually assign videos to an anime.

    target: {"anime_id": n} for an anime already in the library, or a candidate from remote search
    ({"source": "animethemes", "at_id": n} / {"source": "anilist", ...} / {"source": "jikan", ...}).
    """
    with db.tx() as c:
        if target.get("anime_id"):
            anime_id = int(target["anime_id"])
        elif target.get("at_id"):
            node = catalog.anime_node(int(target["at_id"]))
            if node is None:
                hits = at.search_anime(target.get("title_romaji") or "")
                node = next((h for h in hits if h.get("id") == int(target["at_id"])), None)
                if node:
                    catalog.store_anime_nodes([node], c)
            if node is None:
                raise ValueError("AnimeThemes anime not found")
            anime_id = matcher.upsert_anime(c, at.anime_summary(node), "animethemes")
        else:
            cand = dict(target)
            if not cand.get("title_romaji"):
                raise ValueError("candidate needs a title")
            anime_id = matcher.upsert_anime(c, cand, cand.get("source") or "manual")
        at_id = c.execute("SELECT at_id FROM anime WHERE id=?", (anime_id,)).fetchone()["at_id"]
        for vid in ids:
            v = c.execute("SELECT * FROM v_video WHERE id=?", (vid,)).fetchone()
            if v is None:
                continue
            p = parse_filename(v["filename"])
            typ, num, ver = v["type"] or p.type or "OP", v["number"] or p.number, v["version"] or p.version
            song = artists = episodes = None
            if at_id:
                cv = c.execute("SELECT song_title, artists, episodes FROM cat_video WHERE at_anime_id=? AND type=? "
                               "AND number=? ORDER BY version LIMIT 1", (at_id, typ, num)).fetchone()
                if cv:
                    song, artists, episodes = cv["song_title"], cv["artists"], cv["episodes"]
            theme_id = matcher.get_or_create_theme(c, anime_id, typ, num, v["part"] or "", ver, song, artists, episodes)
            c.execute("UPDATE video SET anime_id=?, theme_id=?, match_source='manual', confidence=1.0, "
                      "status='matched', suggestion=NULL WHERE id=?", (anime_id, theme_id, vid))
            c.execute("INSERT OR IGNORE INTO user_override(video_id, updated_at) VALUES(?,?)", (vid, time.time()))
            c.execute("UPDATE user_override SET anime_id=?, updated_at=? WHERE video_id=?", (anime_id, time.time(), vid))
    search.reindex(ids)
    return anime_id


def accept_suggestions(ids: list[int]) -> int:
    n = 0
    for vid in ids:
        r = db.q1("SELECT suggestion FROM video WHERE id=?", (vid,))
        if not r or not r["suggestion"]:
            continue
        s = json.loads(r["suggestion"])
        cand = s.get("candidate") or {}
        assign([vid], cand)
        n += 1
    return n


# ------------------------------------------------------------- remote search (correction screen)

def remote_search(text: str, source: str) -> list[dict]:
    text = (text or "").strip()
    if not text:
        return []
    out: list[dict] = []
    if source == "library":
        like = f"%{text}%"
        for r in db.q("""SELECT a.id, COALESCE(ao.title_romaji,a.title_mal,a.title_romaji) t, a.title_english,
                         a.title_japanese, a.year, a.season, a.cover_path IS NOT NULL hc, a.mal_id, a.at_id
                         FROM anime a LEFT JOIN anime_override ao ON ao.anime_id=a.id
                         WHERE a.title_romaji LIKE ? OR a.title_english LIKE ? OR a.title_japanese LIKE ?
                         OR a.synonyms LIKE ? OR ao.title_romaji LIKE ? ORDER BY a.year DESC LIMIT 30""",
                      (like, like, like, like, like)):
            out.append({"source": "library", "anime_id": r["id"], "title_romaji": r["t"],
                        "title_english": r["title_english"], "title_japanese": r["title_japanese"],
                        "year": r["year"], "season": r["season"], "mal_id": r["mal_id"], "at_id": r["at_id"],
                        "cover": f"/cover/{r['id']}" if r["hc"] else None})
        # also search the offline AnimeThemes catalogue
        in_library = {r.get("at_id") for r in out}
        idx = matcher.index()
        for sc, e in idx.fuzzy(text, None, limit=10):
            if e.at_id in in_library:
                continue
            out.append({"source": "animethemes", "at_id": e.at_id, "title_romaji": e.display, "year": e.year,
                        "score": round(sc)})
        return out
    if source == "animethemes":
        for a in at.search_anime(text):
            s = at.anime_summary(a)
            s["source"] = "animethemes"
            s["cover"] = s.get("cover_url")
            out.append(s)
            with db.tx() as c:
                catalog.store_anime_nodes([a], c)
        return out
    if source == "anilist":
        for m in anilist.search(text, 15):
            cnd = anilist.to_candidate(m)
            cnd["cover"] = cnd.get("cover_url")
            out.append(cnd)
        return out
    if source == "mal":
        for a in jikan.search(text, 15):
            cnd = jikan.to_candidate(a)
            cnd["cover"] = cnd.get("cover_url")
            out.append(cnd)
        return out
    raise ValueError("unknown source")
