"""FFmpeg / ffprobe: media info, video-frame thumbnails and cover art caching.

Originals are only ever read. Thumbnails/covers live in the app's cache folder.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat

from .config import data_dir, exe_dir, resource_dir, settings

log = logging.getLogger("kanade.media")

# CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS: background FFmpeg never competes with the UI
NO_WINDOW = (0x08000000 | 0x00004000) if sys.platform == "win32" else 0


def _find_tool(name: str) -> str | None:
    configured = settings().get(f"{name}_path")
    if configured and os.path.isfile(configured):
        return configured
    exe = name + (".exe" if sys.platform == "win32" else "")
    for d in (resource_dir() / "ffmpeg", exe_dir() / "ffmpeg", exe_dir()):
        if (d / exe).is_file():
            return str(d / exe)
    return shutil.which(name)


def ffmpeg() -> str | None:
    return _find_tool("ffmpeg")


def ffprobe() -> str | None:
    return _find_tool("ffprobe")


def tools_status() -> dict:
    f, p = ffmpeg(), ffprobe()
    return {
        "ok": bool(f and p), "ffmpeg": f, "ffprobe": p,
        "help": None if (f and p) else (
            "FFmpeg was not found, so thumbnails and durations can't be generated. Install it with "
            "'winget install Gyan.FFmpeg' (then restart Kanade), or download it from https://www.gyan.dev/ffmpeg/builds/ "
            "and put ffmpeg.exe and ffprobe.exe in a folder named 'ffmpeg' next to Kanade.exe, "
            "or set their paths in Settings."),
    }


def thumbs_dir() -> Path:
    d = data_dir() / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def covers_dir() -> Path:
    d = data_dir() / "covers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def probe(path: str) -> dict | None:
    exe = ffprobe()
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "format=duration:stream=width,height,codec_name", "-of", "json", path],
            capture_output=True, timeout=60, creationflags=NO_WINDOW)
        if out.returncode != 0:
            log.warning("ffprobe failed for %s: %s", path, out.stderr.decode(errors="ignore")[:300])
            return None
        d = json.loads(out.stdout or b"{}")
        st = (d.get("streams") or [{}])[0]
        dur = float((d.get("format") or {}).get("duration") or 0) or None
        return {"duration": dur, "width": st.get("width"), "height": st.get("height"), "codec": st.get("codec_name")}
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        log.warning("ffprobe error for %s: %s", path, e)
        return None


def _grab(path: str, t: float, width: int) -> Image.Image | None:
    """One keyframe at/before t, scaled to `width` (only keyframes are decoded: fast)."""
    exe = ffmpeg()
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error", "-threads", "1", "-skip_frame", "nokey",
             "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
             "-vf", f"scale={width}:-2:flags=lanczos", "-f", "image2pipe", "-vcodec", "ppm", "-"],
            capture_output=True, timeout=60, creationflags=NO_WINDOW)
        if out.returncode != 0 or not out.stdout:
            return None
        with Image.open(io.BytesIO(out.stdout)) as img:
            return img.convert("RGB")
    except (subprocess.TimeoutExpired, OSError, Image.UnidentifiedImageError):
        return None


def _brightness(img: Image.Image) -> tuple[float, float]:
    st = ImageStat.Stat(img.convert("L"))
    return st.mean[0], st.stddev[0]


# Bump when the frame-selection method changes: existing thumbnails are then upgraded in the background.
THUMB_ALGO = 2
CANDIDATES = (0.16, 0.28, 0.40, 0.52, 0.64, 0.76)


def frame_score(img: Image.Image) -> float:
    """How good a frame looks as a thumbnail: colourful, contrasty, detailed, well exposed.

    Penalises fades/black frames, white flashes and flat title cards; vivid character shots win.
    """
    small = img.resize((160, 90), Image.BILINEAR)
    r, g, b = small.split()
    lum = small.convert("L")
    st = ImageStat.Stat(lum)
    mean, std = st.mean[0], st.stddev[0]
    # Hasler-Suesstrunk colourfulness on opponent channels (offset 128, computed in C by Pillow)
    rg = ImageStat.Stat(ImageChops.subtract(r, g, 1, 128))
    yb = ImageStat.Stat(ImageChops.subtract(ImageChops.add(r, g, 2), b, 1, 128))
    colourful = ((rg.stddev[0] ** 2 + yb.stddev[0] ** 2) ** 0.5
                 + 0.3 * ((rg.mean[0] - 128) ** 2 + (yb.mean[0] - 128) ** 2) ** 0.5)
    edges = ImageStat.Stat(lum.filter(ImageFilter.FIND_EDGES)).mean[0]
    score = colourful + min(std, 75) * 0.7 + min(edges, 40) * 0.8
    if mean < 45:
        score -= (45 - mean) * 2.2
    if mean > 215:
        score -= (mean - 215) * 3
    if std < 18:
        score -= (18 - std) * 3
    return score


def work_dir() -> Path:
    """Scratch space for frame grabs (on the app's data disk, never the archive). Emptied on start."""
    d = thumbs_dir() / ".work"
    d.mkdir(exist_ok=True)
    return d


def clean_work_files():
    """Remove leftovers of an interrupted run (half-written thumbnails, frame grabs)."""
    removed = 0
    for p in list(work_dir().glob("*")) + list(thumbs_dir().glob("*/*.tmp")):
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _grab_many(path: str, times: list[float], width: int, tag: str) -> list[Image.Image]:
    """All candidate keyframes in ONE FFmpeg run (one input per seek point, one single-frame output each).

    Same frames as separate _grab calls, but one process start and one file open instead of six; each
    input decodes with one thread, so memory stays small (~150 MB peak for 1080p).
    """
    exe = ffmpeg()
    if not exe or not times:
        return []
    d = work_dir() / f"{tag}-{os.getpid()}-{threading.get_ident()}"
    d.mkdir(exist_ok=True)
    cmd = [exe, "-hide_banner", "-loglevel", "error"]
    for t in times:
        cmd += ["-threads", "1", "-discard", "nokey", "-skip_frame", "nokey", "-ss", f"{t:.2f}", "-i", path]
    for i in range(len(times)):
        cmd += ["-map", f"{i}:v:0", "-frames:v", "1", "-vf", f"scale={width}:-2:flags=lanczos", "-y", str(d / f"{i}.ppm")]
    frames = []
    try:
        subprocess.run(cmd, capture_output=True, timeout=120, creationflags=NO_WINDOW)
        for i in range(len(times)):
            f = d / f"{i}.ppm"
            if f.is_file() and f.stat().st_size > 0:
                try:
                    with Image.open(f) as img:
                        frames.append(img.convert("RGB"))
                except (OSError, Image.UnidentifiedImageError):
                    pass
    except (subprocess.TimeoutExpired, OSError):
        pass
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return frames


def make_thumbnail(video_id: int, path: str, duration: float | None, width: int | None = None) -> str | None:
    """Score keyframes spread across the video and keep the most attractive one (WebP)."""
    width = int(width or settings().get("thumb_width") or 480)
    dur = duration or 90.0
    times = [max(0.0, dur * frac) for frac in CANDIDATES]
    frames = _grab_many(path, times, width, str(video_id))
    if not frames:  # unusual file: fall back to one seek at a time
        frames = [img for img in (_grab(path, t, width) for t in times) if img is not None]
    if not frames:  # very short file: try the very beginning
        img = _grab(path, 0.0, width)
        frames = [img] if img is not None else []
    if not frames:
        return None
    best = max(frames, key=frame_score)
    sub = thumbs_dir() / f"{video_id // 1000:03d}"
    sub.mkdir(exist_ok=True)
    dest = sub / f"{video_id}.webp"
    tmp = dest.with_suffix(".tmp")
    best.save(tmp, "WEBP", quality=82, method=4)
    tmp.replace(dest)                      # atomic: the old thumbnail stays until the new one is complete
    return str(dest)


def cache_cover(anime_id: int, url: str) -> str | None:
    """Download cover art once and store a resized WebP copy."""
    from .net import http
    if not url:
        return None
    dest = covers_dir() / f"{anime_id}.webp"
    if dest.exists():
        return str(dest)
    raw = covers_dir() / "raw" / f"{anime_id}{Path(url.split('?')[0]).suffix or '.img'}"
    if not raw.exists() and not http().download(url, raw):
        return None
    try:
        img = Image.open(raw)
        img.load()
        img = img.convert("RGB")
        if img.width > 460:
            img = img.resize((460, int(img.height * 460 / img.width)), Image.LANCZOS)
        img.save(dest, "WEBP", quality=86, method=4)
        raw.unlink(missing_ok=True)
        return str(dest)
    except Exception as e:
        # unreadable format (e.g. AVIF without plugin): keep the raw file, it can still be served
        log.info("cover convert failed for %s (%s); serving original", url, e)
        return str(raw)
