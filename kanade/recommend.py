"""'For You' recommendations: engine setup, analysis process supervision and results.

The heavy lifting (PyTorch, CLAP, SigLIP 2) runs in a separate Python environment inside the data folder
(<data>/engine/venv), started as a low-priority child process. The app itself only reads <data>/features.db.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import media
from .config import data_dir, resource_dir, settings

log = logging.getLogger("kanade.recommend")

NO_WINDOW = (0x08000000 | 0x00004000) if sys.platform == "win32" else 0   # no console, below-normal priority
ENGINE_VERSION = 2
PACKAGES = ["transformers>=4.50,<5", "numpy", "pillow", "safetensors", "huggingface_hub", "sentencepiece", "protobuf",
            "nnAudio"]                                  # nnAudio: needed by the MERT music model


def engine_dir() -> Path:
    d = data_dir() / "engine"
    d.mkdir(parents=True, exist_ok=True)
    return d


def engine_python() -> Path:
    return engine_dir() / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def engine_ready() -> bool:
    try:
        info = json.loads((engine_dir() / "ready.json").read_text(encoding="utf-8"))
        return info.get("version") == ENGINE_VERSION and engine_python().exists()
    except (OSError, ValueError):
        return False


def analyzer_script() -> Path:
    return resource_dir() / "analyzer" / "kanade_analyzer.py"


def engine_env() -> dict:
    env = dict(os.environ)
    env["HF_HOME"] = str(engine_dir() / "hf")
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    env["TRANSFORMERS_NO_ADVISORY_WARNINGS"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if models_downloaded():
        env["HF_HUB_OFFLINE"] = "1"            # works fully offline once the models are downloaded
        env["TRANSFORMERS_OFFLINE"] = "1"
    return env


# The AI models For You uses: (repository, what it does, download size in MB)
MODELS = [
    ("m-a-p/MERT-v1-95M", "Music: rhythm, timbre and harmony (finds songs that sound alike)", 360),
    ("laion/larger_clap_music", "Music style: genre, mood, vocals", 745),
    ("google/siglip2-base-patch16-224", "Look of the video: art style, colours, era", 1430),
    ("MIT/ast-finetuned-audioset-10-10-0.4593", "Song type: calm ... intense", 330),
]
MODEL_REPOS = tuple(m[0] for m in MODELS)
TORCH_MB = {True: 2600, False: 250}       # PyTorch download with / without NVIDIA GPU support
LIBS_MB = 300


def model_downloaded(repo: str) -> bool:
    hub = engine_dir() / "hf" / "hub" / ("models--" + repo.replace("/", "--")) / "snapshots"
    return any(hub.glob("*/config.json"))


def models_downloaded() -> bool:
    return all(model_downloaded(r) for r in MODEL_REPOS)


def _hub_mb() -> float:
    """Megabytes in the model cache, including files still being downloaded."""
    total = 0
    for root, _, files in os.walk(engine_dir() / "hf" / "hub"):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1e6


def has_taste() -> bool:
    """Something to learn from: liked folders, or favourites / likes (when they count)."""
    if taste_folders():
        return True
    from . import db
    if settings().get("taste_use_favorites", True) and db.q1("SELECT 1 FROM favorite LIMIT 1"):
        return True
    return bool(db.q1("SELECT 1 FROM rating LIMIT 1"))


_gpu_cache: dict = {}


def engine_info() -> dict:
    """What For You needs and what is already on this computer (for Settings and the set-up pop-ups)."""
    try:
        ready = json.loads((engine_dir() / "ready.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ready = {}
    installed = engine_ready()
    if "gpu" not in _gpu_cache:
        _gpu_cache["gpu"] = has_nvidia_gpu()
    gpu = ready.get("gpu", _gpu_cache["gpu"]) if installed else _gpu_cache["gpu"]
    models = [{"repo": r, "purpose": what, "size_mb": mb, "downloaded": model_downloaded(r)} for r, what, mb in MODELS]
    need = sum(m["size_mb"] for m in models if not m["downloaded"])
    if not installed:
        need += TORCH_MB[bool(gpu)] + LIBS_MB
    return {"installed": installed, "gpu": bool(gpu), "models": models, "models_ready": all(m["downloaded"] for m in models),
            "download_mb": need, "python_ok": base_python() is not None}


def base_python() -> list[str] | None:
    """A Python 3.10+ to create the engine's environment from."""
    if not getattr(sys, "frozen", False):
        return [sys.executable]
    if shutil.which("py"):
        for v in ("-3.12", "-3.11", "-3.13", "-3"):
            try:
                if subprocess.run(["py", v, "-c", "import sys; assert sys.version_info >= (3, 10)"],
                                  capture_output=True, creationflags=0x08000000, timeout=30).returncode == 0:
                    return ["py", v]
            except Exception:
                continue
    for name in ("python", "python3"):
        p = shutil.which(name)
        if p and "WindowsApps" not in p:
            return [p]
    return None


