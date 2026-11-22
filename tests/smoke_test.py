"""End-to-end smoke test that runs without a window or network, on Windows, Linux and macOS.

    python tests/smoke_test.py

Creates a throw-away data folder and a tiny archive with a generated video (needs FFmpeg on PATH), then checks:
scanning, probing and thumbnails, the per-user data folder, the local server and its API, the single-instance
lock, and the For You engine's process helpers (engine lock, parent watch).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="kanade-smoke-"))
HOME = TMP / "home"
ARCHIVE = TMP / "archive"
os.environ["KANADE_HOME"] = str(HOME)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "kanade" / "analyzer"))
failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


def make_video(path: Path, seconds=12):
    path.parent.mkdir(parents=True, exist_ok=True)
    ff = shutil.which("ffmpeg")
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=640x360:rate=24",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libvpx-vp9", "-b:v", "300k",
                    "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus", "-shortest", str(path)], check=True)


def main():
    from kanade import config, db, media, osutil, scanner
    check("FFmpeg found", bool(media.ffmpeg() and media.ffprobe()), str(media.ffmpeg()))
    check("data folder override", config.data_dir() == HOME, str(config.data_dir()))
    check("per-user data root", osutil.user_data_root().is_absolute(), str(osutil.user_data_root()))
    video = ARCHIVE / "2015" / "Spring" / "ShingekiNoKyojin-OP1.webm"
    make_video(video)
    db.init()
    res = scanner.scan(str(ARCHIVE))
    rows = db.q("SELECT id, path, folder, folder_season, p_type, p_number FROM video")
    check("scan finds the video", len(rows) == 1, json.dumps(res, default=str)[:200])
    r = rows[0]
    check("folder and filename parsed", (r["folder"], r["folder_season"], r["p_type"], r["p_number"]) == ("2015", "Spring", "OP", 1),
          str(dict(r)))
    info = media.probe(r["path"])
    check("probe duration", bool(info) and 11 < (info["duration"] or 0) < 13, str(info))
    thumb = media.make_thumbnail(r["id"], r["path"], info and info["duration"])
    check("thumbnail written", bool(thumb) and os.path.getsize(thumb) > 1000, str(thumb))
    check("background command", osutil.background(["ffmpeg"])[-1] == "ffmpeg")

    # local server, API and single instance
    port = 8799
    env = dict(os.environ, KANADE_HOME=str(TMP / "home2"))
    app = subprocess.Popen([sys.executable, str(ROOT / "app.py"), "--headless", "--no-scan", "--port", str(port)], env=env,
                           cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        html = None
        for _ in range(60):
            try:
                html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read().decode()
                break
            except Exception:
                time.sleep(0.5)
        check("server starts", bool(html))
        tok = re.search(r'name="kanade-token" content="([^"]+)"', html or "")
        check("page has a session token", bool(tok))

        def get(path):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-Kanade-Token": tok.group(1)})
            return json.loads(urllib.request.urlopen(req, timeout=20).read())
        for path in ("/api/bootstrap", "/api/status", "/api/engine/info", "/api/videos", "/api/foryou"):
            try:
                get(path)
                check("GET " + path, True)
            except Exception as e:
                check("GET " + path, False, str(e))
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5)
            check("API refuses requests without the token", False)
        except urllib.error.HTTPError as e:
            check("API refuses requests without the token", e.code == 403, str(e.code))
        second = subprocess.run([sys.executable, str(ROOT / "app.py"), "--headless", "--no-scan", "--port", str(port + 1)],
                                env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        check("second instance refuses to start", "already running" in (second.stdout + second.stderr).lower(),
              (second.stdout + second.stderr)[-200:])
    finally:
        app.terminate()
        try:
            app.wait(timeout=20)
        except subprocess.TimeoutExpired:
            app.kill()

    # For You engine helpers (the engine itself needs PyTorch and is not run here)
    try:
        import numpy  # noqa: F401  (the engine module needs it; the app itself doesn't)
    except ImportError:
        print("SKIP engine helpers (numpy not installed in this environment)")
    else:
        engine_checks()
    shutil.rmtree(TMP, ignore_errors=True)
    print()
    print("ALL PASSED" if not failures else "FAILED: " + ", ".join(failures), flush=True)
    sys.exit(1 if failures else 0)


def engine_checks():
    import kanade_analyzer as ka
    lock = ka.single_engine_lock(TMP, wait_s=1)
    check("engine lock taken", bool(lock))
    child = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import kanade_analyzer as ka, "
                            "pathlib; print(ka.single_engine_lock(pathlib.Path(sys.argv[2]), wait_s=1) is None)",
                            str(ROOT / "kanade" / "analyzer"), str(TMP)], capture_output=True, text=True, timeout=60)
    check("second engine is refused", child.stdout.strip() == "True", (child.stdout + child.stderr)[-200:])
    # a child watching its parent (this process), the way the engine watches the app
    watch = subprocess.run([sys.executable, "-c", "import os, sys; sys.path.insert(0, sys.argv[1]); import kanade_analyzer as ka; "
                            "h = ka._open_process(os.getppid()); print(h is not None and not ka._process_exited(h))",
                            str(ROOT / "kanade" / "analyzer")], capture_output=True, text=True, timeout=60)
    check("engine sees the app running", watch.stdout.strip() == "True", (watch.stdout + watch.stderr)[-200:])
    lock.close()


if __name__ == "__main__":
    main()
