"""Identification pipeline: video file -> anime + theme.

Priority order
  1. AnimeThemes exact basename (local catalogue mirror, then live lookup)  conf 1.00
  2. AnimeThemes filename-prefix match (same series, theme not in catalogue)  conf 0.90-0.95
  3. Fuzzy title match against the local AnimeThemes catalogue (year +-1)
  4. AniList search, then Jikan/MyAnimeList search, scored with rapidfuzz (year +-1)
  5. Optional Ollama title suggestion, then steps 3-4 again
Anything below the acceptance threshold goes to Needs Review with its best suggestion.
Videos with a manual anime assignment are never re-matched.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

from rapidfuzz import fuzz

from . import catalog, db
from .config import settings
from .net import HostDown
from .parser import norm, parse_filename, search_title
from .sources import anilist, animethemes as at, jikan, ollama

log = logging.getLogger("kanade.matcher")


# ---------------------------------------------------------------- DB helpers

def upsert_anime(c, s: dict, source: str) -> int:
    """Insert or update an anime from a summary/candidate dict. Dedupes by AT id, then MAL, then AniList id."""
    now = time.time()
    row = None
    if s.get("at_id"):
        row = c.execute("SELECT id FROM anime WHERE at_id=?", (s["at_id"],)).fetchone()
    if row is None and s.get("mal_id"):
        row = c.execute("SELECT id FROM anime WHERE mal_id=? AND (at_id IS NULL OR ? IS NULL)",
                        (s["mal_id"], s.get("at_id"))).fetchone()
    if row is None and s.get("anilist_id"):
        row = c.execute("SELECT id FROM anime WHERE anilist_id=? AND (at_id IS NULL OR ? IS NULL)",
                        (s["anilist_id"], s.get("at_id"))).fetchone()
    syn = json.dumps(s.get("synonyms") or [], ensure_ascii=False)
    if row:
        c.execute("""UPDATE anime SET at_id=COALESCE(?,at_id), at_slug=COALESCE(?,at_slug),
                     mal_id=COALESCE(?,mal_id), anilist_id=COALESCE(?,anilist_id),
                     title_romaji=COALESCE(NULLIF(?,''),title_romaji),
                     title_english=COALESCE(?,title_english), title_japanese=COALESCE(?,title_japanese),
                     synonyms=CASE WHEN ?='[]' THEN synonyms ELSE ? END,
                     year=COALESCE(?,year), season=COALESCE(?,season), format=COALESCE(?,format),
                     cover_url=COALESCE(?,cover_url), source=COALESCE(source,?), updated_at=? WHERE id=?""",
                  (s.get("at_id"), s.get("at_slug"), s.get("mal_id"), s.get("anilist_id"), s.get("title_romaji"),
                   s.get("title_english"), s.get("title_japanese"), syn, syn, s.get("year"), s.get("season"),
                   s.get("format"), s.get("cover_url"), source, now, row["id"]))
        return row["id"]
    cur = c.execute("""INSERT INTO anime(at_id,at_slug,mal_id,anilist_id,title_romaji,title_english,title_japanese,
                       synonyms,year,season,format,cover_url,source,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (s.get("at_id"), s.get("at_slug"), s.get("mal_id"), s.get("anilist_id"),
                     s.get("title_romaji") or "Unknown", s.get("title_english"), s.get("title_japanese"), syn,
                     s.get("year"), s.get("season"), s.get("format"), s.get("cover_url"), source, now, now))
    return cur.lastrowid


def get_or_create_theme(c, anime_id: int, type_: str, number: int, part: str, version: int,
                        song_title=None, artists=None, episodes=None) -> int:
    type_ = (type_ or "OP").upper()
    row = c.execute("SELECT id FROM theme WHERE anime_id=? AND type=? AND number=? AND part=? AND version=?",
                    (anime_id, type_, number or 1, part or "", version or 1)).fetchone()
    if row:
        if song_title or artists:
            c.execute("UPDATE theme SET song_title=COALESCE(NULLIF(?,''),song_title), "
                      "artists=COALESCE(NULLIF(?,''),artists), episodes=COALESCE(?,episodes) WHERE id=?",
                      (song_title, artists, episodes, row["id"]))
        return row["id"]
    cur = c.execute("INSERT INTO theme(anime_id,type,number,part,version,song_title,artists,episodes) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (anime_id, type_, number or 1, part or "", version or 1, song_title or None,
                     artists or None, episodes))
    return cur.lastrowid