def has_nvidia_gpu() -> bool:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "-L"], capture_output=True, timeout=15, creationflags=0x08000000).returncode == 0
    except Exception:
        return False


def taste_folders() -> list[str]:
    return [f for f in (settings().get("taste_folders") or []) if isinstance(f, str) and f.strip()]


# ----------------------------------------------------------------------------- results (read by the app)

_cache = {"rank_at": None, "rows": [], "mtime": 0.0}
_clock = threading.Lock()


def features_db():
    p = data_dir() / "features.db"
    if not p.exists():
        return None
    # a normal connection (only SELECTs are issued): strict read-only mode can't open a WAL database
    # while the engine process is writing to it
    return sqlite3.connect(p, timeout=10)


def engine_status() -> dict:
    """Status written by the analysis process (phase, done/total…), plus readiness info."""
    st = {}
    con = features_db()
    if con is not None:
        try:
            row = con.execute("SELECT value FROM meta WHERE key='status'").fetchone()
            st = json.loads(row[0]) if row else {}
            rk = con.execute("SELECT value FROM meta WHERE key='rank_at'").fetchone()
            tc = con.execute("SELECT value FROM meta WHERE key='taste_count'").fetchone()
            st["rank_at"] = rk[0] if rk else None
            st["taste_count"] = int(tc[0]) if tc else 0
            st["analysed"] = con.execute("SELECT COUNT(*) FROM feat WHERE kind='lib' AND audio IS NOT NULL").fetchone()[0]
        except sqlite3.Error:
            pass
        finally:
            con.close()
    st["engine_ready"] = engine_ready()
    st["folders"] = taste_folders()
    return st


def group_key(members: list) -> str:
    """A stable identity for a taste group: its three most typical liked videos."""
    return " | ".join(members[:3])


def _named(groups: list) -> list:
    """Apply the user's own group names (they survive re-ranking while the group stays recognisable)."""
    custom = settings().get("taste_group_names") or {}
    out = []
    for g in groups:
        key = group_key(g.get("members", []))
        out.append({**g, "key": key, "auto_name": g["name"], "name": custom.get(key) or g["name"]})
    return out


def recommendations() -> dict:
    """{rank_at, rows: [[video_id, audio_pct, mix_pct, why_audio, why_visual], ...]} (cached until re-ranked)."""
    con = features_db()
    if con is None:
        return {"rank_at": None, "rows": []}
    try:
        rk = con.execute("SELECT value FROM meta WHERE key='rank_at'").fetchone()
        rank_at = rk[0] if rk else None
        with _clock:
            if rank_at and rank_at == _cache["rank_at"]:
                return {"rank_at": rank_at, "rows": _cache["rows"], "groups": _named(_cache.get("groups", []))}
        cols = {r[1] for r in con.execute("PRAGMA table_info(rec)")}
        extra = (", ga, gm" if "ga" in cols else ", NULL, NULL") + (", liked" if "liked" in cols else ", 0")
        rows = [list(r) for r in con.execute(f"SELECT video_id, audio, mix, why_audio, why_visual{extra} FROM rec")]
        groups = []
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='taste_group'").fetchone():
            groups = [{"id": i, "name": n, "size": z, "members": json.loads(m or "[]")}
                      for i, n, z, m in con.execute("SELECT id, name, size, members FROM taste_group ORDER BY id")]
        with _clock:
            _cache.update(rank_at=rank_at, rows=rows, groups=groups)
        return {"rank_at": rank_at, "rows": rows, "groups": _named(groups)}
    except sqlite3.Error:
        return {"rank_at": None, "rows": []}
    finally:
        con.close()


