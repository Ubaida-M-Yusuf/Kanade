"""Copy library videos to a folder the user chooses.

Safe by design:
  * the library (archive) is only read; copying into the library folder itself is refused;
  * an existing file is never overwritten: a name that is taken gets " (2)", " (3)"... (the user is told first);
  * each file is copied to a hidden temporary name, checked (size) and only then given its real name, so an
    interrupted copy (quit, crash, full disk, unplugged drive) never leaves a half file under a real name;
  * free space is checked before starting; the original modification time is kept.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time

from . import activity, db
from .config import settings

log = logging.getLogger("kanade.copier")
PARTIAL = ".kanade-partial"
CHUNK = 8 * 1024 * 1024


def _inside(path: str, root: str) -> bool:
    try:
        p, r = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
        return os.path.commonpath([p, r]) == r
    except ValueError:                      # different drives
        return False


def check_destination(dest: str) -> str:
    dest = os.path.abspath(dest or "")
    if not dest or not os.path.isdir(dest):
        raise ValueError("Choose an existing folder to copy to.")
    root = settings().get("archive_root")
    if root and _inside(dest, root):
        raise ValueError("Kanade never changes your library folder: choose a folder outside it.")
    return dest


def free_name(dest: str, name: str, taken: set[str]) -> str:
    """name, or 'stem (2).ext', 'stem (3).ext'... if a file with that name exists (or is planned) in dest."""
    stem, ext = os.path.splitext(name)
    cand, n = name, 1
    while cand.lower() in taken or os.path.exists(os.path.join(dest, cand)):
        n += 1
        cand = f"{stem} ({n}){ext}"
    return cand


def plan(ids: list[int], dest: str) -> dict:
    """What would be copied: files, new names for names already taken, sizes and free space."""
    dest = check_destination(dest)
    files, renamed, missing, taken = [], [], [], set()
    for vid in ids:
        r = db.q1("SELECT id, path, filename, missing FROM video WHERE id=?", (int(vid),))
        if not r or r["missing"] or not os.path.isfile(r["path"]):
            missing.append(r["filename"] if r else str(vid))
            continue
        name = free_name(dest, r["filename"], taken)
        taken.add(name.lower())
        if name != r["filename"]:
            renamed.append([r["filename"], name])
        files.append({"id": r["id"], "src": r["path"], "name": name, "size": os.path.getsize(r["path"])})
    total = sum(f["size"] for f in files)
    free = shutil.disk_usage(dest).free
    return {"dest": dest, "files": files, "count": len(files), "bytes": total, "free": free, "enough_space": total < free,
            "renamed": renamed, "missing": missing}


def _publish(tmp: str, dest: str, name: str, taken: set[str]) -> str:
    """Give the finished temporary file a real name without ever replacing an existing file."""
    for _ in range(1000):
        final = os.path.join(dest, name)
        try:
            if os.name == "nt":
                os.rename(tmp, final)                 # Windows: fails if the name exists (never overwrites)
            else:
                try:
                    os.link(tmp, final)               # POSIX: fails if the name exists (rename would overwrite)
                    os.unlink(tmp)
                except OSError as e:
                    if os.path.exists(final):
                        raise FileExistsError(final) from e
                    os.rename(tmp, final)             # file systems without hard links (FAT, exFAT)
            return name
        except FileExistsError:
            taken.add(name.lower())
            name = free_name(dest, name, taken)       # someone created it meanwhile: next free number
    raise RuntimeError("no free file name")


def copy_files(p: dict, progress, stop: threading.Event):
    """Run a plan from plan(); progress is a jobs.Progress. Returns a short summary."""
    dest, files = p["dest"], p["files"]
    total_mb = max(1, round(p["bytes"] / 1e6))
    progress.set(phase="Copying videos", done=0, total=total_mb, current="")
    done_bytes, copied, failed, taken = 0, [], [], {f["name"].lower() for f in files}
    for f in files:
        if stop.is_set():
            break
        activity.touch_media()                        # background readers step aside: the copy gets the disk
        tmp = os.path.join(dest, "." + f["name"] + PARTIAL)
        progress.set(current=f["name"])
        try:
            with open(f["src"], "rb") as src, open(tmp, "xb") as out:
                while True:
                    if stop.is_set():
                        raise InterruptedError("stopped")
                    buf = src.read(CHUNK)
                    if not buf:
                        break
                    out.write(buf)
                    done_bytes += len(buf)
                    progress.set(done=round(done_bytes / 1e6))
                out.flush()
                os.fsync(out.fileno())
            if os.path.getsize(tmp) != os.path.getsize(f["src"]):
                raise OSError("copy is incomplete (size differs)")
            shutil.copystat(f["src"], tmp)            # keep the original dates
            copied.append(_publish(tmp, dest, f["name"], taken))
        except Exception as e:
            failed.append(f"{f['name']}: {e}")
            log.warning("copy failed for %s: %s", f["src"], e)
            try:
                os.unlink(tmp)                        # never leave a partial file behind
            except OSError:
                pass
            if isinstance(e, InterruptedError):
                break
    return {"copied": len(copied), "failed": failed, "dest": dest, "stopped": stop.is_set()}


def clean_partials(dest: str):
    for n in os.listdir(dest):
        if n.endswith(PARTIAL):
            try:
                os.unlink(os.path.join(dest, n))
            except OSError:
                pass
