"""SQLite database: schema, connections and small helpers.

Every thread gets its own connection (WAL mode, so readers never block the writer).
Writes are serialised with a process-wide lock to avoid 'database is locked' churn.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .config import data_dir

SCHEMA_VERSION = 1

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per anime series (a season/entry as AnimeThemes and MAL define it).
CREATE TABLE IF NOT EXISTS anime(
    id             INTEGER PRIMARY KEY,
    at_id          INTEGER UNIQUE,          -- AnimeThemes anime id
    at_slug        TEXT,
    mal_id         INTEGER,
    anilist_id     INTEGER,
    title_romaji   TEXT NOT NULL,           -- main (MyAnimeList-style) romaji title
    title_mal      TEXT,                    -- title fetched from Jikan when available (wins for display)
    title_english  TEXT,
    title_japanese TEXT,
    synonyms       TEXT NOT NULL DEFAULT '[]',  -- JSON array
    year           INTEGER,
    season         TEXT,
    format         TEXT,
    cover_url      TEXT,
    cover_path     TEXT,
    source         TEXT,                    -- animethemes | anilist | jikan | manual
    created_at     REAL,
    updated_at     REAL
);
CREATE INDEX IF NOT EXISTS anime_mal ON anime(mal_id);
CREATE INDEX IF NOT EXISTS anime_anilist ON anime(anilist_id);

-- One row per theme entry: OP/ED + number (+ part letter) + version.
CREATE TABLE IF NOT EXISTS theme(
    id         INTEGER PRIMARY KEY,
    anime_id   INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    type       TEXT NOT NULL CHECK (type IN ('OP','ED')),
    number     INTEGER NOT NULL DEFAULT 1,
    part       TEXT NOT NULL DEFAULT '',
    version    INTEGER NOT NULL DEFAULT 1,
    song_title TEXT,
    artists    TEXT,
    episodes   TEXT,
    UNIQUE(anime_id, type, number, part, version)
);

-- One row per video file on disk.
CREATE TABLE IF NOT EXISTS video(
    id            INTEGER PRIMARY KEY,
    theme_id      INTEGER REFERENCES theme(id) ON DELETE SET NULL,
    anime_id      INTEGER REFERENCES anime(id) ON DELETE SET NULL,
    path          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    rel_path      TEXT,
    filename      TEXT NOT NULL,
    folder        TEXT,                     -- top-level folder: '2015', '90s', 'misc'
    folder_year   INTEGER,
    folder_season TEXT,
    size          INTEGER,
    mtime         REAL,
    fingerprint   TEXT,
    duration      REAL,
    width         INTEGER,
    height        INTEGER,
    probe_state   INTEGER NOT NULL DEFAULT 0,   -- 0 pending, 1 ok, -1 failed
    thumb_path    TEXT,
    thumb_state   INTEGER NOT NULL DEFAULT 0,   -- 0 pending, 1 ok, -1 failed
    p_prefix      TEXT,                     -- parsed from the filename
    p_title       TEXT,
    p_type        TEXT,
    p_number      INTEGER,
    p_part        TEXT,
    p_version     INTEGER,
    p_tags        TEXT,
    match_source  TEXT,
    confidence    REAL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | matched | review
    suggestion    TEXT,                     -- JSON: best candidate for Needs Review
    date_added    REAL,
    missing       INTEGER NOT NULL DEFAULT 0,
    duplicate_of  INTEGER
);
CREATE INDEX IF NOT EXISTS video_fp ON video(fingerprint);
CREATE INDEX IF NOT EXISTS video_anime ON video(anime_id);
CREATE INDEX IF NOT EXISTS video_status ON video(status);

CREATE TABLE IF NOT EXISTS favorite(
    video_id INTEGER PRIMARY KEY REFERENCES video(id) ON DELETE CASCADE,
    added_at REAL
);

-- For You feedback: 1 = like, -1 = dislike (hidden from For You). The taste model learns from both.
CREATE TABLE IF NOT EXISTS rating(
    video_id INTEGER PRIMARY KEY REFERENCES video(id) ON DELETE CASCADE,
    value    INTEGER NOT NULL CHECK (value IN (1, -1)),
    at       REAL NOT NULL
);

-- Liked-folder files matched by name to library videos of the same opening (the engine confirms by sound).
CREATE TABLE IF NOT EXISTS taste_match(
    path  TEXT PRIMARY KEY,
    fp    TEXT,
    score REAL,
    cands TEXT NOT NULL DEFAULT '[]',    -- JSON list of candidate video ids
    at    REAL
);

-- Manual per-video corrections. Non-NULL columns always win over automatic data.
CREATE TABLE IF NOT EXISTS user_override(
    video_id   INTEGER PRIMARY KEY REFERENCES video(id) ON DELETE CASCADE,
    anime_id   INTEGER REFERENCES anime(id) ON DELETE SET NULL,
    type       TEXT,
    number     INTEGER,
    version    INTEGER,
    year       INTEGER,
    song_title TEXT,
    artists    TEXT,
    updated_at REAL
);

-- Manual per-series corrections (titles, year, season).
CREATE TABLE IF NOT EXISTS anime_override(
    anime_id       INTEGER PRIMARY KEY REFERENCES anime(id) ON DELETE CASCADE,
    title_romaji   TEXT,
    title_english  TEXT,
    title_japanese TEXT,
    year           INTEGER,
    season         TEXT,
    updated_at     REAL
);

-- Local copy of the AnimeThemes catalogue, used for exact basename matching offline.
CREATE TABLE IF NOT EXISTS cat_anime(
    at_id INTEGER PRIMARY KEY,
    slug  TEXT,
    name  TEXT,
    year  INTEGER,
    data  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cat_video(
    basename_l  TEXT NOT NULL,
    basename    TEXT NOT NULL,
    prefix_l    TEXT,
    at_anime_id INTEGER NOT NULL,
    type        TEXT,
    number      INTEGER,
    part        TEXT,
    version     INTEGER,
    slug        TEXT,
    song_title  TEXT,
    artists     TEXT,
    episodes    TEXT,
    resolution  INTEGER,
    source      TEXT,
    tags        TEXT
);
CREATE INDEX IF NOT EXISTS cat_video_b ON cat_video(basename_l);
CREATE INDEX IF NOT EXISTS cat_video_p ON cat_video(prefix_l);

CREATE VIRTUAL TABLE IF NOT EXISTS video_fts USING fts5(
    body, tokenize = "unicode61 remove_diacritics 2", prefix = '2 3'
);

"""

