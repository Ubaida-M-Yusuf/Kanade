"""Kanade desktop app entry point: starts the local server and opens a native window (WebView2 on Windows,
WebKit on macOS, Qt or GTK WebKit on Linux).

    python app.py                     normal start
    python app.py --browser           serve only and open in the default browser (debugging)
    python app.py --headless          serve only (no window)
    python app.py --headless --exit-when-idle
                                      background scan: exits once all work is done and no window is open
    python app.py --port 8765         fixed port

Only one Kanade runs per data folder. Starting it again while it's running (for example while a
background scan runs headless) opens a window on the running instance instead of a second copy.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
import webbrowser

from kanade import APP_NAME, config, db, server
from kanade.jobs import manager

_lock_handle = None


def acquire_instance_lock() -> bool:
    """Exclusive lock on <data>/instance.lock for the lifetime of the process."""
    global _lock_handle
    path = config.data_dir() / "instance.lock"
    fh = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _lock_handle = fh
    return True


def release_instance_lock():
    """Let a new launch start right away: drop the lock and the URL file before slow cleanup."""
    global _lock_handle
    try:
        (config.data_dir() / "instance.json").unlink(missing_ok=True)
    except OSError:
        pass
    if _lock_handle is not None:
        try:
            if sys.platform == "win32":
                import msvcrt
                _lock_handle.seek(0)
                msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            _lock_handle.close()
        except OSError:
            pass
        _lock_handle = None


def running_instance_url() -> str | None:
    try:
        info = json.loads((config.data_dir() / "instance.json").read_text(encoding="utf-8"))
        url = info["url"]
        with urllib.request.urlopen(url, timeout=3) as r:
            if r.status == 200:
                return url
    except Exception:
        return None
    return None


def show_window(url: str, debug: bool = False, pick_hook=True):
    import webview

    window = webview.create_window(APP_NAME, url, width=1480, height=920, min_size=(980, 620),
                                   background_color="#0b0d13", text_select=False)
    if pick_hook:
        def pick(kind="folder"):
            if kind == "file":
                types = ("Programs (*.exe)", "All files (*.*)") if sys.platform == "win32" else ("All files (*)",)
                res = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=types)
            else:
                res = window.create_file_dialog(webview.FOLDER_DIALOG)
            if not res:
                return None
            return res[0] if isinstance(res, (list, tuple)) else res
        server.pick_folder_hook = pick
    # Windows: Edge WebView2. Elsewhere pywebview picks what is installed (Qt WebEngine or GTK WebKit on Linux,
    # WebKit on macOS)
    webview.start(gui="edgechromium" if sys.platform == "win32" else None, private_mode=False,
                  storage_path=str(config.data_dir() / "webview"), debug=debug)


def message_box(text: str):
    from kanade.osutil import message_box as box
    box(APP_NAME, text)


def main():
    ap = argparse.ArgumentParser(prog=APP_NAME)
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--headless", action="store_true", help="only run the server (no window, no browser)")
    ap.add_argument("--exit-when-idle", action="store_true",
                    help="with --headless: quit when all work is done and no window has polled for a minute")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--debug", action="store_true", help="enable WebView developer tools")
    ap.add_argument("--no-scan", action="store_true", help="don't run the incremental scan on startup")
    args = ap.parse_args()

    log = config.setup_logging(console=True)

    locked = acquire_instance_lock()
    if not locked:
        # Another Kanade holds the lock. If it is just closing (its server no longer answers),
        # wait for it to finish and start normally instead of attaching to a dying instance.
        for _ in range(40):
            if running_instance_url():
                break
            time.sleep(0.5)
            if acquire_instance_lock():
                locked = True
                break
    if not locked:
        url = running_instance_url()
        if url and not args.headless:
            log.info("Kanade is already running at %s; opening a window on it", url)
            if args.browser:
                webbrowser.open(url)
            else:
                show_window(url, args.debug, pick_hook=False)
            return
        msg = "Kanade is already running (it may be finishing a background scan)."
        if args.headless:
            print(msg, flush=True)               # scripted / background start: never wait on a dialog
        else:
            message_box(msg)
        return

    log.info("starting %s, data in %s", APP_NAME, config.data_dir())
    db.init()
    jm = manager()
    root = config.settings().get("archive_root")
    if root and not args.no_scan:
        jm.submit("scan")          # incremental: only new/changed files are processed
    jm.wake_thumbs()

    httpd, url = server.start(args.port)
    (config.data_dir() / "instance.json").write_text(json.dumps({"url": url, "pid": os.getpid()}), encoding="utf-8")

    try:
        if args.browser or args.headless:
            if args.browser:
                webbrowser.open(url)
            print(f"{APP_NAME} running at {url}  (Ctrl+C to quit)", flush=True)
            try:
                idle_since = None
                while True:
                    time.sleep(5)
                    if not args.exit_when_idle:
                        continue
                    quiet = time.time() - server.last_api_request > 60
                    if jm.idle() and quiet:
                        idle_since = idle_since or time.time()
                        if time.time() - idle_since > 20:
                            log.info("all work done and no window open; exiting")
                            break
                    else:
                        idle_since = None
            except KeyboardInterrupt:
                pass
            return
        show_window(url, args.debug)
    finally:
        # 1. let a new launch start immediately, 2. stop background work, 3. exit without waiting for
        #    in-flight FFmpeg calls (every DB write is a transaction, so a hard exit loses nothing committed)
        release_instance_lock()
        jm.stop()
        try:
            httpd.shutdown()
        except Exception:
            pass
        log.info("Kanade closed")
        logging.shutdown()
        if not (args.browser or args.headless):
            os._exit(0)


if __name__ == "__main__":
    main()
