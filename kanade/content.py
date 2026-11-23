"""Content ratings: which series are ecchi / suggestive or 18+, from several sources combined.

Sources (any one is enough, so little slips through):
  * AniList  - "Ecchi" / "Hentai" genres, the isAdult flag, tags in the "Sexual Content" category (or adult tags)
  * Kitsu    - nsfw flag, age rating R18, rating notes that mention nudity / sexual content
  * AnimeThemes - NSFW marks on individual opening/ending videos (also flags the video itself)
MyAnimeList genres (Ecchi / Hentai / Erotica) are added when Jikan is reachable.
"""
from __future__ import annotations

import json
import logging
import re
import time

from . import catalog, db
from .sources import anilist, kitsu

log = logging.getLogger("kanade.content")

SEXUAL_RANK = 40                 # AniList tag relevance (0-100) needed for a sexual-content tag to count
GUIDE_RE = re.compile(r"nudity|sexual|ecchi|fan ?service|erotic", re.I)


def classify(src: dict) -> tuple[int, int, list[str]]:
    """(suggestive, explicit, reasons) from the collected source data."""
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
    if src.get("mal_genres"):
        hits = [g for g in src["mal_genres"] if g in ("Ecchi", "Hentai", "Erotica")]
        if hits:
            reasons.append("MyAnimeList: " + ", ".join(hits))
            explicit = explicit or any(g in ("Hentai", "Erotica") for g in hits)
    suggestive = bool(reasons)
    if src.get("at_nsfw"):           # informational only: AnimeThemes also marks gore / flashing images NSFW
        reasons.append(f"AnimeThemes: {src['at_nsfw']} video(s) marked NSFW")
    return int(suggestive), int(explicit), reasons


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
    rows = db.q(f"SELECT id, at_id, anilist_id, mal_id FROM anime WHERE {where}id IN (SELECT anime_id FROM v_video) "
                "ORDER BY id")
    total, done, updated = len(rows), 0, 0
    if total and on_start:
        on_start(total)                       # only now is there something to fetch (and a progress bar to show)
    for i in range(0, total, CHUNK):
        if should_stop():
            break
        chunk = rows[i:i + CHUNK]
        src, complete = _collect(chunk)
        now = time.time()
        with db.tx() as c:
            for aid, s in src.items():
                sug, exp, reasons = classify(s)
                c.execute("UPDATE anime SET content=?, suggestive=?, explicit=?, content_at=? WHERE id=?",
                          (json.dumps({**s, "reasons": reasons}, ensure_ascii=False), sug, exp,
                           now if complete else 0, aid))
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
