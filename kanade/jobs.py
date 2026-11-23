"""Background work: scan -> catalogue -> match -> covers, plus a parallel thumbnail worker pool."""
from __future__ import annotations

import gc
import logging
import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait

from . import activity, catalog, db, matcher, media, scanner, search
from .config import settings
from .net import HostDown

log = logging.getLogger("kanade.jobs")


class Notices:
    """Problems the user should see (bottom left, with a Retry) instead of only a log line. A notice goes away by
    itself when the same step works again."""

    def __init__(self):
        self._d: dict[str, dict] = {}
        self._lock = threading.Lock()

    def set(self, key: str, title: str, text: str, retry: str | None = None):
        with self._lock:
            self._d[key] = {"key": key, "title": title, "text": text, "retry": retry, "at": time.time()}
        log.warning("%s: %s", title, text)

    def clear(self, key: str):
        with self._lock:
            self._d.pop(key, None)

    def as_list(self) -> list[dict]:
        with self._lock:
            return sorted(self._d.values(), key=lambda x: x["at"])


AUTO_RETRY = "Kanade tries again by itself at the next library check (every 30 minutes), or press Retry."


class Progress:
    def __init__(self, name: str):
        self.name = name
        self.state = "idle"
        self.phase = ""
        self.done = 0
        self.total = 0
        self.current = ""
        self.message = ""
        self.error = None
        self.updated = time.time()
        self._last_log = 0.0

    def set(self, phase=None, done=None, total=None, current=None, message=None):
        if phase is not None:
            self.phase = phase
        if done is not None:
            self.done = done
        if total is not None:
            self.total = total
        if current is not None:
            self.current = current
        if message is not None:
            self.message = message
        self.updated = time.time()
        if time.time() - self._last_log > 3:
            self._last_log = time.time()
            log.info("[%s] %s %s/%s %s", self.name, self.phase, self.done, self.total or "?", self.current)

    def as_dict(self):
        return {"state": self.state, "phase": self.phase, "done": self.done, "total": self.total,
                "current": self.current, "message": self.message, "error": self.error}


