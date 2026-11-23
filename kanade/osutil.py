"""Operating-system specific helpers, so the rest of Kanade runs the same on Windows, Linux and macOS."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# Windows process-creation flags (0 elsewhere: other systems reject non-zero creationflags)
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
BELOW_NORMAL_PRIORITY = 0x00004000 if IS_WINDOWS else 0
# background tools: no console window, below-normal priority
BACKGROUND_FLAGS = CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY
_NICE = None if IS_WINDOWS else shutil.which("nice")


def background(cmd: list[str]) -> list[str]:
    """A command line that runs below normal priority. On Windows the priority comes from BACKGROUND_FLAGS;
    elsewhere it is run through `nice` (child processes inherit the lower priority)."""
    if _NICE and cmd and os.path.basename(cmd[0]) != "nice":
        return [_NICE, "-n", "10", *cmd]
    return list(cmd)


def user_data_root() -> Path:
    """Where per-user application data lives: %LOCALAPPDATA% (Windows), ~/Library/Application Support (macOS),
    $XDG_DATA_HOME or ~/.local/share (Linux and other Unix systems)."""
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    if IS_MAC:
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def launch_detached(cmd: list[str]):
    """Start a program (e.g. VLC) that keeps running independently of Kanade."""
    if IS_WINDOWS:
        return subprocess.Popen(cmd, close_fds=True, creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_GROUP
    return subprocess.Popen(cmd, close_fds=True, start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_path(path: str):
    """Open a file or folder with the system's default application."""
    if IS_WINDOWS:
        os.startfile(path)  # noqa: S606 (Windows only)
    elif IS_MAC:
        launch_detached(["open", path])
    else:
        launch_detached(["xdg-open", path])


def reveal_file(path: str):
    """Show a file in the system file manager, selected where the system supports that."""
    path = os.path.normpath(path)
    if IS_WINDOWS:
        subprocess.Popen(["explorer.exe", "/select,", path])
    elif IS_MAC:
        launch_detached(["open", "-R", path])
    else:
        # the freedesktop FileManager1 interface selects the file (Nautilus, Dolphin, Nemo...); else open the folder
        if shutil.which("dbus-send"):
            uri = Path(path).as_uri()
            r = subprocess.run(["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
                                "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                                f"array:string:{uri}", "string:"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return
        open_path(os.path.dirname(path))


def find_vlc() -> str | None:
    """VLC from PATH or its usual install location (None: use the default player)."""
    exe = shutil.which("vlc")
    if exe:
        return exe
    candidates = []
    if IS_WINDOWS:
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                candidates.append(os.path.join(base, "VideoLAN", "VLC", "vlc.exe"))
    elif IS_MAC:
        candidates.append("/Applications/VLC.app/Contents/MacOS/VLC")
    return next((c for c in candidates if os.path.isfile(c)), None)


def lower_thread_priority():
    """Run the calling thread below normal priority (Windows and Linux; no-op elsewhere)."""
    try:
        if IS_WINDOWS:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetThreadPriority(k.GetCurrentThread(), -1)        # THREAD_PRIORITY_BELOW_NORMAL
        elif sys.platform.startswith("linux"):
            import threading
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)   # per-thread nice value on Linux
    except Exception:
        pass


def clipboard_text() -> str:
    """Text on the system clipboard ('' if none): for the app's own Paste menu (the window has no built-in one)."""
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        CF_UNICODETEXT = 13
        for _ in range(5):                              # another program may hold the clipboard for a moment
            if user32.OpenClipboard(None):
                break
            import time
            time.sleep(0.05)
        else:
            return ""
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            p = kernel32.GlobalLock(h)
            if not p:
                return ""
            try:
                return ctypes.wstring_at(p)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()
    for cmd in (["pbpaste"], ["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "-bo"]):
        if shutil.which(cmd[0]):
            try:
                return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
            except (OSError, subprocess.SubprocessError):
                continue
    return ""


def message_box(title: str, text: str):
    """A simple notice when no window is open yet."""
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
            return
        except Exception:
            pass
    else:
        for tool, args in (("zenity", ["--info", "--title", title, "--text", text]),
                           ("kdialog", ["--title", title, "--msgbox", text]),
                           ("osascript", ["-e", f'display dialog "{text}" with title "{title}" buttons {{"OK"}}'])):
            if shutil.which(tool):
                try:
                    subprocess.run([tool, *args], timeout=600)
                    return
                except Exception:
                    break
    print(text)
