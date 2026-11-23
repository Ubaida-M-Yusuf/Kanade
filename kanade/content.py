"""Content ratings: which series are ecchi / suggestive or 18+, from several sources combined.

Sources (any one is enough, so little slips through):
  * AniList  - "Ecchi" / "Hentai" genres, the isAdult flag, tags in the "Sexual Content" category (or adult tags)
  * Kitsu    - nsfw flag, age rating R18, rating notes that mention nudity / sexual content
  * AnimeThemes - NSFW marks on individual opening/ending videos (also flags the video itself)
  * MyAnimeList - genres / themes (Ecchi, Hentai, Erotica, Gore, Horror) and the Rx rating, with the user's own
    client ID (Settings). MyAnimeList only ADDS: every mark the other sources give stays. Each series is fetched
    once: mal_at > 0 = complete (never asked again), < 0 = incomplete (asked again after RETRY_DAYS), 0 = not yet.

A second mark, gore / horror ("dark": 2 gore, 1 horror only), comes from MyAnimeList's Gore / Horror genres, AniList's
Horror genre and AniList's Gore / Body Horror / Torture / Cannibalism tags.
"""
from __future__ import annotations

import json
import logging
import re
import time

from . import catalog, db
from .sources import anilist, kitsu, mal

log = logging.getLogger("kanade.content")

SEXUAL_RANK = 40                 # AniList tag relevance (0-100) needed for a sexual-content tag to count
GUIDE_RE = re.compile(r"nudity|sexual|ecchi|fan ?service|erotic", re.I)


RULES_VERSION = "2"             # bump when the rules below change: every series is re-marked (offline)
GORE_TAGS = ("Gore", "Body Horror", "Torture", "Cannibalism")
RETRY_DAYS = 30                 # an incomplete answer (no genres yet, e.g. not aired) is asked again after this


def classify(src: dict, genres=(), tags=()) -> tuple[int, int, int, list[str], list[str]]:
    """(suggestive, explicit, dark, reasons, dark_reasons) from the collected source data plus the stored AniList
    genres / tags. Every source can only add a mark."""
    reasons, explicit = [], False
    al = src.get("anilist") or {}
    if al.get("adult"):
        explicit = True
        reasons.append("AniList: 18+")
    if "Hentai" in (al.get("genres") or []):
        explicit = True
        reasons.append("AniList genre: Hentai")
    if "Ecchi" in (al.get("genres") or []):
        reasons.append("AniList genre: Ecchi")
    if al.get("sexual_tags"):
        reasons.append("AniList tags: " + ", ".join(al["sexual_tags"][:4]))
    ki = src.get("kitsu") or {}
    if ki.get("nsfw") or (ki.get("age") or "").upper() == "R18":
        explicit = True
        reasons.append(f"Kitsu: {'NSFW' if ki.get('nsfw') else ''} {ki.get('age', '')}".strip())
    elif GUIDE_RE.search(ki.get("guide") or ""):
        reasons.append(f"Kitsu: {ki.get('guide')}")
    m = src.get("mal") or {}
    mal_genres = m.get("genres") or src.get("mal_genres") or []
    hits = [g for g in mal_genres if g in ("Ecchi", "Hentai", "Erotica")]
    if hits:
        reasons.append("MyAnimeList: " + ", ".join(hits))
        explicit = explicit or any(g in ("Hentai", "Erotica") for g in hits)
    if (m.get("rating") or "").lower() == "rx":
        explicit = True
        reasons.append("MyAnimeList rating: Rx (hentai)")
    suggestive = bool(reasons)
    if src.get("at_nsfw"):           # informational only: AnimeThemes also marks gore / flashing images NSFW
        reasons.append(f"AnimeThemes: {src['at_nsfw']} video(s) marked NSFW")
    # gore / horror
    dark_reasons, gore = [], False
    mg = [g for g in mal_genres if g in ("Gore", "Horror")]
    if mg:
        dark_reasons.append("MyAnimeList: " + ", ".join(mg))
        gore = gore or "Gore" in mg
    al_genres = set((src.get("anilist") or {}).get("genres") or []) | set(genres or [])
    if "Horror" in al_genres:
        dark_reasons.append("AniList genre: Horror")
    gt = [t for t in GORE_TAGS if t in set(tags or [])]
    if gt:
        dark_reasons.append("AniList tags: " + ", ".join(gt))
        gore = True
    dark = 2 if gore else 1 if dark_reasons else 0
    return int(suggestive), int(explicit), dark, reasons, dark_reasons


def _apply(c, aid: int, src: dict, genres_json: str, tags_json: str, extra_sets: str = "", extra_args=()):
    """Store the sources and the combined marks of one series (c: a write transaction)."""
    try:
        genres, tags = json.loads(genres_json or "[]"), json.loads(tags_json or "[]")
    except ValueError:
        genres, tags = [], []
    sug, exp, dark, reasons, dark_reasons = classify(src, genres, tags)
    data = {**src, "reasons": reasons, "dark_reasons": dark_reasons}
    c.execute(f"UPDATE anime SET content=?, suggestive=?, explicit=?, dark=?{extra_sets} WHERE id=?",
              (json.dumps(data, ensure_ascii=False), sug, exp, dark, *extra_args, aid))