class JobManager:
    def __init__(self):
        self.pipeline = Progress("library")
        self.thumbs = Progress("thumbnails")
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thumb_wake = threading.Event()
        self._thumbs_saved = threading.Event()
        self._busy = threading.Lock()
        self._cover_lock = threading.Lock()
        self.last_scan: dict | None = None
        self._started = time.time()
        from . import recommend
        self.recs = Progress("For You")
        self.copy = Progress("Copy")
        self.mal = Progress("MyAnimeList")       # genres / themes from MyAnimeList (own thread: never holds up scans)
        self._mal_lock = threading.Lock()
        self._mal_wake = threading.Event()
        self.notices = Notices()
        self.engine = recommend.EngineRunner(self.recs)
        self._rank_timer: threading.Timer | None = None
        self._rank_lock = threading.Lock()
        self._unlearned = 0                    # ratings / favourites changed since the model last learned
        threading.Thread(target=self._pipeline_loop, name="pipeline", daemon=True).start()
        threading.Thread(target=self._thumb_loop, name="thumbs", daemon=True).start()
        threading.Thread(target=self._auto_rescan_loop, name="auto-rescan", daemon=True).start()

    # ---------------------------------------------------------- public API
    def status(self) -> dict:
        return {"pipeline": self.pipeline.as_dict(), "thumbs": self.thumbs.as_dict(), "recs": self.recs.as_dict(),
                "copy": self.copy.as_dict(), "mal": self.mal.as_dict(), "notices": self.notices.as_list(),
                "library_version": db.version("library"), "media_version": db.version("media"),
                "recs_version": db.version("recs"), "last_scan": self.last_scan,
                # other windows of the app use these to pick up likes / favourites and settings changed elsewhere
                "fav_version": db.version("fav"), "settings_version": db.version("settings")}

    def request_recs(self, what: str = "analyze"):
        """Queue For You work if the engine is installed and there is something to learn from."""
        from . import recommend
        if what == "setup":
            self.engine.request("setup")
        elif recommend.taste_folders() or settings().get("taste_use_favorites", True):
            self.engine.request(what)          # without the engine / models it reports what is missing, never downloads

    LEARN_AFTER = 5            # new likes/dislikes learnt together...
    LEARN_IDLE_S = 30.0        # ...or after this long without another one

    def taste_changed(self, immediate: bool = False) -> bool:
        """Likes, dislikes or favourites changed. Hiding is instant in the UI; learning (a few seconds, no
        re-analysis) happens after LEARN_AFTER changes, after LEARN_IDLE_S without one, or right away when the
        UI asks (when the user leaves For You). Returns True if a learning run was requested."""
        with self._rank_lock:
            if not immediate:
                self._unlearned += 1
            if self._rank_timer:
                self._rank_timer.cancel()
                self._rank_timer = None
            if not self._unlearned:
                return False
            if immediate or self._unlearned >= self.LEARN_AFTER:
                self._unlearned = 0
                self.engine.learn()
                return True
            self._rank_timer = threading.Timer(self.LEARN_IDLE_S, lambda: self.taste_changed(immediate=True))
            self._rank_timer.daemon = True
            self._rank_timer.start()
            return False

    def idle(self) -> bool:
        return (self.pipeline.state != "running" and self.thumbs.state != "running"
                and self._q.empty() and not self._thumb_wake.is_set())

    def start_mal_list(self, force: bool = False):
        """Read the user's MyAnimeList list (in the background; a failure becomes a notice with Retry)."""
        from . import mallist

        def run():
            try:
                mallist.refresh(force)
                self.notices.clear("mal_list")
            except Exception as e:
                db.meta_set("mal_list_error", str(e)[:200])
                self.notices.set("mal_list", "Your MyAnimeList list", f"Couldn't read it: {str(e)[:160]}. "
                                 "The \"My list\" filter uses the last copy meanwhile.", "mal_list")
        threading.Thread(target=run, name="mal-list", daemon=True).start()

    RETRY_JOBS = {"scan": "scan", "identify": "scan", "catalog": "sync_catalog", "titles": "fill_from_anilist",
                  "genres": "fill_genres", "content": "fill_content", "relations": "fill_relations"}

    def retry(self, key: str) -> bool:
        """The Retry button of a notice (or of a failed job)."""
        from .net import http
        http().reset_hosts()
        self.notices.clear(key)
        if key in self.RETRY_JOBS:
            return self.submit(self.RETRY_JOBS[key])
        if key == "covers":
            threading.Thread(target=self._covers, kwargs={"quiet": True}, name="covers", daemon=True).start()
        elif key == "thumbs":
            self.wake_thumbs()
        elif key == "mal":
            self._mal_wake.set()
            self.start_mal()
        elif key == "foryou":
            self.request_recs("analyze")
        elif key == "mal_list":
            self.start_mal_list(force=True)
        else:
            return False
        return True

    def _step(self, key: str, title: str, what: str, fn):
        """Run one online step of the library check; a failure becomes a visible notice with Retry."""
        try:
            fn()
            self.notices.clear(key)
        except Exception as e:
            self.notices.set(key, title, f"{what} ({str(e)[:140]}). {AUTO_RETRY}", key)

    def start_mal(self):
        """Fetch MyAnimeList genres for series not asked yet, in the background (needs the user's client ID)."""
        cid = (settings().get("mal_client_id") or "").strip()
        if not cid or self._mal_lock.locked():
            return
        from . import content

        def run():
            if not self._mal_lock.acquire(blocking=False):
                return
            m = self.mal
            try:
                activity.lower_thread_priority()
                m.state, m.error, m.message = "running", None, ""
                total = 0
                while not self._stop.is_set():
                    m.set(phase="MyAnimeList genres & themes", current="")
                    st = content.mal_status()
                    base = st["done"] + st["waiting"]
                    res = content.refresh_mal(cid, lambda d, t, msg: m.set(done=base + d, total=st["total"],
                                                                         current=f"{base + d:,}/{st['total']:,} series"),
                                              should_stop=self._stop.is_set)
                    total += res["done"]
                    if not res["paused"] or content.mal_status()["error"]:
                        break
                    # the site refused connections for a while: wait (visibly) and continue by itself
                    from .net import http
                    resume = max(time.time() + 600, http().host_free_at("api.myanimelist.net"))
                    m.set(phase="MyAnimeList paused: the site is busy",
                          current="continues at " + time.strftime("%H:%M", time.localtime(resume)))
                    self._mal_wake.clear()
                    while time.time() < resume and not self._stop.is_set() and not self._mal_wake.is_set():
                        time.sleep(1)
                err = content.mal_status()["error"]
                m.state, m.phase = ("error", "MyAnimeList") if err else ("idle", "")
                m.error = err or None
                m.message = err or (f"MyAnimeList: {total} series updated" if total else "")
            except Exception as e:
                log.exception("MyAnimeList pass failed")
                m.state, m.error, m.message = "error", str(e), f"MyAnimeList: {e}"
            finally:
                self._mal_lock.release()
        threading.Thread(target=run, name="mal", daemon=True).start()

    def submit(self, kind: str, **kw):
        names = [k for k, _ in list(self._q.queue)]
        if kind in names:
            return False
        self._q.put((kind, kw))
        return True

    def wake_thumbs(self):
        self._thumb_wake.set()

    def start_copy(self, ids: list[int], dest: str) -> dict:
        """Copy videos to a folder in the background (see copier.py: never overwrites, never leaves half files)."""
        from . import copier
        if self.copy.state == "running":
            raise ValueError("A copy is already running; wait for it to finish.")
        plan = copier.plan(ids, dest)
        if not plan["count"]:
            raise ValueError("Nothing to copy (the files are missing).")
        if not plan["enough_space"]:
            raise ValueError(f"Not enough free space in {plan['dest']}: {plan['bytes'] / 1e9:.1f} GB needed, "
                             f"{plan['free'] / 1e9:.1f} GB free.")
        c = self.copy
        c.state, c.error, c.message = "running", None, ""

        def run():
            try:
                copier.clean_partials(plan["dest"])
                res = copier.copy_files(plan, c, self._stop)
                msg = f"Copied {res['copied']} of {plan['count']} videos to {plan['dest']}"
                if res["failed"]:
                    msg += f" · {len(res['failed'])} failed (see the log)"
                c.state, c.phase, c.current, c.message = ("error" if res["failed"] and not res["copied"] else "idle"), "", "", msg
                c.error = "; ".join(res["failed"][:3]) or None
                log.info(msg)
            except Exception as e:
                log.exception("copy failed")
                c.state, c.error, c.message = "error", str(e), f"Copy failed: {e}"
        threading.Thread(target=run, name="copy", daemon=True).start()
        return {k: plan[k] for k in ("dest", "count", "bytes", "renamed", "missing")}

    def stop(self, wait_s: float = 1.5):
        """Stop background work; give the thumbnail worker a moment to save what it finished."""
        self._stop.set()
        self._thumb_wake.set()
        self.engine.stop()                     # signal first, so both save at the same time
        if self.thumbs.state == "running":
            self._thumbs_saved.wait(wait_s)
        self.engine.wait_stopped()

    def _auto_rescan_loop(self):
        """Pick up new folders/files (e.g. a new year) while the app stays open. Unchanged files cost
        only a directory listing, so this is cheap; it never runs while other work is in progress."""
        while not self._stop.wait(60):
            try:
                minutes = int(settings().get("auto_rescan_minutes") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes <= 0 or not settings().get("archive_root"):
                continue
            last = (self.last_scan or {}).get("at", self._started)
            if time.time() - last >= minutes * 60 and self.idle():
                log.info("periodic rescan (every %s min)", minutes)
                self.submit("scan")
                self.last_scan = {**(self.last_scan or {}), "at": time.time()}

    # ---------------------------------------------------------- pipeline
    def _pipeline_loop(self):
        activity.lower_thread_priority()
        while not self._stop.is_set():
            try:
                kind, kw = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            p = self.pipeline
            p.state, p.error, p.done, p.total, p.current = "running", None, 0, 0, ""
            try:
                with self._busy:
                    getattr(self, f"_job_{kind}")(**kw)
                p.state, p.phase, p.message = "idle", "", "Up to date"
            except Exception as e:
                log.exception("job %s failed", kind)
                p.state, p.error = "error", str(e)
                p.message = f"{kind} failed: {e}"
            finally:
                db.bump_library_version()
                self.wake_thumbs()
                self._release_memory()

    def _release_memory(self):
        """Big jobs build large temporary structures: drop caches and give memory back."""
        matcher.invalidate_index()
        db.release_memory()
        gc.collect()

    def _ensure_catalog(self, force=False):
        p = self.pipeline
        age = catalog.catalog_age_days()
        max_age = float(settings().get("catalog_max_age_days") or 14)
        if not force and age is not None and age < max_age and catalog.catalog_size() > 0:
            return
        p.set(phase="Syncing AnimeThemes catalogue", done=0, total=0, current="")
        try:
            n = catalog.sync(lambda d, t, m: p.set(done=d, total=t, current=m))
            matcher.invalidate_index()
            log.info("catalogue synced: %s anime", n)
            self.notices.clear("catalog")
        except (HostDown, Exception) as e:
            if catalog.catalog_size() > 0:
                log.warning("catalogue sync failed (%s); using cached copy", e)
                p.message = "AnimeThemes unreachable - using cached catalogue"
                self.notices.set("catalog", "AnimeThemes catalogue", "AnimeThemes didn't answer, so its newest "
                                 f"entries are missing (the saved copy is used meanwhile). {AUTO_RETRY}", "catalog")
            else:
                log.warning("catalogue sync failed and no cached copy: %s", e)
                p.message = "AnimeThemes unreachable - files will be matched by search where possible"
                self.notices.set("catalog", "AnimeThemes catalogue", "AnimeThemes didn't answer, so videos can only "
                                 f"be identified by search for now. {AUTO_RETRY}", "catalog")

    def _job_scan(self, rematch_changed=True):
        p = self.pipeline
        root = settings().get("archive_root")
        if not root:
            raise RuntimeError("No archive folder configured")
        p.set(phase="Scanning archive", done=0, total=0, current=root)
        stats = scanner.scan(root, lambda d, t, m: p.set(done=d, total=t, current=m),
                             workers=int(settings().get("workers") or 4), stop_event=self._stop)
        touched = stats.pop("touched", [])
        self.last_scan = {**stats, "at": time.time()}
        db.bump_library_version()
        self.wake_thumbs()  # thumbnails don't need identification: start them right away
        # The catalogue copy is only needed to identify new files: with nothing new, stay offline.
        if touched or catalog.catalog_size() == 0 or db.q1("SELECT 1 FROM video WHERE status='pending' AND missing=0 LIMIT 1"):
            self._ensure_catalog()
        self._match(None)
        if touched:
            search.reindex(touched)
        self._covers()
        self._refresh_catalog_songs()
        if settings().get("use_anilist"):
            self._step("titles", "AniList titles", "English / Japanese titles of some series are still missing",
                       self._job_fill_from_anilist)
            self._step("genres", "AniList genres & tags", "Genres and tags of some series are still missing",
                       self._job_fill_genres)
            self._step("content", "Content ratings", "Ecchi / 18+ ratings of some series are still missing",
                       self._job_fill_content)
        self.start_mal()
        self.start_mal_list()                  # the user's list, when it is a few hours old
        self.request_recs("analyze")
        if settings().get("use_anilist"):
            self._step("relations", "Related series (AniList)", "Sequels / prequels of some series are still missing",
                       self._job_fill_relations)

    def _job_sync_catalog(self):
        self._ensure_catalog(force=True)
        # re-match anything that was not an exact AnimeThemes match
        with db.tx() as c:
            c.execute("""UPDATE video SET status='pending' WHERE missing=0 AND COALESCE(match_source,'')<>'animethemes'
                         AND id NOT IN (SELECT video_id FROM user_override WHERE anime_id IS NOT NULL)""")
        self._match(None)
        self._covers()

    def _job_rematch(self, ids=None, all=False):
        if all:
            with db.tx() as c:
                c.execute("UPDATE video SET status='pending' WHERE missing=0 "
                          "AND id NOT IN (SELECT video_id FROM user_override WHERE anime_id IS NOT NULL)")
            self._match(None)
        else:
            self._match(ids)
        self._covers()

    def _job_enrich_mal(self):
        """Optional: fetch official MyAnimeList titles via Jikan for series in the library."""
        from .sources import jikan
        p = self.pipeline
        rows = db.q("SELECT id, mal_id FROM anime WHERE mal_id IS NOT NULL AND title_mal IS NULL")
        p.set(phase="Fetching MyAnimeList titles (Jikan)", done=0, total=len(rows))
        for i, r in enumerate(rows, 1):
            if self._stop.is_set():
                break
            try:
                a = jikan.get_anime(r["mal_id"])
            except HostDown:
                p.message = "Jikan (api.jikan.moe) is unreachable - skipped"
                log.warning("Jikan unreachable; stopping MAL enrichment")
                break
            except Exception as e:
                log.info("jikan %s: %s", r["mal_id"], e)
                continue
            if a:
                cand = jikan.to_candidate(a)
                # MAL theme credits fill artists AnimeThemes doesn't have
                credits = {}
                if db.q1("SELECT 1 FROM theme WHERE anime_id=? AND COALESCE(artists,'')=''", (r["id"],)):
                    try:
                        credits = jikan.theme_credits(r["mal_id"])
                    except Exception as e:
                        log.info("jikan themes %s: %s", r["mal_id"], e)
                with db.tx() as c:
                    c.execute("UPDATE anime SET title_mal=?, title_english=COALESCE(title_english,?), "
                              "title_japanese=COALESCE(title_japanese,?) WHERE id=?",
                              (cand["title_romaji"], cand["title_english"], cand["title_japanese"], r["id"]))
                    for (typ, num), (song, artist) in credits.items():
                        c.execute("UPDATE theme SET artists=?, song_title=COALESCE(song_title,?) WHERE anime_id=? "
                                  "AND type=? AND number=? AND COALESCE(artists,'')=''",
                                  (artist, song, r["id"], typ, num))
                search.reindex_anime(r["id"])
            p.set(done=i, current=str(r["mal_id"]))

    def _job_fill_from_anilist(self):
        """Fill missing English/Japanese titles from AniList in batches of 50 MAL ids."""
        from .sources import anilist
        p = self.pipeline
        rows = db.q("SELECT id, mal_id FROM anime WHERE mal_id IS NOT NULL AND titles_at=0 AND "
                    "(title_english IS NULL OR title_japanese IS NULL)")
        if not rows:
            return
        p.set(phase="Filling titles from AniList", done=0, total=len(rows))
        by_mal = {r["mal_id"]: r["id"] for r in rows}
        ids = sorted(by_mal)
        changed, failed = [], None
        for i in range(0, len(ids), 50):
            if self._stop.is_set():
                break
            try:
                media_list = anilist.by_mal_ids(ids[i:i + 50])
            except Exception as e:
                log.warning("AniList batch failed: %s", e)
                failed = e
                break
            with db.tx() as c:
                now = time.time()
                for mid in ids[i:i + 50]:               # asked: don't ask again at the next start
                    c.execute("UPDATE anime SET titles_at=? WHERE mal_id=?", (now, mid))   # every series with that id
                for m in media_list:
                    cand = anilist.to_candidate(m)
                    aid = by_mal.get(cand["mal_id"])
                    if aid and (cand["title_english"] or cand["title_japanese"]):
                        c.execute("UPDATE anime SET title_english=COALESCE(title_english,?), "
                                  "title_japanese=COALESCE(title_japanese,?), anilist_id=COALESCE(anilist_id,?) "
                                  "WHERE id=?", (cand["title_english"], cand["title_japanese"],
                                                 cand["anilist_id"], aid))
                        changed.append(aid)
            p.set(done=min(len(ids), i + 50))
        for aid in set(changed):
            search.reindex_anime(aid)
        if failed is not None:
            raise RuntimeError(f"AniList didn't answer: {failed}")

    def _job_fill_content(self, all=False):
        """Ecchi / 18+ ratings from AniList, Kitsu and AnimeThemes (once per series; 'all' refreshes)."""
        from . import content
        p = self.pipeline
        content.refresh(lambda d, t, m: p.set(done=d, total=t, current=m), only_missing=not all,
                        should_stop=self._stop.is_set,
                        on_start=lambda n: p.set(phase="Content ratings (AniList, Kitsu, AnimeThemes)",
                                                 done=0, total=n, current=""))

    def _job_fill_relations(self, all=False):
        """Sequels / prequels / side stories from AniList, once per series (saved every 50 series). A new season
        added later is fetched when it arrives and links both ways, so no periodic refresh is needed."""
        import json as _json
        from .sources import anilist
        p = self.pipeline
        stale = time.time() if all else 0               # automatic runs: only series never fetched
        rows = db.q("SELECT id, anilist_id, mal_id FROM anime WHERE relations_at<=? AND "
                    "(anilist_id IS NOT NULL OR mal_id IS NOT NULL) AND id IN (SELECT anime_id FROM v_video)", (stale,))
        if not rows:
            return
        p.set(phase="Related series (AniList)", done=0, total=len(rows), current="")
        by_al, by_mal = {}, {}
        for r in rows:
            if r["anilist_id"]:
                by_al.setdefault(r["anilist_id"], []).append(r["id"])
            elif r["mal_id"]:
                by_mal.setdefault(r["mal_id"], []).append(r["id"])
        done = 0
        try:
            for key, mapping in (("id", by_al), ("idMal", by_mal)):
                ids = sorted(mapping)
                for i in range(0, len(ids), 50):
                    if self._stop.is_set():
                        return
                    chunk = ids[i:i + 50]
                    try:
                        media_list = anilist.relations_batch(chunk, by=key)
                    except Exception as e:
                        log.warning("AniList relations batch failed (%s); the rest is fetched on the next scan", e)
                        raise RuntimeError(f"AniList didn't answer: {e}") from e
                    now = time.time()
                    found = set()
                    with db.tx() as c:                  # saved per batch: a quit loses at most one request
                        for m in media_list:
                            ext = m.get("id") if key == "id" else m.get("idMal")
                            for aid in mapping.get(ext, []):
                                found.add(ext)
                                c.execute("UPDATE anime SET relations=?, relations_at=? WHERE id=?",
                                          (_json.dumps(anilist.clean_relations(m), ensure_ascii=False), now, aid))
                        for ext in set(chunk) - found:  # unknown to AniList: don't ask again every scan
                            for aid in mapping[ext]:
                                c.execute("UPDATE anime SET relations_at=? WHERE id=?", (now, aid))
                    done += sum(len(mapping[x]) for x in chunk)
                    p.set(done=done, current=f"{done}/{len(rows)} series")
        finally:
            if done:
                db.bump_library_version()

    def _job_fill_genres(self):
        """Genres + tags from AniList for every series in the library, once. genres_at > 0 = complete (never asked
        again); < 0 = no genres/tags yet or unknown to AniList (asked again after 30 days); 0 = not asked yet."""
        import json as _json
        from .sources import anilist
        p = self.pipeline
        retry = -(time.time() - 30 * 86400)
        rows = db.q("SELECT id, anilist_id, mal_id FROM anime WHERE (genres_at=0 OR (genres_at<0 AND genres_at>?)) AND "
                    "(anilist_id IS NOT NULL OR mal_id IS NOT NULL) AND id IN (SELECT anime_id FROM v_video)", (retry,))
        if not rows:
            return
        p.set(phase="Fetching genres & tags (AniList)", done=0, total=len(rows), current="")
        by_al = {r["anilist_id"]: r["id"] for r in rows if r["anilist_id"]}
        by_mal = {r["mal_id"]: r["id"] for r in rows if not r["anilist_id"] and r["mal_id"]}
        done, changed = 0, []
        for key, mapping in (("id", by_al), ("idMal", by_mal)):
            ids = sorted(mapping)
            for i in range(0, len(ids), 50):
                if self._stop.is_set():
                    return
                chunk = ids[i:i + 50]
                try:
                    media_list = anilist.genres_batch(chunk, by=key)
                except Exception as e:
                    log.warning("AniList genres batch failed (%s); will retry next scan", e)
                    raise RuntimeError(f"AniList didn't answer: {e}") from e
                now = time.time()
                found = set()
                with db.tx() as c:
                    for m in media_list:
                        ext = m.get("id") if key == "id" else m.get("idMal")
                        aid = mapping.get(ext)
                        if not aid:
                            continue
                        found.add(ext)
                        g, t = m.get("genres") or [], anilist.clean_tags(m)
                        c.execute("UPDATE anime SET genres=?, tags=?, genres_at=? WHERE id=?",
                                  (_json.dumps(g), _json.dumps(t), now if (g or t) else -now, aid))
                        changed.append(aid)
                    for ext in set(chunk) - found:  # unknown to AniList: ask again in 30 days, not every scan
                        c.execute("UPDATE anime SET genres_at=? WHERE id=?", (-now, mapping[ext]))
                done += len(chunk)
                p.set(done=done, current=f"{done}/{len(rows)} series")
        if changed:
            p.set(phase="Updating search index", done=0, total=0, current="")
            ids = [r["id"] for r in db.q(f"SELECT id FROM video WHERE anime_id IN ({','.join('?' * len(changed))})", changed)] \
                if len(changed) < 900 else None
            search.reindex(ids)
        db.bump_library_version()

    # ---------------------------------------------------------- steps
    def _match(self, ids):
        p = self.pipeline
        try:                                   # videos identified once for everyone (shipped list): no review needed
            from . import known
            known.apply()
        except Exception as e:
            log.warning("shipped identifications skipped: %s", e)
        def covers_in_background():
            db.bump_library_version()
            threading.Thread(target=self._covers, kwargs={"quiet": True}, name="covers", daemon=True).start()

        def progress(d, t, m):              # a progress bar only when there are videos to identify
            if t:
                p.set(phase="Identifying videos", done=d, total=t, current=m)

        stats = matcher.match_pending(progress, only_ids=ids,
                                      stop_event=self._stop, after_offline=covers_in_background)
        log.info("matching: %s", stats)
        db.bump_library_version()
        if stats.get("deferred"):
            self.notices.set("identify", "Identifying new videos", f"{stats['deferred']} new video(s) couldn't be "
                             f"identified yet because AniList / AnimeThemes didn't answer. {AUTO_RETRY}", "identify")
        elif ids is None:
            self.notices.clear("identify")

    def _covers(self, quiet=False):
        with self._cover_lock:
            self._covers_locked(quiet)

    def _covers_locked(self, quiet):
        p = self.pipeline if not quiet else Progress("covers")
        rows = db.q("SELECT id, cover_url FROM anime WHERE cover_url IS NOT NULL AND "
                    "(cover_path IS NULL OR cover_path='') AND id IN (SELECT anime_id FROM v_video)")
        if not rows:
            return
        p.set(phase="Downloading cover art", done=0, total=len(rows), current="")
        done, failed = 0, 0
        with ThreadPoolExecutor(max_workers=4, initializer=activity.lower_thread_priority) as ex:
            futs = {ex.submit(media.cache_cover, r["id"], r["cover_url"]): r["id"] for r in rows}
            for fut in as_completed(futs):
                aid = futs[fut]
                try:
                    path = fut.result()
                except Exception as e:
                    log.info("cover %s failed: %s", aid, e)
                    path = None
                if not path:
                    failed += 1
                if path:
                    with db.tx() as c:
                        c.execute("UPDATE anime SET cover_path=?, cover_at=? WHERE id=?",
                                  (path, time.time(), aid))
                done += 1
                if done % 20 == 0 or done == len(rows):
                    p.set(done=done, current=f"cover {done}/{len(rows)}")
                if done % 40 == 0:
                    db.bump("media")
        db.bump("media")
        if failed:
            self.notices.set("covers", "Cover art", f"{failed} cover image(s) couldn't be downloaded. {AUTO_RETRY}",
                             "covers")
        else:
            self.notices.clear("covers")

    def _refresh_catalog_songs(self):
        """Fill song/artist on themes created before the catalogue had them."""
        with db.tx() as c:
            c.execute("""UPDATE theme SET song_title=(SELECT cv.song_title FROM cat_video cv JOIN anime a ON a.at_id=cv.at_anime_id
                         WHERE a.id=theme.anime_id AND cv.type=theme.type AND cv.number=theme.number LIMIT 1),
                         artists=COALESCE(artists,(SELECT cv.artists FROM cat_video cv JOIN anime a ON a.at_id=cv.at_anime_id
                         WHERE a.id=theme.anime_id AND cv.type=theme.type AND cv.number=theme.number LIMIT 1))
                         WHERE song_title IS NULL""")

    # ---------------------------------------------------------- thumbnails
    def _thumb_loop(self):
        activity.lower_thread_priority()
        try:
            n = media.clean_work_files()       # leftovers of a crash / hard quit mid-thumbnail
            if n:
                log.info("removed %d unfinished thumbnail files from an earlier run", n)
        except Exception:
            log.exception("thumbnail cleanup failed")
        while not self._stop.is_set():
            self._thumb_wake.wait(timeout=30)
            self._thumb_wake.clear()
            if self._stop.is_set():
                return
            try:
                self._thumb_batch()
                self.notices.clear("thumbs")
            except Exception as e:
                log.exception("thumbnail worker crashed")
                self.notices.set("thumbs", "Thumbnails", f"Making thumbnails stopped with an error ({str(e)[:120]}). "
                                 "It starts again by itself in 30 seconds, or press Retry.", "thumbs")
                time.sleep(5)

    def _thumb_batch(self):
        t = self.thumbs
        if not media.tools_status()["ok"]:
            t.state, t.message = "error", media.tools_status()["help"]
            return
        algo = media.THUMB_ALGO
        from . import recommend
        self._engine_thumbs_at = recommend.import_engine_thumbs(getattr(self, "_engine_thumbs_at", 0.0))
        # new videos first, then upgrading thumbnails made by an older frame-selection method
        rows = db.q("SELECT id, path, duration, probe_state, thumb_state, thumb_algo FROM video WHERE missing=0 AND "
                    "(probe_state=0 OR thumb_state=0 OR (thumb_state=1 AND thumb_algo<?)) "
                    "ORDER BY thumb_state<>0, folder_year DESC, path", (algo,))   # folder order: short disk seeks
        if not rows:
            t.state, t.phase, t.message = "idle", "", "All thumbnails ready"
            return
        fresh = sum(1 for r in rows if r["thumb_state"] == 0)
        t.state, t.done, t.total, t.error = "running", 0, len(rows), None
        t.phase = "Generating thumbnails" if fresh else "Improving thumbnails"
        workers = max(1, int(settings().get("workers") or 4))
        if not fresh:
            # upgrading existing thumbnails is not urgent: fewer parallel readers on the archive disk
            # (measured on a hard disk: 3 readers ~ 0.35 s/video, more only adds seeking)
            workers = min(workers, 3)
        done = 0
        pending_updates = []

        waiting, wait_lock = [0], threading.Lock()   # workers paused for For You (keeps the "paused" label right)

        def work(r):
            # step aside while the user is clicking around or playing/previewing a video
            activity.yield_to_user(self._stop)
            # upgrading old thumbnails is cosmetic: let the For You analysis have the archive disk first
            if r["thumb_state"] == 1 and self.recs.state == "running":
                with wait_lock:
                    waiting[0] += 1
                    t.set(phase="Thumbnails: made by For You",
                          current="the analysis makes them from the frames it reads anyway")
                while self.recs.state == "running" and not self._stop.is_set():
                    time.sleep(2)
                with wait_lock:
                    waiting[0] -= 1
                    if not waiting[0]:
                        t.set(phase="Improving thumbnails", current="")
            if self._stop.is_set():
                raise RuntimeError("stopping")   # never record a result for work that didn't run
            if r["thumb_state"] == 1 and r["probe_state"] != 0:
                cur = db.q1("SELECT thumb_algo FROM video WHERE id=?", (r["id"],))
                if cur and cur["thumb_algo"] >= algo:
                    return r, None, "done"       # the For You engine made it from the frames it read anyway
            info = None
            dur = r["duration"]
            if r["probe_state"] == 0:
                info = media.probe(r["path"])
                dur = (info or {}).get("duration") or dur
            thumb = None
            if r["thumb_state"] == 0 or (r["thumb_state"] == 1 and r["thumb_algo"] < algo):
                thumb = media.make_thumbnail(r["id"], r["path"], dur)
            return r, info, thumb

        def flush():
            if not pending_updates:
                return
            now = time.time()
            with db.tx() as c:
                for r, info, thumb in pending_updates:
                    if thumb == "done":
                        continue
                    if r["probe_state"] == 0:
                        if info:
                            c.execute("UPDATE video SET duration=?, width=?, height=?, probe_state=1, media_at=? "
                                      "WHERE id=?", (info["duration"], info["width"], info["height"], now, r["id"]))
                        else:
                            c.execute("UPDATE video SET probe_state=-1 WHERE id=?", (r["id"],))
                    if r["thumb_state"] == 0:
                        c.execute("UPDATE video SET thumb_path=?, thumb_state=?, thumb_algo=?, media_at=? WHERE id=?",
                                  (thumb, 1 if thumb else -1, algo, now, r["id"]))
                    elif r["thumb_state"] == 1 and r["thumb_algo"] < algo:
                        # upgrade: on failure keep the old thumbnail, but don't retry it forever
                        c.execute("UPDATE video SET thumb_algo=?, media_at=? WHERE id=?",
                                  (algo, now, r["id"]))
            pending_updates.clear()
            db.bump("media")

        ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="thumb",
                                initializer=activity.lower_thread_priority)
        try:
            it = iter(rows)
            futs = set()
            for r in it:
                futs.add(ex.submit(work, r))
                if len(futs) >= workers * 2:
                    break
            last_flush = last_import = time.time()
            while futs and not self._stop.is_set():
                finished, futs = wait(futs, timeout=0.5, return_when=FIRST_COMPLETED)
                if time.time() - last_import > 5:
                    last_import = time.time()
                    self._engine_thumbs_at = recommend.import_engine_thumbs(self._engine_thumbs_at)
                    if waiting[0]:
                        made = getattr(recommend.import_engine_thumbs, "count", 0)
                        t.set(current=f"{made:,} upgraded so far by the For You analysis (same disk read)")
                for fin in finished:
                    current = ""
                    try:
                        res = fin.result()
                        pending_updates.append(res)
                        current = str(res[0]["path"]).rsplit("\\", 1)[-1]
                        if res[0]["thumb_state"] != 0 and res[0]["probe_state"] != 0 and not waiting[0]:
                            t.phase = "Improving thumbnails"
                        if res[2] is None and res[0]["thumb_state"] == 0:
                            log.warning("no thumbnail for %s (unreadable or corrupt?)", res[0]["path"])
                            self._unreadable = getattr(self, "_unreadable", 0) + 1
                            self.notices.set("thumbs_bad", "Thumbnails", f"{self._unreadable} video(s) couldn't be "
                                             "read to make a thumbnail (the file may be damaged); they still play "
                                             "if your player can open them. Details are in the log.")
                    except Exception:
                        if not self._stop.is_set():       # work cancelled because Kanade is closing: not an error
                            log.exception("thumbnail job failed")
                    done += 1
                    t.set(done=done, current=None if waiting[0] else current)
                    nxt = next(it, None)
                    if nxt is not None:
                        futs.add(ex.submit(work, nxt))
                # progress is saved within ~1 s, so a crash or quit loses at most a second of work
                if pending_updates and (len(pending_updates) >= 20 or time.time() - last_flush > 1.0):
                    flush()
                    last_flush = time.time()
        finally:
            flush()
            # on quit: don't wait for in-flight FFmpeg runs (their results are simply redone next time)
            ex.shutdown(wait=not self._stop.is_set(), cancel_futures=True)
        if self._stop.is_set():
            self._thumbs_saved.set()
            return
        del rows
        db.release_memory()
        gc.collect()
        t.state, t.phase, t.message = "idle", "", "All thumbnails ready"


_jm: JobManager | None = None


def manager() -> JobManager:
    global _jm
    if _jm is None:
        _jm = JobManager()
    return _jm
