"""Local HTTP server (127.0.0.1 only) that serves the UI, JSON API, thumbnails, covers and
range-enabled video streaming for hover previews. A random per-session token protects the API."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import secrets
import socket
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import APP_NAME, __version__, activity, catalog, db, library, media, recommend, search
from .config import data_dir, resource_dir, settings
from .jobs import manager
from .net import HostDown
from .sources import ollama

log = logging.getLogger("kanade.server")

TOKEN = secrets.token_urlsafe(24)
UI_DIR = resource_dir() / "ui"
pick_folder_hook = None  # set by app.py when running inside pywebview
last_api_request = 0.0  # used by --exit-when-idle to know whether a window is still open

mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("text/javascript", ".js")


class RawJSON:
    """Pre-encoded JSON body."""
    __slots__ = ("data",)

    def __init__(self, data: bytes):
        self.data = data


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


_cache_lock = threading.Lock()
_cache: dict = {}


def _cached(name: str, key: str, build):
    """Tiny memo keyed by the change counters, so repeated polls cost nothing."""
    with _cache_lock:
        hit = _cache.get(name)
        if hit and hit[0] == key:
            return hit[1]
    value = build()
    with _cache_lock:
        _cache[name] = (key, value)
    return value


def counts_cached():
    return _cached("counts", db.version("library") + db.version("fav"), library.counts)


def videos_payload() -> bytes:
    """The whole library list, JSON-encoded once per change and then served from memory."""
    key = db.version("library") + db.version("fav")

    def build():
        now = time.time()
        payload = {"cols": library.VIDEO_COLS, "rows": library.list_videos(), "counts": counts_cached(),
                   "version": db.version("library"), "media_version": db.version("media"), "media_ts": now}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return _cached("videos", key, build)


def media_updates(since: float) -> dict:
    """Thumbnails / durations / covers that became available after `since` (small, incremental)."""
    now = time.time()
    vids = db.q("SELECT id, thumb_state, duration, width, height, CAST(media_at AS INTEGER) AS tv FROM video "
                "WHERE media_at > ? AND missing=0 LIMIT 25000", (since,))
    anime = db.q("SELECT id FROM anime WHERE cover_at > ? AND cover_path IS NOT NULL", (since,))
    return {"now": now, "version": db.version("media"),
            "videos": [[r["id"], r["thumb_state"], r["duration"], r["width"], r["height"], r["tv"]] for r in vids],
            "anime": [r["id"] for r in anime]}


def _ids(body) -> list[int]:
    ids = body.get("ids")
    if ids is None and body.get("id") is not None:
        ids = [body["id"]]
    if not isinstance(ids, list) or not ids:
        raise ApiError("ids required")
    return [int(i) for i in ids]


# ------------------------------------------------------------------ API routes

def api_get(path: str, qs: dict):
    g = lambda k, d=None: (qs.get(k) or [d])[0]
    if path == "/api/bootstrap":
        return {"app": APP_NAME, "version": __version__, "settings": settings().as_dict(),
                "tools": media.tools_status(), "counts": counts_cached(),
                "catalog": {"size": catalog.catalog_size(), "age_days": catalog.catalog_age_days()},
                "data_dir": str(data_dir()), "status": manager().status(), "taste": recommend.engine_status(),
                "root_exists": bool(settings().get("archive_root")) and os.path.isdir(settings().get("archive_root"))}
    if path == "/api/videos":
        return RawJSON(videos_payload())
    if path == "/api/foryou":
        return {**recommend.recommendations(), "status": recommend.engine_status(),
                "progress": manager().recs.as_dict()}
    if path == "/api/media-updates":
        return media_updates(float(g("since", "0") or 0))
    if path == "/api/anime":
        return _cached("anime", db.version("library") + db.version("fav") + db.version("media"),
                       lambda: {"rows": library.list_anime()})
    m = re.fullmatch(r"/api/anime/(\d+)", path)
    if m:
        d = library.anime_detail(int(m.group(1)))
        if not d:
            raise ApiError("not found", 404)
        return d
    m = re.fullmatch(r"/api/video/(\d+)", path)
    if m:
        d = library.video_detail(int(m.group(1)))
        if not d:
            raise ApiError("not found", 404)
        return d
    if path == "/api/search":
        return {"ids": search.search_ids(g("q", ""))}
    if path == "/api/status":
        st = manager().status()
        st["counts"] = counts_cached()
        return st
    if path == "/api/engine/info":
        return recommend.engine_info()
    if path == "/api/remote-search":
        try:
            return {"results": library.remote_search(g("q", ""), g("source", "library"))}
        except HostDown as e:
            return {"results": [], "error": f"{e} is unreachable from this network right now. Try another source."}
        except Exception as e:
            return {"results": [], "error": f"{type(e).__name__}: {e}"}
    if path == "/api/ollama":
        ok = ollama.available()
        return {"available": ok, "models": ollama.models() if ok else []}
    raise ApiError("unknown endpoint", 404)


def api_post(path: str, body: dict):
    jm = manager()
    if path == "/api/favorite":
        library.set_favorite(_ids(body), bool(body.get("on", True)))
        db.bump("fav")
        jm.taste_changed()
        return {"ok": True}
    if path == "/api/rate":
        value = int(body.get("value", 0))
        if value not in (1, -1, 0):
            raise ApiError("value must be 1, -1 or 0")
        library.set_rating(_ids(body), value)
        db.bump("fav")
        jm.taste_changed()
        return {"ok": True}
    if path == "/api/foryou/learn":
        return {"learning": jm.taste_changed(immediate=True)}
    if path in ("/api/copy/plan", "/api/copy/start"):
        from . import copier
        try:
            if path == "/api/copy/plan":
                p = copier.plan(_ids(body), str(body.get("dest") or ""))
                return {k: p[k] for k in ("dest", "count", "bytes", "free", "enough_space", "renamed", "missing")}
            return jm.start_copy(_ids(body), str(body.get("dest") or ""))
        except ValueError as e:
            raise ApiError(str(e))
    if path == "/api/play":
        activity.touch_play()
        library.play(int(body["id"]))
        return {"ok": True}
    if path == "/api/reveal":
        library.reveal(int(body["id"]))
        return {"ok": True}
    if path == "/api/scan":
        return {"queued": jm.submit("scan")}
    if path == "/api/sync-catalog":
        return {"queued": jm.submit("sync_catalog")}
    if path == "/api/rematch":
        if body.get("all"):
            return {"queued": jm.submit("rematch", all=True)}
        return {"queued": jm.submit("rematch", ids=_ids(body))}
    if path == "/api/taste":
        folders = [str(f).strip() for f in body.get("folders", []) if str(f).strip()]
        missing = [f for f in folders if not os.path.isdir(f)]
        if missing:
            raise ApiError("Folder not found: " + missing[0])
        settings().update({"taste_folders": folders,
                           "taste_use_favorites": bool(body.get("use_favorites", True))})
        jm.request_recs("analyze")
        return {"ok": True, "status": recommend.engine_status()}
    if path == "/api/taste-group-name":
        names = dict(settings().get("taste_group_names") or {})
        key, name = str(body.get("key") or ""), str(body.get("name") or "").strip()
        if not key:
            raise ApiError("group key required")
        if name:
            names[key] = name[:60]
        else:
            names.pop(key, None)            # empty name: back to the automatic one
        settings().update({"taste_group_names": names})
        return {"ok": True}
    if path == "/api/engine/setup":
        jm.request_recs("setup")
        return {"queued": True}
    if path == "/api/engine/analyze":
        jm.request_recs("analyze")
        return {"queued": True}
    if path == "/api/enrich-mal":
        return {"queued": jm.submit("enrich_mal")}
    if path == "/api/fill-genres":
        with db.tx() as c:
            c.execute("UPDATE anime SET genres_at=0 WHERE genres='[]'")
        return {"queued": jm.submit("fill_genres")}
    if path == "/api/fill-relations":
        return {"queued": jm.submit("fill_relations", all=True)}
    if path == "/api/fill-content":
        return {"queued": jm.submit("fill_content", all=True)}
    if path == "/api/fill-anilist":
        with db.tx() as c:
            c.execute("UPDATE anime SET titles_at=0")
        return {"queued": jm.submit("fill_from_anilist")}
    if path == "/api/thumbs/retry":
        with db.tx() as c:
            c.execute("UPDATE video SET thumb_state=0 WHERE thumb_state=-1 AND missing=0")
            c.execute("UPDATE video SET probe_state=0 WHERE probe_state=-1 AND missing=0")
        jm.wake_thumbs()
        return {"ok": True}
    if path == "/api/override":
        ids = _ids(body)
        if body.get("video"):
            library.save_video_override(ids, body["video"])
        if body.get("anime") and body.get("anime_id"):
            library.save_anime_override(int(body["anime_id"]), body["anime"])
        db.bump_library_version()
        return {"ok": True}
    if path == "/api/override/clear":
        library.clear_overrides(_ids(body))
        db.bump_library_version()
        return {"ok": True}
    if path == "/api/assign":
        aid = library.assign(_ids(body), body.get("target") or {})
        jm.submit("rematch", ids=[])  # triggers cover download for a newly added series
        db.bump_library_version()
        return {"ok": True, "anime_id": aid}
    if path == "/api/accept":
        n = library.accept_suggestions(_ids(body))
        jm.submit("rematch", ids=[])
        db.bump_library_version()
        return {"ok": True, "accepted": n}
    if path == "/api/settings":
        before = settings().get("archive_root")
        settings().update(body or {})
        if settings().get("archive_root") != before and settings().get("archive_root"):
            jm.submit("scan")
        return {"ok": True, "settings": settings().as_dict(), "tools": media.tools_status()}
    if path == "/api/pick-folder":
        if pick_folder_hook is None:
            raise ApiError("folder picker only available in the desktop window")
        return {"path": pick_folder_hook(body.get("kind", "folder"))}
    if path == "/api/open-url":
        url = str(body.get("url") or "")
        host = urlparse(url).hostname or ""
        allowed = ("myanimelist.net", "anilist.co", "animethemes.moe")
        if not url.startswith("https://") or not any(host == h or host.endswith("." + h) for h in allowed):
            raise ApiError("link not allowed")
        webbrowser.open(url)
        return {"ok": True}
    if path == "/api/open-data-dir":
        from . import osutil
        osutil.open_path(str(data_dir()))
        return {"ok": True}
    raise ApiError("unknown endpoint", 404)


# ------------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def finish(self):
        try:
            super().finish()
        finally:
            db.close_thread_connection()  # every connection thread opened its own SQLite handle

    def _authorised(self, qs) -> bool:
        tok = self.headers.get("X-Kanade-Token") or (qs.get("k") or [None])[0]
        return tok is not None and secrets.compare_digest(tok, TOKEN)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def _json(self, obj, code=200):
        if isinstance(obj, RawJSON):
            return self._send(code, obj.data, "application/json; charset=utf-8", {"Cache-Control": "no-store"})
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _file(self, path: Path | str, cache=True):
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError:
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, data, ctype, {"Cache-Control": "private, max-age=604800" if cache else "no-cache"})

    def _stream(self, path: str):
        """Serve a video with HTTP Range support (read-only)."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(path)[0] or "video/webm"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                code = 206
        if start > end:
            return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        global last_api_request
        u = urlparse(self.path)
        path, qs = unquote(u.path), parse_qs(u.query)
        if path.startswith("/api/"):
            last_api_request = time.time()
            if path not in ("/api/status", "/api/media-updates"):
                activity.touch_ui()
        elif path.startswith("/media/"):
            activity.touch_media()
        try:
            if path in ("/", "/index.html"):
                html = (UI_DIR / "index.html").read_text(encoding="utf-8").replace("%%TOKEN%%", TOKEN)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            if path.startswith("/ui/"):
                target = (UI_DIR / path[4:]).resolve()
                if UI_DIR.resolve() not in target.parents:
                    return self._send(403, b"forbidden", "text/plain")
                return self._file(target, cache=False)
            if not self._authorised(qs):
                return self._send(403, b"forbidden", "text/plain")
            m = re.fullmatch(r"/thumb/(\d+)", path)
            if m:
                r = db.q1("SELECT thumb_path FROM video WHERE id=?", (int(m.group(1)),))
                if r and r["thumb_path"]:
                    return self._file(r["thumb_path"])
                return self._send(404, b"", "text/plain")
            m = re.fullmatch(r"/cover/(\d+)", path)
            if m:
                r = db.q1("SELECT cover_path FROM anime WHERE id=?", (int(m.group(1)),))
                if r and r["cover_path"]:
                    return self._file(r["cover_path"])
                return self._send(404, b"", "text/plain")
            m = re.fullmatch(r"/media/(\d+)", path)
            if m:
                r = db.q1("SELECT path FROM video WHERE id=?", (int(m.group(1)),))
                if not r:
                    return self._send(404, b"", "text/plain")
                return self._stream(r["path"])
            if path.startswith("/api/"):
                return self._json(api_get(path, qs))
            self._send(404, b"not found", "text/plain")
        except ApiError as e:
            self._json({"error": str(e)}, e.code)
        except Exception as e:
            log.error("GET %s failed: %s\n%s", path, e, traceback.format_exc())
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        path, qs = unquote(u.path), parse_qs(u.query)
        try:
            if not self._authorised(qs):
                return self._send(403, b"forbidden", "text/plain")
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            body = json.loads(raw or b"{}")
            return self._json(api_post(path, body))
        except ApiError as e:
            self._json({"error": str(e)}, e.code)
        except FileNotFoundError as e:
            self._json({"error": str(e)}, 404)
        except Exception as e:
            log.error("POST %s failed: %s\n%s", path, e, traceback.format_exc())
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # The window aborting a request (page reload, scrolling past an image) is normal: don't spam tracebacks.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        log.error("request from %s failed: %s", client_address, traceback.format_exc())


def start(port: int | None = None) -> tuple[ThreadingHTTPServer, str]:
    port = port or free_port()
    httpd = QuietServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    log.info("UI server at %s", url)
    return httpd, url