def _stored_sources(content_json: str) -> dict:
    try:
        d = json.loads(content_json or "{}")
    except ValueError:
        d = {}
    d.pop("reasons", None)
    d.pop("dark_reasons", None)
    return d


def reclassify_all() -> int:
    """Re-mark every series from what is already stored (offline, a second or so); used when the rules change."""
    rows = db.q("SELECT id, content, genres, tags FROM anime WHERE id IN (SELECT anime_id FROM v_video)")
    with db.tx() as c:
        for r in rows:
            _apply(c, r["id"], _stored_sources(r["content"]), r["genres"], r["tags"])
    db.meta_set("content_rules", RULES_VERSION)
    db.bump_library_version()
    log.info("content marks re-applied to %s series (rules %s)", len(rows), RULES_VERSION)
    return len(rows)


def mal_status() -> dict:
    r = db.q1("SELECT COUNT(*) total, SUM(mal_at>0) done, SUM(mal_at<0) waiting FROM anime WHERE mal_id IS NOT NULL "
              "AND id IN (SELECT anime_id FROM v_video)")
    return {"total": r["total"] or 0, "done": r["done"] or 0, "waiting": r["waiting"] or 0,
            "error": db.meta_get("mal_error") or ""}


def mal_genres_of(content_json: str) -> list[str]:
    """MyAnimeList genres / themes stored for a series (shown next to the AniList ones; never replaces them)."""
    return list((_stored_sources(content_json).get("mal") or {}).get("genres") or [])


def refresh_mal(client_id: str, progress=None, should_stop=lambda: False, on_start=None) -> dict:
    """Ask MyAnimeList for the genres / themes / rating of every series not asked yet (or long ago), saving every
    25 series; a quit or a network problem loses at most that, and the rest continues next time."""
    result = {"done": 0, "left": 0, "paused": False}
    if not client_id:
        return result
    rows = db.q("SELECT id, mal_id FROM anime WHERE mal_id IS NOT NULL AND (mal_at=0 OR (mal_at<0 AND mal_at>?)) "
                "AND id IN (SELECT anime_id FROM v_video) ORDER BY mal_at DESC, id", (-(time.time() - RETRY_DAYS * 86400),))
    if not rows:
        return result
    if on_start:
        on_start(len(rows))
    done, got = 0, {}

    def save():
        if not got:
            return
        ids = tuple(got)
        cur = {r["id"]: r for r in db.q("SELECT id, content, genres, tags FROM anime WHERE id IN (%s)"
                                        % ",".join("?" * len(ids)), ids)}
        now = time.time()
        with db.tx() as c:
            for aid, info in got.items():
                r = cur.get(aid)
                if not r:
                    continue
                src = _stored_sources(r["content"])
                if info is not None:
                    src["mal"] = info
                complete = bool(info and info.get("genres"))      # no genres yet / unknown: ask again later
                _apply(c, aid, src, r["genres"], r["tags"], ", mal_at=?", (now if complete else -now,))
        got.clear()
        db.bump_library_version()

    try:
        for r in rows:
            if should_stop():
                break
            try:
                got[r["id"]] = mal.anime_info(r["mal_id"], client_id)
            except mal.Rejected:
                db.meta_set("mal_error", "MyAnimeList refused the client ID: check it in Settings")
                log.warning("MyAnimeList refused the client ID")
                break
            except Exception as e:
                log.warning("MyAnimeList not reachable (%s); pausing", e)
                result["paused"] = True
                break
            done += 1
            if len(got) >= 25:
                save()
            if progress:
                progress(done, len(rows), "%d/%d series" % (done, len(rows)))
        else:
            db.meta_set("mal_error", "")
    finally:
        save()
    log.info("MyAnimeList genres fetched for %s series", done)
    result.update(done=done, left=len(rows) - done)
    return result


def _at_nsfw(node: dict) -> tuple[int, set[str]]:
    """Count NSFW-marked theme entries of an AnimeThemes anime and collect their video basenames."""
    n, names = 0, set()
    for th in node.get("animethemes") or []:
        for ent in th.get("animethemeentries") or []:
            if ent.get("nsfw"):
                n += 1
                for v in (ent.get("videos") or {}).get("nodes", []):
                    if v and v.get("basename"):
                        names.add(v["basename"].lower())
    return n, names


def _refresh_video_marks(rows) -> int:
    """AnimeThemes NSFW marks on individual videos (offline, from the catalogue mirror; whole library)."""
    names: set[str] = set()
    for r in rows:
        node = catalog.anime_node(r["at_id"]) if r["at_id"] else None
        if node:
            names |= _at_nsfw(node)[1]
    with db.tx() as c:
        c.execute("UPDATE video SET nsfw=0 WHERE nsfw<>0")
        for name in names:
            c.execute("UPDATE video SET nsfw=1 WHERE lower(filename)=?", (name,))
    return len(names)