VIEW_SQL = r"""
-- Effective (override-applied) view of every video, used by the UI and search.
CREATE VIEW v_video AS
SELECT
    v.id, v.path, v.rel_path, v.filename, v.folder, v.folder_year, v.folder_season,
    v.size, v.mtime, v.duration, v.width, v.height, v.thumb_state, v.thumb_path,
    v.match_source, v.confidence, v.status, v.suggestion, v.date_added, v.missing, v.duplicate_of,
    v.p_prefix, v.p_title, v.p_tags, v.theme_id, CAST(v.media_at AS INTEGER) AS media_at, v.nsfw AS nsfw,
    a.suggestive AS suggestive, a.explicit AS explicit, a.content AS content,
    a.id AS anime_id,
    COALESCE(o.type, t.type, v.p_type) AS type,
    COALESCE(o.number, t.number, v.p_number, 1) AS number,
    COALESCE(t.part, v.p_part, '') AS part,
    COALESCE(o.version, t.version, v.p_version, 1) AS version,
    COALESCE(o.song_title, t.song_title) AS song_title,
    COALESCE(o.artists, t.artists) AS artists,
    t.episodes AS episodes,
    COALESCE(o.year, v.folder_year, ao.year, a.year) AS year,
    COALESCE(ao.title_romaji, a.title_mal, a.title_romaji, v.p_title) AS title,
    COALESCE(ao.title_english, a.title_english) AS title_english,
    COALESCE(ao.title_japanese, a.title_japanese) AS title_japanese,
    COALESCE(ao.season, a.season) AS season,
    a.year AS anime_year,
    a.synonyms AS synonyms, a.genres AS anime_genres, a.tags AS anime_tags,
    a.mal_id, a.anilist_id, a.at_id, a.at_slug, a.cover_path,
    (f.video_id IS NOT NULL) AS fav,
    f.added_at AS fav_at,
    COALESCE(rt.value, 0) AS rating,
    (o.video_id IS NOT NULL) AS has_override
FROM video v
LEFT JOIN user_override o ON o.video_id = v.id
LEFT JOIN anime a ON a.id = COALESCE(o.anime_id, v.anime_id)
LEFT JOIN anime_override ao ON ao.anime_id = a.id
LEFT JOIN theme t ON t.id = v.theme_id
LEFT JOIN favorite f ON f.video_id = v.id
LEFT JOIN rating rt ON rt.video_id = v.id;
"""