def import_engine_thumbs(since: float = 0.0) -> float:
    """Record the thumbnails the engine made (from frames it read for the analysis anyway) in the library.
    Returns the newest timestamp seen, to continue from next time."""
    con = features_db()
    if con is None:
        return since
    try:
        rows = con.execute("SELECT video_id, path, at FROM thumb_made WHERE at>?", (since,)).fetchall()
    except sqlite3.Error:
        return since
    finally:
        con.close()
    if not rows:
        return since
    from . import db
    now, n = time.time(), 0
    with db.tx() as c:
        for vid, path, _ in rows:
            if path and os.path.isfile(path):
                n += c.execute("UPDATE video SET thumb_path=?, thumb_state=1, thumb_algo=?, media_at=? "
                               "WHERE id=? AND thumb_algo<?", (path, media.THUMB_ALGO, now, vid, media.THUMB_ALGO)).rowcount
    if n:
        db.bump("media")
    import_engine_thumbs.count = getattr(import_engine_thumbs, "count", 0) + n
    return max(r[2] for r in rows)


# ----------------------------------------------------------------------------- engine process control

class EngineRunner:
    """Installs the engine and runs analysis/ranking passes one at a time in the background."""

    def __init__(self, progress):
        self.p = progress
        self.proc: subprocess.Popen | None = None
        self._q: list[str] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._running_analysis = False
        threading.Thread(target=self._loop, name="engine", daemon=True).start()

    def request(self, what: str):
        """what: 'setup' | 'analyze' | 'rank'. 'analyze' includes ranking."""
        with self._lock:
            if what not in self._q:
                if what == "rank" and "analyze" in self._q:
                    return
                self._q.append(what)
        self._wake.set()

    def learn(self):
        """Learn from new likes / dislikes now. A running analysis does it in-process (it watches
        engine/rank.request); otherwise a short ranking run starts."""
        try:
            (engine_dir() / "rank.request").write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass
        if not (self.proc and self.proc.poll() is None and self._running_analysis):
            self.request("rank")

    def stop(self):
        """Ask a running engine to save its current batch and exit (it checks for engine/stop.flag)."""
        self._stop.set()
        self._wake.set()
        if self.proc and self.proc.poll() is None:
            try:
                (engine_dir() / "stop.flag").write_text("stop", encoding="utf-8")
            except OSError:
                pass

    def wait_stopped(self, wait_s: float = 2.5):
        """Give the engine a moment to save; end it if it is still busy (it also exits by itself once the app is gone)."""
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        try:
            proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
            except OSError:
                pass

    def _loop(self):
        while not self._stop.is_set():
            self._wake.wait(timeout=5)
            self._wake.clear()
            while not self._stop.is_set():
                with self._lock:
                    what = self._q.pop(0) if self._q else None
                if what is None:
                    break
                try:
                    if what == "setup":
                        self._setup()
                    elif what in ("analyze", "rank"):
                        self._run_engine(rank_only=(what == "rank"))
                except Exception as e:
                    log.exception("engine %s failed", what)
                    self.p.state, self.p.error, self.p.message = "error", str(e), f"For You: {e}"

    # -- install
    def _pip(self, args, phase):
        self.p.set(phase=phase, current=" ".join(a for a in args if not a.startswith("-"))[:80])
        cmd = [str(engine_python()), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", *args]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
                                env=engine_env(), text=True, encoding="utf-8", errors="replace")
        self.proc = proc
        tail = []
        for line in proc.stdout:
            line = line.strip()
            if line:
                tail = (tail + [line])[-20:]
                if line.startswith(("Collecting", "Downloading", "Installing", "Successfully")):
                    self.p.set(current=line[:90])
            if self._stop.is_set():
                proc.terminate()
                return
        if proc.wait() != 0:
            raise RuntimeError("package install failed: " + " | ".join(tail[-3:]))

    def _setup(self):
        p = self.p
        p.state, p.error, p.done, p.total = "running", None, 0, 4
        if engine_ready():                       # engine installed: only the missing models
            self._download_models()
            p.state, p.phase, p.message = "idle", "", "AI models downloaded"
            if has_taste():
                self.request("analyze")
            return
        base = base_python()
        if not base:
            raise RuntimeError("Python 3.10+ is needed to install the For You engine (python.org)")
        if not engine_python().exists():
            p.set(phase="Installing For You engine", done=0, current="creating environment")
            r = subprocess.run([*base, "-m", "venv", str(engine_dir() / "venv")], capture_output=True, text=True,
                               creationflags=0x08000000)
            if r.returncode != 0:
                raise RuntimeError("could not create the engine environment: " + r.stderr[-300:])
        p.set(done=1)
        gpu = has_nvidia_gpu()
        torch_args = ["torch", "--index-url", "https://download.pytorch.org/whl/cu124"] if gpu else \
                     ["torch", "--index-url", "https://download.pytorch.org/whl/cpu"]
        self._pip(torch_args, f"Installing PyTorch ({'GPU' if gpu else 'CPU'}, large download)")
        p.set(done=2)
        self._pip(PACKAGES, "Installing AI libraries")
        p.set(done=3)
        self._download_models()
        (engine_dir() / "ready.json").write_text(json.dumps({"version": ENGINE_VERSION, "gpu": gpu, "at": time.time()}),
                                                encoding="utf-8")
        p.set(done=4)
        p.state, p.phase, p.message = "idle", "", "For You engine installed"
        log.info("For You engine installed (gpu=%s)", gpu)
        if has_taste():
            self.request("analyze")

    def _download_models(self):
        """Download the missing models (the engine loads them once), showing real progress in MB."""
        p = self.p
        missing = [(r, mb) for r, _, mb in MODELS if not model_downloaded(r)]
        if not missing:
            return
        expected = sum(mb for _, mb in missing)
        start = _hub_mb()
        p.set(phase="Downloading AI models", done=0, total=expected, current=f"0 / {expected:,} MB")
        stop = threading.Event()

        def watch():
            while not stop.wait(1.0):
                got = int(min(expected, max(0.0, _hub_mb() - start)))
                p.set(done=got, total=expected, current=f"{got:,} / {expected:,} MB")
        threading.Thread(target=watch, daemon=True).start()
        try:
            self._run([str(engine_python()), str(analyzer_script()), "--home", str(data_dir()), "--prefetch"])
        finally:
            stop.set()
        if not models_downloaded():
            raise RuntimeError("some AI models could not be downloaded (check the internet connection)")
        p.set(done=expected, total=expected, current="")

    def _run(self, cmd):
        log_path = data_dir() / "logs" / "engine.log"
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"\n--- {time.ctime()} {' '.join(cmd[1:])}\n")
            lf.flush()
            self.proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
                                         env=engine_env())
            while self.proc.poll() is None:
                if self._stop.is_set():
                    return 1                       # stop() asked it to save and exit; wait_stopped() ends it
                time.sleep(0.5)
        if self.proc.returncode != 0:
            raise RuntimeError(f"engine exited with code {self.proc.returncode} (see logs/engine.log)")
        return 0

    # -- analysis / ranking
    def _run_engine(self, rank_only=False):
        p = self.p
        if not has_taste():
            return                                  # nothing to learn from yet
        if not engine_ready():
            p.state, p.phase, p.error = "needs-engine", "For You: AI engine not installed", None
            p.message = "For You needs its AI engine (one-time download). Open For You or Settings to install it."
            return
        if not models_downloaded():
            p.state, p.phase, p.error = "needs-models", "For You: AI models missing", None
            p.message = "For You needs to download AI models. Open For You or Settings to download them."
            return
        ff, fp = media.ffmpeg(), media.ffprobe()
        if not ff or not fp:
            raise RuntimeError("FFmpeg is needed for For You analysis")
        try:                                      # liked files -> library copies of the same openings (names only)
            from . import tastematch
            tastematch.refresh(taste_folders())
        except Exception:
            log.exception("matching liked files to the library failed")
        p.state, p.error = "running", None
        p.set(phase="Ranking" if rank_only else "Analysing videos", done=0, total=0, current="")
        cmd = [str(engine_python()), str(analyzer_script()), "--home", str(data_dir()), "--ffmpeg", ff, "--ffprobe", fp,
               "--parent-pid", str(os.getpid())]
        if rank_only:
            cmd.append("--rank-only")
        stop_poll = threading.Event()
        from . import db
        seen = {"rank_at": engine_status().get("rank_at"), "thumbs": 0.0}

        def poll():
            while not stop_poll.wait(1.0):
                st = engine_status()
                if st.get("phase"):
                    p.set(phase=f"For You: {st['phase']}", done=st.get("done", 0), total=st.get("total", 0),
                          current=st.get("current", ""))
                if st.get("rank_at") != seen["rank_at"]:          # new results (the UI shows them on its next visit)
                    seen["rank_at"] = st.get("rank_at")
                    db.bump("recs")
        t = threading.Thread(target=poll, daemon=True)
        t.start()
        self._running_analysis = not rank_only
        try:
            self._run(cmd)
        finally:
            self._running_analysis = False
            stop_poll.set()
            try:
                import_engine_thumbs(0.0)
            except Exception:
                log.exception("importing engine thumbnails failed")
        p.state, p.phase, p.message = "idle", "", "For You is up to date"
        db.bump("recs")