CHUNK = 200                      # series per saved step: a quit or a network failure loses at most one step


def _marks_signature() -> str:
    """Changes when the catalogue mirror is re-synced or the library's videos / series change."""
    r = db.q1("SELECT COUNT(*) n, MAX(id) mx, COUNT(DISTINCT anime_id) na, SUM(DISTINCT anime_id) sa FROM v_video")
    return f"{db.meta_get('catalog_synced_at')}|{r['n']}|{r['mx']}|{r['na']}|{r['sa']}"


def refresh(progress=None, only_missing: bool = True, should_stop=lambda: False, on_start=None) -> int:
    """Collect content ratings for series in the library, saving after every chunk of series.

    A series is marked done only when every source answered for it; if a source fails, what was
    collected is still shown, and the series is asked again on the next scan.
    """
    # per-video NSFW marks come from the local catalogue copy: redone only when it or the library changed
    sig = _marks_signature()
    marked = None
    if sig != db.meta_get("content_marks_sig") or not only_missing:
        every = db.q("SELECT id, at_id FROM anime WHERE id IN (SELECT anime_id FROM v_video)")
        marked = _refresh_video_marks(every)
        db.meta_set("content_marks_sig", sig)
    where = "content_at=0 AND " if only_missing else ""
    if db.meta_get("content_rules") != RULES_VERSION:
        reclassify_all()
    rows = db.q(f"SELECT id, at_id, anilist_id, mal_id, content, genres, tags FROM anime WHERE {where}"
                "id IN (SELECT anime_id FROM v_video) ORDER BY id")
    total, done, updated = len(rows), 0, 0
    if total and on_start:
        on_start(total)                       # only now is there something to fetch (and a progress bar to show)
    for i in range(0, total, CHUNK):
        if should_stop():
            break
        chunk = rows[i:i + CHUNK]
        src, complete = _collect(chunk)
        now = time.time()
        by_id = {r["id"]: r for r in chunk}
        with db.tx() as c:
            for aid, s in src.items():
                r = by_id[aid]
                old = _stored_sources(r["content"])
                if "mal" in old:                     # MyAnimeList data is kept (it is fetched separately)
                    s = {**s, "mal": old["mal"]}
                _apply(c, aid, s, r["genres"], r["tags"], ", content_at=?", (now if complete else 0,))
        updated += len(src)
        done += len(chunk)
        db.bump_library_version()
        if progress:
            progress(done, total, f"{done}/{total} series")
        if not complete:
            log.warning("content ratings: a source did not answer; the rest is retried on the next scan")
            break
    if total or marked is not None:
        log.info("content ratings updated for %s series%s", updated,
                 f" ({marked} NSFW-marked videos)" if marked is not None else "")
    return updated


def _collect(rows) -> tuple[dict, bool]:
    """({anime_id: source data}, every source answered) for a chunk of series."""
    src = {r["id"]: {} for r in rows}
    complete = True
    kitsu_ids: dict[int, int] = {}
    for r in rows:                                  # AnimeThemes (offline) + Kitsu ids from the catalogue
        node = catalog.anime_node(r["at_id"]) if r["at_id"] else None
        if not node:
            continue
        n, _ = _at_nsfw(node)
        if n:
            src[r["id"]]["at_nsfw"] = n
        for res in (node.get("resources") or {}).get("nodes", []):
            if res and res.get("site") == "KITSU" and res.get("externalId"):
                kitsu_ids[r["id"]] = int(res["externalId"])
    by_al = {r["anilist_id"]: r["id"] for r in rows if r["anilist_id"]}
    by_mal = {r["mal_id"]: r["id"] for r in rows if not r["anilist_id"] and r["mal_id"]}
    for key, mapping in (("id", by_al), ("idMal", by_mal)):          # AniList, 50 per request
        ids = sorted(mapping)
        for j in range(0, len(ids), 50):
            try:
                media = anilist.content_batch(ids[j:j + 50], by=key)
            except Exception as e:
                log.warning("AniList content batch failed: %s", e)
                complete = False
                break
            for m in media:
                aid = mapping.get(m.get("id") if key == "id" else m.get("idMal"))
                if not aid:
                    continue
                sexual = [t["name"] for t in m.get("tags") or []
                          if (t.get("category") == "Sexual Content" or t.get("isAdult")) and (t.get("rank") or 0) >= SEXUAL_RANK]
                src[aid]["anilist"] = {"adult": bool(m.get("isAdult")), "genres": m.get("genres") or [], "sexual_tags": sexual}
    if kitsu_ids:                                                    # Kitsu, 20 per request
        inv: dict[int, list[int]] = {}
        for aid, kid in kitsu_ids.items():
            inv.setdefault(kid, []).append(aid)
        try:
            got = kitsu.ratings(sorted(inv))
        except Exception as e:
            log.warning("Kitsu ratings failed: %s", e)
            got, complete = {}, False
        for kid, rating in got.items():
            for aid in inv.get(kid, []):
                src[aid]["kitsu"] = rating
    return src, complete
