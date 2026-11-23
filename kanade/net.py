"""HTTP client shared by all metadata sources.

* per-host throttling (minimum interval + rolling per-minute cap)
* exponential backoff with Retry-After support on 429 / 5xx / network errors
* on-disk response cache (so re-runs and offline use work)
* a simple circuit breaker: a host that keeps failing is skipped for a while
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import APP_NAME, __version__
from .config import data_dir

log = logging.getLogger("kanade.net")

USER_AGENT = f"{APP_NAME}/{__version__} (local anime OP/ED library)"

# host -> (min seconds between requests, max requests per minute)
LIMITS = {
    "graphql.animethemes.moe": (0.75, 80),
    "api.animethemes.moe": (0.75, 80),
    "api.jikan.moe": (0.4, 55),
    "graphql.anilist.co": (2.1, 28),
    "kitsu.io": (0.5, 100),
    "api.myanimelist.net": (1.0, 55),        # faster bursts get connections refused for a while
}
DEFAULT_LIMIT = (0.2, 240)


class HostDown(Exception):
    pass


class _Throttle:
    def __init__(self, min_interval: float, per_minute: int):
        self.min_interval = min_interval
        self.per_minute = per_minute
        self.lock = threading.Lock()
        self.last = 0.0
        self.window: deque[float] = deque()

    def wait(self):
        while True:
            with self.lock:
                now = time.monotonic()
                while self.window and now - self.window[0] > 60:
                    self.window.popleft()
                delay = max(0.0, self.last + self.min_interval - now)
                if len(self.window) >= self.per_minute:
                    delay = max(delay, 60 - (now - self.window[0]) + 0.05)
                if delay <= 0:
                    self.last = now
                    self.window.append(now)
                    return
            time.sleep(min(delay, 2.0))

    def penalise(self, seconds: float):
        with self.lock:
            self.last = time.monotonic() + seconds - self.min_interval


class Http:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.session.headers["Accept"] = "application/json"
        self._throttles: dict[str, _Throttle] = {}
        self._tlock = threading.Lock()
        self._down_until: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        self.cache_dir = data_dir() / "cache" / "http"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.offline = False

    # -- helpers ---------------------------------------------------------
    def _throttle(self, host: str) -> _Throttle:
        with self._tlock:
            t = self._throttles.get(host)
            if t is None:
                t = _Throttle(*LIMITS.get(host, DEFAULT_LIMIT))
                self._throttles[host] = t
            return t

    def _cache_file(self, key: str) -> Path:
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def cache_get(self, key: str, max_age: float | None):
        f = self._cache_file(key)
        if not f.exists():
            return None
        if max_age is not None and time.time() - f.stat().st_mtime > max_age:
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None

    def cache_put(self, key: str, value):
        f = self._cache_file(key)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)

    def host_available(self, host: str) -> bool:
        return time.time() >= self._down_until.get(host, 0)

    def host_free_at(self, host: str) -> float:
        """When a host that kept failing will be tried again (0 = now)."""
        return self._down_until.get(host, 0)

    def _mark_fail(self, host: str):
        n = self._fails.get(host, 0) + 1
        self._fails[host] = n
        if n >= 3:
            self._down_until[host] = time.time() + 1800
            log.warning("%s keeps failing; skipping it for 30 minutes", host)

    def _mark_ok(self, host: str):
        self._fails[host] = 0
        self._down_until.pop(host, None)

    # -- main entry point ----------------------------------------------
    def request_json(self, method: str, url: str, *, params=None, json_body=None, headers=None,
                     cache_key: str | None = None, max_age: float | None = 30 * 86400,
                     timeout: float = 30, retries: int = 4, allow_stale: bool = True):
        key = cache_key or (method + " " + url + " " + json.dumps(params, sort_keys=True) + " " +
                            json.dumps(json_body, sort_keys=True))
        if max_age != 0:
            cached = self.cache_get(key, max_age)
            if cached is not None:
                return cached
        host = urlparse(url).hostname or ""
        if self.offline or not self.host_available(host):
            stale = self.cache_get(key, None) if allow_stale else None
            if stale is not None:
                return stale
            raise HostDown(host)
        throttle = self._throttle(host)
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            throttle.wait()
            try:
                r = self.session.request(method, url, params=params, json=json_body, headers=headers,
                                         timeout=(min(6.0, timeout), timeout))
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                self._mark_fail(host)
                if not self.host_available(host):
                    break
                time.sleep(min(30, (2 ** attempt) + random.random()))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                retry_after = r.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else (2 ** attempt) * 1.5
                except ValueError:
                    wait = (2 ** attempt) * 1.5
                wait = min(90.0, wait + random.random())
                log.info("%s -> HTTP %s, retrying in %.1fs", host, r.status_code, wait)
                throttle.penalise(wait)
                last_exc = requests.HTTPError(f"HTTP {r.status_code}")
                if r.status_code >= 500:
                    self._mark_fail(host)
                    if not self.host_available(host):
                        break
                time.sleep(wait)
                continue
            if r.status_code == 404:
                self._mark_ok(host)
                return None
            if r.status_code >= 400:
                self._mark_ok(host)
                raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:300]}")
            self._mark_ok(host)
            try:
                data = r.json()
            except ValueError as e:
                raise requests.HTTPError(f"invalid JSON from {host}: {e}")
            if max_age != 0 or cache_key:
                self.cache_put(key, data)
            return data
        stale = self.cache_get(key, None) if allow_stale else None
        if stale is not None:
            return stale
        if last_exc and not self.host_available(host):
            raise HostDown(host) from last_exc
        raise last_exc or HostDown(host)

    def download(self, url: str, dest: Path, timeout: float = 60) -> bool:
        host = urlparse(url).hostname or ""
        if self.offline or not self.host_available(host):
            return False
        throttle = self._throttle(host)
        for attempt in range(4):
            throttle.wait()
            try:
                with self.session.get(url, timeout=timeout, stream=True, headers={"Accept": "*/*"}) as r:
                    if r.status_code == 404:
                        return False
                    if r.status_code == 429 or r.status_code >= 500:
                        time.sleep((2 ** attempt) + random.random())
                        continue
                    r.raise_for_status()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_content(65536):
                            fh.write(chunk)
                    tmp.replace(dest)
                    self._mark_ok(host)
                    return True
            except (requests.ConnectionError, requests.Timeout):
                self._mark_fail(host)
                if not self.host_available(host):
                    return False
                time.sleep((2 ** attempt) + random.random())
        return False


_http: Http | None = None
_hlock = threading.Lock()


def http() -> Http:
    global _http
    with _hlock:
        if _http is None:
            _http = Http()
        return _http
