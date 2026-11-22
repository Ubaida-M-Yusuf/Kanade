"""Mirror of the AnimeThemes catalogue in the local DB (cat_anime / cat_video)."""
from __future__ import annotations

import json
import logging
import time

from . import db
from .sources import animethemes as at

log = logging.getLogger("kanade.catalog")


def catalog_age_days() -> float | None:
    ts = db.meta_get("catalog_synced_at")
    if not ts:
        return None
    return (time.time() - float(ts)) / 86400


def catalog_size() -> int:
    return db.q1("SELECT COUNT(*) n FROM cat_anime")["n"]


def store_anime_nodes(nodes: list[dict], conn):
    for a in nodes:
        if not a or not a.get("id"):
            continue
        s = at.anime_summary(a)
        conn.execute("INSERT INTO cat_anime(at_id,slug,name,year,data) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(at_id) DO UPDATE SET slug=excluded.slug,name=excluded.name,"
                     "year=excluded.year,data=excluded.data",
                     (a["id"], a.get("slug"), s["title_romaji"], s["year"], json.dumps(a, ensure_ascii=False)))
        conn.execute("DELETE FROM cat_video WHERE at_anime_id=?", (a["id"],))
        conn.executemany(
            "INSERT INTO cat_video(basename_l,basename,prefix_l,at_anime_id,type,number,part,version,slug,"
            "song_title,artists,episodes,resolution,source,tags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(v["basename_l"], v["basename"], v["prefix_l"], a["id"], v["type"], v["number"], v["part"],
              v["version"], v["slug"], v["song_title"], v["artists"], v["episodes"], v["resolution"],
              v["source"], v["tags"]) for v in at.iter_videos(a)])


def sync(progress=None, page_size: int = 100) -> int:
    """Download the full AnimeThemes catalogue. progress(done, total, message)."""
    page, last = 1, None
    fetched = 0
    while True:
        info, nodes = at.fetch_catalog_page(page, page_size, max_age=0)
        last = info.get("lastPage") or last or page
        with db.tx() as c:
            store_anime_nodes(nodes, c)
        fetched += len(nodes)
        if progress:
            progress(page, last, f"AnimeThemes catalogue page {page}/{last}")
        log.info("catalogue page %s/%s (%s anime)", page, last, len(nodes))
        if not info.get("hasMorePages") or not nodes:
            break
        page += 1
    db.meta_set("catalog_synced_at", time.time())
    return fetched


def lookup_basename(filename: str) -> list:
    return db.q("SELECT * FROM cat_video WHERE basename_l=?", (filename.lower(),))


def anime_node(at_id: int) -> dict | None:
    row = db.q1("SELECT data FROM cat_anime WHERE at_id=?", (at_id,))
    return json.loads(row["data"]) if row else None