# ---------------------------------------------------------- local fuzzy index

@dataclass
class CatEntry:
    at_id: int
    year: int | None
    titles: list[str]       # normalised
    display: str


class CatalogIndex:
    def __init__(self):
        self.entries: list[CatEntry] = []
        self.by_prefix: dict[str, list[int]] = {}
        self.by_mal: dict[int, int] = {}
        self.by_anilist: dict[int, int] = {}
        self.years: dict[int, int | None] = {}
        self.built_at = 0.0

    def build(self):
        entries = []
        for row in db.q("SELECT at_id, slug, name, year, data FROM cat_anime"):
            a = json.loads(row["data"])
            s = at.anime_summary(a)
            titles = {norm(s["title_romaji"]), norm(s["title_english"]), norm(row["slug"])}
            titles |= {norm(x) for x in s["synonyms"]}
            titles.discard("")
            entries.append(CatEntry(row["at_id"], row["year"], sorted(titles), s["title_romaji"]))
            if s["mal_id"]:
                self.by_mal.setdefault(int(s["mal_id"]), row["at_id"])
            if s["anilist_id"]:
                self.by_anilist.setdefault(int(s["anilist_id"]), row["at_id"])
            self.years[row["at_id"]] = row["year"]
        self.entries = entries
        self.by_prefix = {}
        for r in db.q("SELECT DISTINCT prefix_l, at_anime_id FROM cat_video"):
            self.by_prefix.setdefault(norm(r["prefix_l"]), []).append(r["at_anime_id"])
        self.built_at = time.time()
        log.info("catalogue index: %s anime, %s prefixes", len(self.entries), len(self.by_prefix))

    def fuzzy(self, query: str, year: int | None, limit: int = 5):
        qn = norm(query)
        if not qn:
            return []
        scored = []
        for e in self.entries:
            best = 0.0
            for t in e.titles:
                sc = fuzz.ratio(qn, t)
                if sc > best:
                    best = sc
            if best < 60:
                continue
            scored.append((year_adjust(best, year, e.year), e))
        scored.sort(key=lambda x: -x[0])
        return scored[:limit]


def year_adjust(score: float, folder_year: int | None, cand_year: int | None) -> float:
    if not folder_year or not cand_year:
        return score
    diff = abs(int(folder_year) - int(cand_year))
    if diff == 0:
        return min(100.0, score + 2)
    if diff == 1:
        return score
    return score * 0.82


_index: CatalogIndex | None = None
_index_lock = threading.Lock()


def index() -> CatalogIndex:
    global _index
    with _index_lock:
        synced = float(db.meta_get("catalog_synced_at") or 0)
        if _index is None or _index.built_at < synced:
            _index = CatalogIndex()
            _index.build()
        return _index


def invalidate_index():
    global _index
    with _index_lock:
        _index = None


# ---------------------------------------------------------------- matching

def score_candidate(query: str, cand: dict, folder_year: int | None) -> float:
    qn = norm(query)
    titles = [cand.get("title_romaji"), cand.get("title_english"), *(cand.get("synonyms") or [])]
    best = max((fuzz.ratio(qn, norm(t)) for t in titles if t), default=0)
    return year_adjust(best, folder_year, cand.get("year"))


def _pick_closest_year(at_ids, folder_year, years):
    if len(at_ids) == 1 or not folder_year:
        return at_ids[0]
    return min(at_ids, key=lambda i: abs((years.get(i) or folder_year) - folder_year))


def _cat_match(video, filename: str):
    rows = catalog.lookup_basename(filename)
    if not rows:
        return None
    years = {r["at_anime_id"]: db.q1("SELECT year FROM cat_anime WHERE at_id=?", (r["at_anime_id"],))["year"]
             for r in rows}
    best_id = _pick_closest_year(list(years), video["folder_year"], years)
    r = next(r for r in rows if r["at_anime_id"] == best_id)
    return r