_local = threading.local()
write_lock = threading.RLock()
_db_path: Path | None = None


def db_path() -> Path:
    global _db_path
    if _db_path is None:
        _db_path = data_dir() / "library.db"
    return _db_path


def set_db_path(path: Path):
    global _db_path
    _db_path = path
    _local.__dict__.clear()


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(db_path(), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-8000")   # cap the page cache at ~8 MB per connection
        _local.conn = conn
    return conn


def close_thread_connection():
    """Close this thread's connection (used by short-lived HTTP worker threads)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None


def release_memory():
    """Give SQLite's page cache back to the OS after big jobs."""
    try:
        connect().execute("PRAGMA shrink_memory")
    except Exception:
        pass


def _migrate(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(video)")}
    if "media_at" not in cols:
        conn.execute("ALTER TABLE video ADD COLUMN media_at REAL NOT NULL DEFAULT 0")
    if "nsfw" not in cols:        # AnimeThemes marks this opening/ending video itself as NSFW
        conn.execute("ALTER TABLE video ADD COLUMN nsfw INTEGER NOT NULL DEFAULT 0")
    if "thumb_algo" not in cols:  # which frame-selection method produced the thumbnail
        conn.execute("ALTER TABLE video ADD COLUMN thumb_algo INTEGER NOT NULL DEFAULT 1")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(anime)")}
    if "cover_at" not in cols:
        conn.execute("ALTER TABLE anime ADD COLUMN cover_at REAL NOT NULL DEFAULT 0")
    if "content" not in cols:  # content ratings: sources JSON + combined flags
        conn.execute("ALTER TABLE anime ADD COLUMN content TEXT NOT NULL DEFAULT '{}'")
        conn.execute("ALTER TABLE anime ADD COLUMN suggestive INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE anime ADD COLUMN explicit INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE anime ADD COLUMN content_at REAL NOT NULL DEFAULT 0")
    if "genres" not in cols:  # AniList genres / tags (JSON arrays) and when they were fetched
        conn.execute("ALTER TABLE anime ADD COLUMN genres TEXT NOT NULL DEFAULT '[]'")
        conn.execute("ALTER TABLE anime ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        conn.execute("ALTER TABLE anime ADD COLUMN genres_at REAL NOT NULL DEFAULT 0")
    if "titles_at" not in cols:  # when AniList was last asked for missing English/Japanese titles (asked once)
        conn.execute("ALTER TABLE anime ADD COLUMN titles_at REAL NOT NULL DEFAULT 0")
    if "relations" not in cols:  # AniList relations (sequel, prequel, side story...) as JSON
        conn.execute("ALTER TABLE anime ADD COLUMN relations TEXT NOT NULL DEFAULT '{}'")
        conn.execute("ALTER TABLE anime ADD COLUMN relations_at REAL NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS video_media_at ON video(media_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS video_dup ON video(duplicate_of)")
    conn.execute("CREATE INDEX IF NOT EXISTS anime_cover_at ON anime(cover_at)")


def init():
    conn = connect()
    with write_lock:
        conn.executescript(SCHEMA)
        _migrate(conn)
        # views hold no data: always recreate so new columns appear in existing databases
        conn.execute("DROP VIEW IF EXISTS v_video")
        conn.executescript(VIEW_SQL)
        conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        conn.commit()


@contextmanager
def tx():
    """Serialised write transaction."""
    conn = connect()
    with write_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def q(sql: str, params=()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def q1(sql: str, params=()):
    return connect().execute(sql, params).fetchone()


def meta_get(key: str, default=None):
    row = q1("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else default


def meta_set(key: str, value):
    with tx() as c:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))


# In-memory change counters polled by the UI (no DB writes needed):
#   library -> structural changes (scan, matching, corrections): the UI reloads its lists
#   media   -> thumbnails / durations / covers became available: the UI fetches a small delta
#   fav     -> favourites changed (only invalidates the server-side list cache)
_versions = {"library": time.time(), "media": time.time(), "fav": 0.0, "recs": time.time()}
_vlock = threading.Lock()


def bump(kind: str = "library"):
    with _vlock:
        _versions[kind] = max(time.time(), _versions[kind] + 1e-6)


def version(kind: str = "library") -> str:
    with _vlock:
        return f"{_versions[kind]:.6f}"


def bump_library_version():
    bump("library")
