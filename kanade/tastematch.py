"""Match liked-folder files (any naming: YouTube downloads, fansub NC files...) to library videos of the same
opening, by series title + OP/ED + number. The For You engine confirms each candidate by sound and then learns
from the library copy, which sounds and looks like the videos it ranks.

Only names are compared here (no file is opened); results go to the taste_match table.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

from rapidfuzz import fuzz, process

from . import db

log = logging.getLogger("kanade.tastematch")

VIDEO_EXTS = {".webm", ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".mpg", ".mpeg", ".ts"}
TYPE_RE = re.compile(r"(?<![a-z])(nc)?(op|opening|openning|ed|ending)\s*[-_ ]?\s*(\d{1,2})?(?![a-z])", re.I)
NOISE = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}|「[^」]*」|【[^】]*】|- youtube|youtube|on vimeo|\bhd\b|\bhq\b|"
                   r"\b(bd|dvd|web)(rip)?\b|\d{3,4}p|\b(x26[45]|h26[45]|aac|flac|10 ?bit|creditless|non-credit|"
                   r"full|version|tv size|eng ?sub(bed)?|hard ?subbed|subbed|subs|lyrics|karaoke|amv)\b", re.I)
MIN_SCORE = 80.0          # title similarity (0-100) for a series to be considered
SPREAD = 8.0              # also keep series scoring within this of the best (sequels, alternative titles)


def _norm(t: str) -> str:
    t = re.sub(r"[^0-9a-z]+", " ", t.lower()).strip()
    return re.sub(r"\b(season|s)\s*(\d)\b", r"\2", t)


def parse(name: str):
    """'Bleach - Opening 4 (HD)' -> ('bleach', 'OP', 4); type/number are None when the name doesn't say."""
    s = name.replace("_", " ").replace(".", " ")
    s = NOISE.sub(" ", s)
    m = TYPE_RE.search(s)
    typ = num = None
    title = s
    if m:
        typ = "OP" if m.group(2).lower().startswith("op") else "ED"
        num = int(m.group(3)) if m.group(3) else None
        title = s[:m.start()]
        if len(re.sub(r"[^a-z]", "", title.lower())) < 3:      # "OP - Series" style
            title = s[m.end():]
    return _norm(title), typ, num


def list_files(folders):
    out = {}
    for folder in folders:
        for dp, _, fs in os.walk(folder):
            for f in fs:
                if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                    p = os.path.join(dp, f)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    out[p] = f"{st.st_size}:{int(st.st_mtime)}"
    return out


def refresh(folders) -> int:
    """Re-match when the liked files or the library changed. Returns the number of files with candidates."""
    files = list_files([f for f in folders if os.path.isdir(f)])
    lib = db.q1("SELECT COUNT(*) AS n, MAX(id) AS m, (SELECT COUNT(*) FROM user_override) AS o FROM video WHERE missing=0")
    sig = json.dumps([sorted(files.items()), lib["n"], lib["m"], lib["o"]])
    if db.meta_get("taste_match_sig") == sig:
        return -1
    t0 = time.time()
    choices: dict[str, set[int]] = {}
    for r in db.q("SELECT a.id, a.title_romaji, a.title_english, a.title_mal, a.synonyms, ao.title_romaji AS o_r, "
                  "ao.title_english AS o_e FROM anime a LEFT JOIN anime_override ao ON ao.anime_id=a.id "
                  "WHERE a.id IN (SELECT anime_id FROM v_video WHERE missing=0)"):
        try:
            syn = json.loads(r["synonyms"] or "[]")
        except ValueError:
            syn = []
        for t in [r["title_romaji"], r["title_english"], r["title_mal"], r["o_r"], r["o_e"], *syn]:
            k = _norm(t) if t else ""
            if len(k) >= 2:
                choices.setdefault(k, set()).add(r["id"])
    themes: dict[int, list[tuple]] = {}
    for r in db.q("SELECT id, anime_id, type, number FROM v_video WHERE missing=0 AND anime_id IS NOT NULL"):
        themes.setdefault(r["anime_id"], []).append((r["type"], r["number"], r["id"]))
    keys = list(choices)
    rows, found = [], 0
    for path, fp in files.items():
        title, typ, num = parse(os.path.splitext(os.path.basename(path))[0])
        cands, best = [], 0.0
        if title:
            hits = process.extract(title, keys, scorer=fuzz.WRatio, limit=6)
            best = hits[0][1] if hits else 0.0
            anime = set()
            for k, sc, _ in hits:
                if sc >= MIN_SCORE and sc >= best - SPREAD:
                    anime |= choices[k]
            for aid in anime:
                for t, n, vid in themes.get(aid, []):
                    if (typ is None or t == typ) and (num is None or n == num):
                        cands.append(vid)
        found += bool(cands)
        rows.append((path, fp, best, json.dumps(sorted(set(cands))), time.time()))
    with db.tx() as c:
        c.execute("DELETE FROM taste_match")
        c.executemany("INSERT INTO taste_match(path, fp, score, cands, at) VALUES(?,?,?,?,?)", rows)
        c.execute("INSERT INTO meta(key,value) VALUES('taste_match_sig',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (sig,))
    log.info("liked files matched to library openings: %d of %d (%.1f s)", found, len(files), time.time() - t0)
    return found