def match_one(video, online: bool = True) -> dict:
    """Return a match result for one `video` row (does not write).

    online=False only tries the offline strategies and returns {"deferred": True} when the
    network would be needed, so a first pass can commit thousands of exact matches quickly.
    """
    filename = video["filename"]
    p = parse_filename(filename)
    folder_year = video["folder_year"]
    accept = float(settings().get("accept_confidence") or 0.85)
    idx = index()

    # 1. exact basename in the local AnimeThemes catalogue mirror
    r = _cat_match(video, filename)
    if r is None and p.ok:
        # Not in the local mirror (e.g. a brand-new season): ask AnimeThemes live before falling back to
        # the filename prefix, so a new season isn't attached to an older one with the same prefix.
        if not online:
            return {"deferred": True}
        # not in the mirror: try a live lookup (covers entries added after the last sync)
        try:
            hits = at.lookup_video(filename)
        except Exception as e:  # network down etc. -> continue with the other strategies
            log.debug("live AnimeThemes lookup failed for %s: %s", filename, e)
            hits = []
        if hits:
            with db.tx() as c:
                catalog.store_anime_nodes([h[2] for h in hits if h[2]], c)
            invalidate_index()
            idx = index()
            r = _cat_match(video, filename)
    if r is not None:
        return {"kind": "at", "at_id": r["at_anime_id"], "theme": dict(r), "source": "animethemes",
                "confidence": 1.0, "status": "matched"}

    if not p.ok:
        return {"kind": "none", "source": None, "confidence": 0.0, "status": "review",
                "suggestion": None, "reason": "filename not recognised"}

    # 2. same filename prefix as a known AnimeThemes series
    ids = idx.by_prefix.get(norm(p.prefix))
    if ids:
        ids = list(dict.fromkeys(ids))
        best = _pick_closest_year(ids, folder_year, idx.years)
        conf = 0.95 if len(ids) == 1 else 0.9
        return {"kind": "at", "at_id": best, "theme": None, "source": "animethemes-prefix",
                "confidence": conf, "status": "matched" if conf >= accept else "review"}

    best_sugg = None
    queries = [search_title(p), p.title_guess]
    # Game / visual-novel openings (the 'misc' folder, "...VN" / "...Game" names) are not anime:
    # a similar anime title is only a suggestion there, never an automatic match, and
    # online anime databases won't know them, so only the offline catalogue is searched.
    non_anime = (video["folder"] or "").lower() == "misc" or p.prefix.endswith(("VN", "Game"))

    def consider(cand, score, source):
        nonlocal best_sugg
        conf = round(score / 100.0, 3)
        if best_sugg is None or conf > best_sugg["confidence"]:
            best_sugg = {"candidate": cand, "confidence": conf, "source": source}

    def try_strategies(qs, tag=""):
        # 3. fuzzy against local catalogue
        for q_ in qs:
            for sc, e in idx.fuzzy(q_, folder_year, limit=3):
                consider({"source": "animethemes", "at_id": e.at_id, "title_romaji": e.display, "year": e.year},
                         sc, "animethemes-fuzzy" + tag)
        if (best_sugg and best_sugg["confidence"] >= max(accept, 0.9)) or non_anime:
            return
        # 4. online searches
        if settings().get("use_anilist"):
            for q_ in qs[:1]:
                try:
                    for m in anilist.search(q_, 8):
                        cand = anilist.to_candidate(m)
                        if cand.get("mal_id") in idx.by_mal:
                            cand["at_id"] = idx.by_mal[cand["mal_id"]]
                        consider(cand, score_candidate(q_, cand, folder_year), "anilist" + tag)
                except (HostDown, Exception) as e:
                    log.debug("AniList search failed: %s", e)
        if settings().get("use_jikan") and (not best_sugg or best_sugg["confidence"] < accept):
            for q_ in qs[:1]:
                try:
                    for a in jikan.search(q_, 8):
                        cand = jikan.to_candidate(a)
                        if cand.get("mal_id") in idx.by_mal:
                            cand["at_id"] = idx.by_mal[cand["mal_id"]]
                        consider(cand, score_candidate(q_, cand, folder_year), "jikan" + tag)
                except (HostDown, Exception) as e:
                    log.debug("Jikan search failed: %s", e)

    try_strategies(queries)

    # 5. optional Ollama fallback
    if (not best_sugg or best_sugg["confidence"] < accept) and not non_anime and ollama.available():
        guesses = ollama.suggest_title(p.prefix, p.title_guess, folder_year)
        if guesses:
            log.info("Ollama suggests %s for %s", guesses, filename)
            try_strategies(guesses, "+ollama")

    if best_sugg and best_sugg["confidence"] >= accept and not non_anime:
        cand = best_sugg["candidate"]
        return {"kind": "at" if cand.get("at_id") else "cand", "at_id": cand.get("at_id"), "candidate": cand,
                "theme": None, "source": best_sugg["source"], "confidence": best_sugg["confidence"],
                "status": "matched"}
    return {"kind": "none", "source": best_sugg["source"] if best_sugg else None,
            "confidence": best_sugg["confidence"] if best_sugg else 0.0, "status": "review",
            "suggestion": best_sugg}


