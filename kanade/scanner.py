"""Archive scanner. Strictly read-only on the archive: it only lists files and reads bytes.

Incremental rules
  * unchanged files (same path, size and mtime) are skipped
  * changed files get a new fingerprint, new media info/thumbnail and are re-matched
    (manual corrections are kept and still win)
  * new files whose fingerprint matches a record that disappeared are treated as moves:
    the record (favorites, corrections) follows the file
  * files that disappeared are flagged missing, never deleted
  * identical files at several paths are linked through duplicate_of
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db
from .parser import VIDEO_EXTS, parse_filename, parse_folder

log = logging.getLogger("kanade.scanner")

CHUNK = 64 * 1024


def fingerprint(path: str, size: int) -> str:
    """size + SHA-1 of the first and last 64 KiB: fast and stable across moves/renames."""
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(CHUNK))
        if size > CHUNK * 2:
            f.seek(-CHUNK, os.SEEK_END)
            h.update(f.read(CHUNK))
    return h.hexdigest()


def walk(root: str):
    # KANADE_SCAN_ONLY="2022,misc" limits scanning to some top-level folders (testing aid)
    only = {x.strip().lower() for x in os.environ.get("KANADE_SCAN_ONLY", "").split(",") if x.strip()}
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name.startswith((".", "$")):
                                continue
                            if only and d == root and e.name.lower() not in only:
                                continue
                            stack.append(e.path)
                        elif e.is_file() and os.path.splitext(e.name)[1].lower() in VIDEO_EXTS:
                            st = e.stat()
                            yield e.path, st.st_size, st.st_mtime
                    except OSError as ex:
                        log.warning("cannot read %s: %s", e.path, ex)
        except OSError as ex:
            log.warning("cannot list %s: %s", d, ex)


def _row_values(path: str, root: str, size: int, mtime: float, fp: str | None):
    rel = os.path.relpath(path, root)
    parts = Path(rel).parts
    fi = parse_folder(tuple(parts[:-1]))
    p = parse_filename(parts[-1])
    return {
        "path": path, "rel_path": rel, "filename": parts[-1], "folder": fi.folder or "",
        "folder_year": fi.year, "folder_season": fi.season, "size": size, "mtime": mtime, "fingerprint": fp,
        "p_prefix": p.prefix, "p_title": p.title_guess, "p_type": p.type, "p_number": p.number if p.ok else None,
        "p_part": p.part, "p_version": p.version if p.ok else None, "p_tags": "-".join(p.tags),
    }


def scan(root: str, progress=None, workers: int = 8, stop_event=None) -> dict:
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise FileNotFoundError(f"Archive folder not found: {root}")
    t0 = time.time()
    existing = {r["path"].lower(): r for r in db.q("SELECT id, path, size, mtime, fingerprint, missing FROM video")}
    seen_ids: set[int] = set()
    new_files, changed = [], []
    count = 0
    for path, size, mtime in walk(root):
        count += 1
        r = existing.get(path.lower())
        if r is not None:
            seen_ids.add(r["id"])
            if r["size"] != size or abs((r["mtime"] or 0) - mtime) > 1:
                changed.append((r, path, size, mtime))
        else:
            new_files.append((path, size, mtime))
        if progress and count % 500 == 0:
            progress(count, 0, f"Listing files... {count}")
        if stop_event is not None and stop_event.is_set():
            return {"cancelled": True}
    log.info("listed %s files in %.1fs (%s new, %s changed)", count, time.time() - t0, len(new_files), len(changed))

    # fingerprints for new + changed files (read-only, parallel)
    todo = [(p, s) for p, s, _ in new_files] + [(p, s) for _, p, s, _ in changed]
    fps: dict[str, str | None] = {}

    def fp_job(item):
        p, s = item
        try:
            return p, fingerprint(p, s)
        except OSError as e:
            log.warning("fingerprint failed for %s: %s", p, e)
            return p, None

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for i, (p, fp) in enumerate(ex.map(fp_job, todo), 1):
                fps[p] = fp
                if progress and (i % 100 == 0 or i == len(todo)):
                    progress(i, len(todo), f"Fingerprinting {os.path.basename(p)}")
                if stop_event is not None and stop_event.is_set():
                    break

    vanished = {r["id"]: r for r in existing.values() if r["id"] not in seen_ids}
    vanished_by_fp: dict[str, list] = {}
    for r in vanished.values():
        if r["fingerprint"]:
            vanished_by_fp.setdefault(r["fingerprint"], []).append(r)

    now = time.time()
    stats = {"total": count, "new": 0, "moved": 0, "changed": len(changed), "missing": 0, "restored": 0}
    touched: list[int] = []
    with db.tx() as c:
        for path, size, mtime in new_files:
            fp = fps.get(path)
            vals = _row_values(path, root, size, mtime, fp)
            moved_from = vanished_by_fp.get(fp, []).pop(0) if fp and vanished_by_fp.get(fp) else None
            if moved_from is not None:
                vanished.pop(moved_from["id"], None)
                # keep the record (and favourites/overrides): update location-dependent fields only
                c.execute("""UPDATE video SET path=:path, rel_path=:rel_path, filename=:filename, folder=:folder,
                             folder_year=:folder_year, folder_season=:folder_season, size=:size, mtime=:mtime,
                             missing=0, p_prefix=:p_prefix, p_title=:p_title, p_type=:p_type, p_number=:p_number,
                             p_part=:p_part, p_version=:p_version, p_tags=:p_tags WHERE id=:id""",
                          {**vals, "id": moved_from["id"]})
                # identical content -> the previous identification stays valid even if renamed
                stats["moved"] += 1
                touched.append(moved_from["id"])
                log.info("moved: %s -> %s", moved_from["path"], path)
                continue
            cur = c.execute("""INSERT INTO video(path,rel_path,filename,folder,folder_year,folder_season,size,mtime,
                               fingerprint,p_prefix,p_title,p_type,p_number,p_part,p_version,p_tags,date_added,status)
                               VALUES(:path,:rel_path,:filename,:folder,:folder_year,:folder_season,:size,:mtime,
                               :fingerprint,:p_prefix,:p_title,:p_type,:p_number,:p_part,:p_version,:p_tags,:now,
                               'pending')""", {**vals, "now": now})
            stats["new"] += 1
            touched.append(cur.lastrowid)
        for r, path, size, mtime in changed:
            c.execute("UPDATE video SET size=?, mtime=?, fingerprint=?, probe_state=0, thumb_state=0, "
                      "status='pending', missing=0 WHERE id=?", (size, mtime, fps.get(path), r["id"]))
            touched.append(r["id"])
        # restored files (were missing, now seen again)
        was_missing = {r["id"] for r in existing.values() if r["missing"]}
        restored = [i for i in seen_ids if i in was_missing]
        for i in restored:
            c.execute("UPDATE video SET missing=0 WHERE id=?", (i,))
        stats["restored"] = len(restored)
        for vid, r in vanished.items():
            if not r["missing"]:
                c.execute("UPDATE video SET missing=1 WHERE id=?", (vid,))
                stats["missing"] += 1
        # duplicates: same fingerprint at several present paths
        c.execute("UPDATE video SET duplicate_of=NULL")
        c.execute("""UPDATE video SET duplicate_of=(SELECT MIN(v2.id) FROM video v2 WHERE v2.fingerprint=video.fingerprint
                     AND v2.missing=0) WHERE missing=0 AND fingerprint IS NOT NULL AND id > (SELECT MIN(v2.id) FROM video v2
                     WHERE v2.fingerprint=video.fingerprint AND v2.missing=0)""")
    stats["touched"] = touched
    stats["seconds"] = round(time.time() - t0, 1)
    log.info("scan done: %s", {k: v for k, v in stats.items() if k != "touched"})
    return stats

