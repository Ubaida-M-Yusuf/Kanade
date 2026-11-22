"""Full-text search (SQLite FTS5) across titles, synonyms, songs, artists, filenames and OP/ED numbers."""
from __future__ import annotations

import json
import re
import unicodedata

from . import db
from .parser import decamel

_CJK = re.compile(r"([぀-ヿ㐀-䶿一-鿿가-힯ｦ-ﾟ])")


def _spread_cjk(text: str) -> str:
    """Put spaces between CJK characters so substrings are searchable as phrases."""
    return _CJK.sub(r" \1 ", text)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    return _spread_cjk(t)


def body_for(row) -> str:
    """Build the searchable text for one v_video row."""
    parts = [row["title"], row["title_english"], row["title_japanese"], row["song_title"], row["artists"],
             row["filename"], row["p_prefix"], row["p_title"], row["folder"], row["folder_season"],
             str(row["year"] or ""), row["season"]]
    for col in ("synonyms", "anime_genres", "anime_tags"):
        try:
            parts += json.loads(row[col] or "[]")
        except (ValueError, IndexError, KeyError):
            pass
    if row["p_prefix"]:
        parts.append(decamel(row["p_prefix"]))
        parts.append(row["p_prefix"].lower())
    t, n, ver = row["type"], row["number"], row["version"]
    if t:
        long = "opening" if t == "OP" else "ending"
        parts += [f"{t}{n}", f"{t} {n}", long, f"{long}{n}", f"{t}{n}v{ver}" if ver and ver > 1 else ""]
    # compressed forms so 'bocchitherock' and 'shingekinokyojin' match too
    for title in (row["title"], row["title_english"]):
        if title:
            parts.append(re.sub(r"[^0-9A-Za-z]+", "", unicodedata.normalize("NFKD", title)))
    return " ".join(_clean(p) for p in parts if p)


def reindex(video_ids: list[int] | None = None):
    if video_ids is not None and not video_ids:
        return
    conn = db.connect()
    if video_ids is None:
        rows = conn.execute("SELECT * FROM v_video").fetchall()
    else:
        rows = []
        for i in range(0, len(video_ids), 500):
            chunk = video_ids[i:i + 500]
            rows += conn.execute(f"SELECT * FROM v_video WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
    with db.tx() as c:
        if video_ids is None:
            c.execute("DELETE FROM video_fts")
        else:
            for i in range(0, len(video_ids), 500):
                chunk = video_ids[i:i + 500]
                c.execute(f"DELETE FROM video_fts WHERE rowid IN ({','.join('?' * len(chunk))})", chunk)
        c.executemany("INSERT INTO video_fts(rowid, body) VALUES(?,?)", [(r["id"], body_for(r)) for r in rows])


def reindex_anime(anime_id: int):
    ids = [r["id"] for r in db.q("SELECT id FROM v_video WHERE anime_id=?", (anime_id,))]
    reindex(ids)


def build_query(text: str) -> str | None:
    text = unicodedata.normalize("NFKC", text or "").strip()
    if not text:
        return None
    text = _spread_cjk(text)
    tokens = re.findall(r"\w+", text, flags=re.UNICODE)
    if not tokens:
        return None
    out = []
    for i, tok in enumerate(tokens):
        tok = tok.replace('"', "")
        # prefix-match every token ("bocchi op1" -> "bocchi"* AND "op1"*)
        if _CJK.fullmatch(tok):
            out.append(f'"{tok}"')
        else:
            out.append(f'"{tok}"*')
    return " AND ".join(out)


def search_ids(text: str, limit: int = 20000) -> list[int]:
    fts = build_query(text)
    if not fts:
        return []
    try:
        rows = db.q("SELECT rowid FROM video_fts WHERE video_fts MATCH ? ORDER BY rank LIMIT ?", (fts, limit))
    except Exception:
        return []
    return [r["rowid"] for r in rows]