def apply_result(c, video, res: dict):
    """Write a match result to the DB."""
    p = parse_filename(video["filename"])
    anime_id = theme_id = None
    if res["kind"] == "at" and res.get("at_id"):
        node = catalog.anime_node(res["at_id"])
        if node:
            anime_id = upsert_anime(c, at.anime_summary(node), "animethemes")
    elif res["kind"] == "cand":
        anime_id = upsert_anime(c, res["candidate"], res["candidate"].get("source", "manual"))
    if anime_id:
        th = res.get("theme")
        if th:
            theme_id = get_or_create_theme(c, anime_id, th["type"], th["number"], th["part"] or "",
                                           th["version"] or 1, th["song_title"], th["artists"], th["episodes"])
        else:
            # find the matching theme in the catalogue by type/number to pick up song info
            song = artists = episodes = None
            if res.get("at_id"):
                cv = c.execute("SELECT song_title, artists, episodes FROM cat_video WHERE at_anime_id=? AND type=? "
                               "AND number=? ORDER BY version LIMIT 1",
                               (res["at_id"], p.type, p.number)).fetchone()
                if cv:
                    song, artists, episodes = cv["song_title"], cv["artists"], cv["episodes"]
            theme_id = get_or_create_theme(c, anime_id, p.type or "OP", p.number, p.part, p.version,
                                           song, artists, episodes)
    sugg = res.get("suggestion")
    c.execute("UPDATE video SET anime_id=?, theme_id=?, match_source=?, confidence=?, status=?, suggestion=? "
              "WHERE id=?",
              (anime_id, theme_id, res.get("source"), res.get("confidence"), res["status"],
               json.dumps(sugg, ensure_ascii=False) if sugg else None, video["id"]))
    return anime_id


def match_pending(progress=None, only_ids: list[int] | None = None, stop_event=None, after_offline=None) -> dict:
    """Match all pending videos (or the given ids). Manual assignments are skipped."""
    from . import search  # local import to avoid a cycle
    if only_ids:
        rows = db.q(f"SELECT v.* FROM video v LEFT JOIN user_override o ON o.video_id=v.id "
                    f"WHERE v.id IN ({','.join('?' * len(only_ids))}) AND o.anime_id IS NULL", only_ids)
    else:
        rows = db.q("SELECT v.* FROM video v LEFT JOIN user_override o ON o.video_id=v.id "
                    "WHERE v.status='pending' AND v.missing=0 AND o.anime_id IS NULL ORDER BY v.id")
    stats = {"matched": 0, "review": 0, "deferred": 0}
    index()
    done = 0
    total = len(rows)

    def run(batch_rows, online, commit_every):
        nonlocal done
        deferred, batch = [], []

        def flush():
            if not batch:
                return
            with db.tx() as c:
                for vv, rr in batch:
                    apply_result(c, vv, rr)
            search.reindex([vv["id"] for vv, _ in batch])
            batch.clear()

        for v in batch_rows:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                res = match_one(v, online=online)
            except Exception as e:
                log.exception("matching failed for %s", v["filename"])
                res = {"kind": "none", "source": None, "confidence": 0.0, "status": "review",
                       "suggestion": None, "reason": str(e)}
            if res.get("deferred"):
                deferred.append(v)
                continue
            done += 1
            batch.append((v, res))
            stats[res["status"]] = stats.get(res["status"], 0) + 1
            if len(batch) >= commit_every:
                flush()
            if progress and (done % 25 == 0 or not online):
                progress(done, total, v["filename"])
        flush()
        return deferred

    # pass 1: everything that can be identified offline (exact AnimeThemes basenames etc.)
    later = run(rows, online=False, commit_every=300)
    stats["deferred"] = len(later)
    if after_offline:
        after_offline()
    # pass 2: the few files that need online searches
    if later:
        log.info("%s file(s) need online identification", len(later))
        run(later, online=True, commit_every=5)
    if progress:
        progress(total, total, "")
    return stats
