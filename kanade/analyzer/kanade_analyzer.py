"""Kanade analysis engine: taste-based recommendations ("For You").

Runs as a separate, low-priority process with its own Python environment (PyTorch + transformers), so the
main app stays small. It never modifies any video: it only reads them.

  * Audio similarity: MERT (m-a-p/MERT-v1-95M, music model trained on rhythm/timbre/harmony) embeddings of three
    10-second windows of each video's soundtrack. In a same-song retrieval test it found the right song 15/17 times
    vs 4/17 for CLAP, whose embeddings are nearly identical for all songs.
  * Group names: CLAP (laion/larger_clap_music) audio embeddings matched against short text descriptions.
  * Visual: SigLIP 2 (google/siglip2-base-patch16-224) embedding of the video's thumbnail (library videos) or
    of a few keyframes (liked videos outside the library).
  * Pace: scene-cut rate from the video's keyframe count (no decoding needed).

Each library video is read from disk once (audio extraction); everything else comes from the OS cache or the
thumbnails on the app's own (fast) disk. Results go to <home>/features.db; the app only reads that file.

Usage:
  python kanade_analyzer.py --home DIR --ffmpeg PATH --ffprobe PATH [--rank-only] [--prefetch]
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np

AUDIO_MODEL = "laion/larger_clap_music"           # used for naming taste groups (text <-> audio)
MERT_MODEL = "m-a-p/MERT-v1-95M"                    # used for music similarity
MERT_REVISION = "12af15fef9d0ac838c3f475bfbbf26d2060dd4f5"   # pinned: the model ships its own (reviewed) code
VISUAL_MODEL = "google/siglip2-base-patch16-224"
MODEL_TAG = "mert95m+clap-music+siglip2-b16-224/v3-6frames"
VIDEO_EXTS = {".webm", ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".mpg", ".mpeg", ".ts"}
SR = 48000
WIN_S = 10.0
# Video frames: the same six positions the app uses to pick a thumbnail. They are read right after the audio
# (from the OS cache, no extra disk work), give the visual embedding (average of six frames, the same for liked
# files and library videos) and, for videos whose thumbnail is due an upgrade, the new thumbnail.
FRAME_POS = (0.16, 0.28, 0.40, 0.52, 0.64, 0.76)      # = media.CANDIDATES in the app
THUMB_ALGO = 2                                         # = media.THUMB_ALGO in the app
FRAME_WORKERS = 5
NO_WINDOW = (0x08000000 | 0x00004000) if sys.platform == "win32" else 0   # no console, below-normal priority

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS feat(
    kind   TEXT NOT NULL,          -- 'lib' (library video, key = video id) | 'taste' (liked file, key = path)
    key    TEXT NOT NULL,
    fp     TEXT,                   -- size:mtime of the source, to detect changes
    model  TEXT,
    audio  BLOB,                   -- float16, L2-normalised (MERT)
    clap   BLOB,                   -- float16, L2-normalised (CLAP; for naming groups)
    visual BLOB,                   -- float16, L2-normalised
    cuts   REAL,                   -- scene cuts per minute (keyframes)
    duration REAL,
    name   TEXT,                   -- display name for "why" explanations
    at     REAL,
    PRIMARY KEY(kind, key)
);
-- thumbnails made by the engine from the frames it read anyway; the app records them in its library
CREATE TABLE IF NOT EXISTS thumb_made(video_id INTEGER PRIMARY KEY, path TEXT NOT NULL, at REAL NOT NULL);
"""

REC_SCHEMA = """
DROP TABLE IF EXISTS rec;
CREATE TABLE rec(
    video_id INTEGER PRIMARY KEY,
    audio REAL, mix REAL,          -- 0-100 match (percentile within the library)
    why_audio TEXT, why_visual TEXT,
    ga TEXT, gm TEXT,              -- per taste group: comma-separated percentiles (audio, audio+visual)
    liked INTEGER                  -- 1: the song is in the user's liked folders (a library copy of a liked file)
);
DROP TABLE IF EXISTS taste_group;
CREATE TABLE taste_group(id INTEGER PRIMARY KEY, name TEXT, size INTEGER, members TEXT);
"""

# Words used to describe taste groups. CLAP embeds text and audio in the same space, so each group is named by
# the mood, style and vocals that are most characteristic of it compared with the user's other groups.
LABEL_FACETS = {
    "mood": [("Calm", "calm, relaxing, gentle music"), ("Energetic", "energetic, intense, high-energy music"),
             ("Emotional", "emotional, melancholic, bittersweet music"), ("Happy", "happy, cheerful, uplifting music"),
             ("Dark", "dark, mysterious, ominous music"), ("Epic", "epic, dramatic, heroic music"),
             ("Playful", "cute, playful, bubbly music"), ("Dreamy", "dreamy, ethereal, atmospheric music"),
             ("Aggressive", "aggressive, angry, heavy music"), ("Nostalgic", "nostalgic, warm, retro music")],
    "style": [("rock", "rock music with electric guitars"), ("metal", "heavy metal music"), ("pop", "pop music"),
              ("electronic", "electronic dance music with synthesizers"), ("orchestral", "orchestral music with strings"),
              ("ballad", "a slow piano ballad"), ("jazz", "jazz music with brass"), ("hip-hop", "hip hop music with rap"),
              ("acoustic", "acoustic guitar music"), ("punk", "fast punk rock music"), ("city pop", "1980s city pop"),
              ("idol pop", "japanese idol pop music"), ("funk", "funky groovy music with bass"),
              ("folk", "folk music with traditional instruments")],
    "vocals": [("female vocals", "a song sung by a female singer"), ("male vocals", "a song sung by a male singer"),
               ("choir", "a song sung by a choir"), ("instrumental", "instrumental music without vocals"),
               ("duet", "a duet with male and female singers")],
}
LABEL_VERSION = 2

# ----------------------------------------------------------------------------- helpers

class Engine:
    def __init__(self, args):
        self.home = Path(args.home)
        self.ffmpeg = args.ffmpeg
        self.ffprobe = args.ffprobe
        self.sample = getattr(args, "sample", 0) or 0
        self.include = [p.lower() for p in (getattr(args, "include", "") or "").split(",") if p]
        self.db = sqlite3.connect(self.home / "features.db", timeout=60)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(feat)")}
        if "clap" not in cols:
            self.db.execute("ALTER TABLE feat ADD COLUMN clap BLOB")
        if "dsp" not in cols:
            self.db.execute("ALTER TABLE feat ADD COLUMN dsp TEXT")   # loudness / brightness / activity (naming)
        self.db.commit()
        self.cfg = json.loads((self.home / "config.json").read_text(encoding="utf-8-sig"))
        self.stamps = self.home / "engine"
        self.thumb_width = int(self.cfg.get("thumb_width") or 480)
        self.work = self.stamps / "work"                  # frame grabs in progress (emptied at start)
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True, exist_ok=True)
        self.status ={"state": "running", "phase": "Starting", "done": 0, "total": 0, "current": "", "error": None}
        self._last_status = 0.0
        self._stop_requested = False
        self._parent = _open_process(getattr(args, "parent_pid", 0) or 0)

    # progress for the app (it polls features.db meta 'status')
    def set_status(self, force=False, **kw):
        self.status.update(kw)
        if force or time.time() - self._last_status > 1.0:
            self._last_status = time.time()
            self.db.execute("INSERT INTO meta(key,value) VALUES('status',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps({**self.status, "at": time.time()}),))
            self.db.commit()

    def user_busy(self) -> bool:
        """The app touches these files when a video starts playing (in the app or in VLC). Browsing the app
        doesn't pause the analysis: the app's pages and thumbnails come from its own disk, not the archive."""
        now = time.time()
        for name, quiet in (("media.stamp", 4.0), ("play.stamp", 15.0)):
            try:
                if now - (self.stamps / name).stat().st_mtime < quiet:
                    return True
            except OSError:
                pass
        return False

    def yield_to_user(self, max_wait=120.0):
        waited = 0.0
        while self.user_busy() and waited < max_wait:
            time.sleep(0.5)
            waited += 0.5

    def should_stop(self) -> bool:
        """The app asked us to stop (quit), or the app is gone (crash): finish the current batch and exit."""
        if self._stop_requested:
            return True
        if (self.stamps / "stop.flag").exists() or (self._parent and _process_exited(self._parent)):
            self._stop_requested = True
        return self._stop_requested

    def library_db(self):
        return sqlite3.connect(f"file:{self.home / 'library.db'}?mode=ro", uri=True, timeout=60)


# Hard cap for the engine and each FFmpeg it starts. On Windows, CUDA allocations count towards a process's
# memory total, so this covers models + GPU work. Measured peak is far lower; the cap only stops runaways.
PROCESS_MEMORY_LIMIT = int(float(os.environ.get("KANADE_ENGINE_MEMLIMIT_GB", "8")) * 1024 ** 3)
# never fill the graphics card (no spill-over into system RAM). On Windows GPU memory also counts towards the
# process's memory total: the three models + batches need ~2 GB, so a third of a 12 GB card is plenty.
GPU_MEMORY_FRACTION = 0.3


def limit_own_memory(max_bytes=PROCESS_MEMORY_LIMIT):
    if max_bytes <= 0:
        return None
    """Put this process (and its children) in a Windows job object with a per-process memory cap.

    If anything ever goes over the cap, Windows refuses the allocation / ends just this process instead of
    letting the whole system run out of memory.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                       "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x00000100     # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.ProcessMemoryLimit = max_bytes
    ok = k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)) and \
        k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(k32.GetCurrentProcess()))
    print(f"memory limit {max_bytes // 1024 ** 3} GB: {'on' if ok else 'unavailable'}", flush=True)
    return job


def _open_process(pid):
    """A handle to the app's process, to notice when it is gone (works even if the pid is reused)."""
    if not pid or sys.platform != "win32":
        return None
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))      # SYNCHRONIZE
    return h or None


def _process_exited(handle) -> bool:
    import ctypes
    return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0          # WAIT_OBJECT_0: it has exited


def single_engine_lock(home: Path, wait_s=45.0):
    """Only one engine at a time (e.g. an old one still saving while a new app starts). Returns the lock file."""
    if sys.platform != "win32":
        return True
    import msvcrt
    d = home / "engine"
    d.mkdir(parents=True, exist_ok=True)
    f = open(d / "engine.lock", "a+b")
    t0 = time.time()
    while True:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return f
        except OSError:
            if time.time() - t0 > wait_s:
                f.close()
                return None
            time.sleep(1.0)


def run_quiet(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)


def probe_duration(ffprobe, path):
    try:
        out = run_quiet([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], timeout=60)
        return float(out.stdout.decode().strip().split()[0])
    except Exception:
        return None


def sample_points(duration):
    """Start times of three 10 s windows centred at 25 / 50 / 75 % (one window for very short videos)."""
    if not duration or duration <= WIN_S * 1.5:
        return [0.0]
    return [max(0.0, min(duration * f - WIN_S / 2, duration - WIN_S)) for f in (0.25, 0.5, 0.75)]


def decode_audio(ffmpeg, path, duration):
    """The three sample windows, read in ONE sequential pass (first window start -> last window end).

    On a hard disk this beats three seeking readers (measured 0.33 vs 0.58 s per video) because the
    disk never has to jump between positions; the windows are then cut out in memory (same samples:
    MERT embeddings agree to cosine >= 0.9996).
    """
    starts = sample_points(duration)
    last = starts[-1]
    # start the read at the first video-frame position (16 %) if that is earlier: the frame grabs that follow
    # then find everything in the OS cache (a few % more bytes read, no second trip to the disk)
    first = min(starts[0], FRAME_POS[0] * duration) if duration and len(starts) > 1 else starts[0]
    cmd = [ffmpeg, "-v", "error", "-threads", "1", "-ss", f"{first:.2f}", "-t", f"{last - first + WIN_S:.2f}", "-i", path,
           "-vn", "-sn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    try:
        out = run_quiet(cmd)
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0 or len(out.stdout) < SR * 4:
        return None
    a = np.frombuffer(out.stdout, dtype=np.float32)
    n = int(WIN_S * SR)
    wins = []
    for st in starts:
        w = a[int(round((st - first) * SR)):][:n]
        if len(w) > SR:                                  # keep the 10 s grid intact for audio_windows()
            wins.append(w if len(w) == n else np.pad(w, (0, n - len(w))))
    return np.concatenate(wins) if wins else None


def audio_windows(a):
    """Split the decoded samples back into 10 s windows (padding the last one)."""
    n = int(WIN_S * SR)
    wins = [a[i:i + n] for i in range(0, len(a), n)][:3]
    return [w if len(w) == n else np.pad(w, (0, n - len(w))) for w in wins if len(w) > SR]


def audio_stats(a):
    """Plain acoustic measurements of the sampled audio, used to describe taste groups in words."""
    hop = 2048
    frames = a[: len(a) // hop * hop].reshape(-1, hop)
    if len(frames) < 8:
        return None
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    spec = np.abs(np.fft.rfft(frames * np.hanning(hop), axis=1))
    freqs = np.fft.rfftfreq(hop, 1.0 / SR)
    total = spec.sum(axis=1) + 1e-9
    centroid = (spec * freqs).sum(axis=1) / total
    flux = np.sqrt((np.clip(np.diff(spec, axis=0), 0, None) ** 2).sum(axis=1)) / total[1:]
    db = 20 * np.log10(rms + 1e-9)
    return {"loud": float(np.median(db)), "dyn": float(np.std(db)), "bright": float(np.median(centroid)),
            "activity": float(np.mean(flux)), "bass": float(np.mean(spec[:, freqs < 200].sum(axis=1) / total))}


def probe_pace(ffprobe, path, duration):
    """Keyframes per minute inside the same three windows (keyframes follow scene cuts): a cheap pace estimate."""
    starts = sample_points(duration)
    intervals = ",".join(f"{s:.2f}%+{WIN_S:.0f}" for s in starts)
    try:
        out = run_quiet([ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", intervals,
                         "-show_entries", "packet=flags", "-of", "csv=p=0", path], timeout=60)
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    keys = sum(1 for ln in out.stdout.decode(errors="ignore").splitlines() if ln.startswith("K"))
    sampled = WIN_S * len(starts)
    return keys / (sampled / 60.0)


def grab_frames(eng, path, duration, tag):
    """The keyframes at FRAME_POS in ONE FFmpeg run (one single-threaded input per position), scaled like the
    app's thumbnails. Same command as the app's thumbnail maker, so the frames (and thumbnails) are identical."""
    from PIL import Image
    dur = duration or 90.0
    times = [max(0.0, dur * f) for f in FRAME_POS]
    d = eng.work / f"{tag}-{threading.get_ident()}"
    d.mkdir(parents=True, exist_ok=True)
    cmd = [eng.ffmpeg, "-hide_banner", "-loglevel", "error"]
    for t in times:
        cmd += ["-threads", "1", "-discard", "nokey", "-skip_frame", "nokey", "-ss", f"{t:.2f}", "-i", path]
    for i in range(len(times)):
        cmd += ["-map", f"{i}:v:0", "-frames:v", "1", "-vf", f"scale={eng.thumb_width}:-2:flags=lanczos", "-y",
                str(d / f"{i}.ppm")]
    frames = []
    try:
        run_quiet(cmd, timeout=120)
        for i in range(len(times)):
            f = d / f"{i}.ppm"
            if f.is_file() and f.stat().st_size > 0:
                try:
                    with Image.open(f) as im:
                        frames.append(im.convert("RGB"))
                except OSError:
                    pass
    except (subprocess.TimeoutExpired, OSError):
        pass
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return frames


def frame_score(img) -> float:
    """How good a frame looks as a thumbnail. Identical to media.frame_score in the app (keep them in sync)."""
    from PIL import Image, ImageChops, ImageFilter, ImageStat
    small = img.resize((160, 90), Image.BILINEAR)
    r, g, b = small.split()
    lum = small.convert("L")
    st = ImageStat.Stat(lum)
    mean, std = st.mean[0], st.stddev[0]
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


def save_thumbnail(eng, video_id: int, frames) -> str | None:
    """Write the best frame as the video's thumbnail (atomic swap, like the app does)."""
    if not frames:
        return None
    best = max(frames, key=frame_score)
    sub = eng.home / "thumbs" / f"{video_id // 1000:03d}"
    sub.mkdir(parents=True, exist_ok=True)
    dest = sub / f"{video_id}.webp"
    tmp = dest.with_suffix(".tmp")
    try:
        best.save(tmp, "WEBP", quality=82, method=4)
        os.replace(tmp, dest)                    # the old thumbnail stays until the new one is complete
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return str(dest)


def to_blob(v):
    return None if v is None else np.asarray(v, dtype=np.float16).tobytes()


def from_blob(b):
    return None if b is None else np.frombuffer(b, dtype=np.float16).astype(np.float32)


def l2(v):
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(n, 1e-8)


# ----------------------------------------------------------------------------- models

class Models:
    def __init__(self):
        import torch
        from transformers import AutoImageProcessor, AutoModel, ClapModel, ClapProcessor, Wav2Vec2FeatureExtractor
        self.torch = torch
        torch.set_num_threads(max(1, (os.cpu_count() or 4) // 2))
        self.dev = "cuda" if torch.cuda.is_available() else "cpu"
        if self.dev == "cuda":
            # stay well inside the card's memory: going over would make the driver spill into system RAM
            torch.cuda.set_per_process_memory_fraction(GPU_MEMORY_FRACTION)
        self.mert = AutoModel.from_pretrained(MERT_MODEL, revision=MERT_REVISION, trust_remote_code=True).to(self.dev).eval()
        self.mert_proc = Wav2Vec2FeatureExtractor.from_pretrained(MERT_MODEL, revision=MERT_REVISION, trust_remote_code=True)
        half = torch.float16 if self.dev == "cuda" else torch.float32   # naming / visual models: half precision on GPU
        self.half = half
        self.clap = ClapModel.from_pretrained(AUDIO_MODEL, torch_dtype=half).to(self.dev).eval()
        self.clap_proc = ClapProcessor.from_pretrained(AUDIO_MODEL)
        self.vis = AutoModel.from_pretrained(VISUAL_MODEL, torch_dtype=half).to(self.dev).eval()
        import gc
        gc.collect()                                   # drop the CPU-side copies left over from loading
        self.vis_proc = AutoImageProcessor.from_pretrained(VISUAL_MODEL)

    def audio(self, windows):
        """MERT: windows (1-D float32 at 48 kHz) -> (n, 1536) normalised embeddings (mid + upper layers, time-averaged)."""
        torch = self.torch
        x24 = [((w[0::2] + w[1::2]) * 0.5).astype(np.float32) for w in windows]     # 48 kHz -> 24 kHz
        inputs = self.mert_proc(x24, sampling_rate=24000, return_tensors="pt", padding=True)
        inputs = {k: v.to(self.dev) for k, v in inputs.items()}
        # half precision on the GPU: twice as fast, same embeddings (measured cosine 1.00000 to full precision)
        with torch.no_grad(), torch.autocast("cuda", dtype=torch.float16, enabled=self.dev == "cuda"):
            hs = self.mert(**inputs, output_hidden_states=True).hidden_states          # 13 x (n, frames, 768)
        h = torch.stack(hs[1:]).float()                                                 # (12, n, frames, 768)
        feat = torch.cat([h[3:7].mean(dim=(0, 2)), h[7:11].mean(dim=(0, 2))], dim=-1)   # (n, 1536)
        return l2(feat.float().cpu().numpy())

    def clap_audio(self, windows):
        """CLAP: windows -> (n, 512) normalised embeddings (same space as the text descriptions)."""
        torch = self.torch
        try:
            inputs = self.clap_proc(audio=windows, sampling_rate=SR, return_tensors="pt")
        except TypeError:
            inputs = self.clap_proc(audios=windows, sampling_rate=SR, return_tensors="pt")
        inputs = {k: (v.to(self.dev, self.half) if v.is_floating_point() else v.to(self.dev))
                  for k, v in inputs.items() if k in ("input_features", "is_longer")}
        with torch.no_grad():
            f = self.clap.get_audio_features(**inputs)
        return l2(f.float().cpu().numpy())

    def clap_inputs(self, windows):
        """CPU part of CLAP (mel spectrograms, ~70 % of its time): runs in the parallel CPU stage."""
        try:
            inputs = self.clap_proc(audio=windows, sampling_rate=SR, return_tensors="np")
        except TypeError:
            inputs = self.clap_proc(audios=windows, sampling_rate=SR, return_tensors="np")
        return {k: np.asarray(inputs[k]) for k in ("input_features", "is_longer") if k in inputs}

    def clap_from_inputs(self, parts):
        """GPU part of CLAP: a list of clap_inputs() results -> (n_windows, 512) normalised embeddings."""
        torch = self.torch
        cat = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}

        def run(idx):
            batch = {}
            for k, v in cat.items():
                t = torch.from_numpy(np.ascontiguousarray(v[idx[0]:idx[-1] + 1]))
                batch[k] = t.to(self.dev, self.half) if t.is_floating_point() else t.to(self.dev)
            with torch.no_grad():
                f = self.clap.get_audio_features(**batch)
            return l2(f.float().cpu().numpy())
        return self.chunked(run, list(range(len(cat["input_features"]))), 6)

    def chunked(self, fn, items, size=6):
        """Run a model over items in small batches; on GPU out-of-memory, free the cache and retry smaller."""
        out, i = [], 0
        while i < len(items):
            part = items[i:i + size]
            try:
                out.append(fn(part))
                i += len(part)
            except self.torch.cuda.OutOfMemoryError:
                self.torch.cuda.empty_cache()
                if size == 1:
                    raise
                size = max(1, size // 2)
        return np.concatenate(out) if out else np.zeros((0, 1), np.float32)

    def texts(self, texts):
        torch = self.torch
        inputs = self.clap_proc(text=texts, return_tensors="pt", padding=True)
        inputs = {k: v.to(self.dev) for k, v in inputs.items() if k in ("input_ids", "attention_mask")}
        with torch.no_grad():
            f = self.clap.get_text_features(**inputs)
        return l2(f.float().cpu().numpy())

    def images(self, imgs):
        torch = self.torch
        inputs = self.vis_proc(images=imgs, return_tensors="pt")
        inputs = {k: (v.to(self.dev, self.half) if v.is_floating_point() else v.to(self.dev)) for k, v in inputs.items()}
        with torch.no_grad():
            f = self.vis.get_image_features(**inputs)
        return l2(f.float().cpu().numpy())


# ----------------------------------------------------------------------------- analysis

def list_taste_files(folders):
    out = []
    for folder in folders:
        for dp, _, fs in os.walk(folder):
            for f in fs:
                if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                    p = os.path.join(dp, f)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    out.append(("taste", p, f"{st.st_size}:{int(st.st_mtime)}", p, None, None, os.path.splitext(f)[0]))
    return out


def pending_items(eng: Engine):
    done = {(k, key): (fp, model) for k, key, fp, model in eng.db.execute("SELECT kind, key, fp, model FROM feat")}
    items = []
    folders = [f for f in eng.cfg.get("taste_folders") or [] if os.path.isdir(f)]
    taste = list_taste_files(folders)
    wanted_taste = {t[1] for t in taste}
    # forget liked files that were removed / whose folder was removed
    for (k, key) in list(done):
        if k == "taste" and key not in wanted_taste:
            eng.db.execute("DELETE FROM feat WHERE kind='taste' AND key=?", (key,))
    eng.db.commit()
    for t in taste:
        if done.get(("taste", t[1])) != (t[2], MODEL_TAG):
            items.append(t)
    lib = eng.library_db()
    tcols = {r[1] for r in lib.execute("PRAGMA table_info(video)")}
    algo = "v.thumb_algo" if "thumb_algo" in tcols else "0"
    by_folder: dict = {}
    for vid, path, size, mtime, upgrade, dur, title, folder in lib.execute(
            f"SELECT v.id, v.path, v.size, v.mtime, (v.thumb_state=1 AND {algo}<{THUMB_ALGO}), v.duration, "
            "COALESCE(ao.title_romaji, a.title_romaji, v.filename), v.folder FROM video v "
            "LEFT JOIN anime a ON a.id=v.anime_id LEFT JOIN anime_override ao ON ao.anime_id=a.id "
            "WHERE v.missing=0 ORDER BY v.folder_year DESC, v.path"):
        fp = f"{size}:{int(mtime or 0)}"
        if done.get(("lib", str(vid))) != (fp, MODEL_TAG):
            # 5th field: this video's thumbnail is due an upgrade (made from the frames read anyway)
            by_folder.setdefault(folder, []).append(("lib", str(vid), fp, path, bool(upgrade), dur, title))
    # Every era early: runs of 8 neighbouring files from each year folder in turn. For You is then representative
    # of the whole library long before the analysis ends; runs keep the disk seeks short (costs ~2 % speed).
    runs = [[f[i:i + 8] for i in range(0, len(f), 8)] for f in by_folder.values()]
    for r in range(max((len(x) for x in runs), default=0)):
        for x in runs:
            if r < len(x):
                items.extend(x[r])
    # videos the taste model learns from first (library copies of liked files, favourites, rated videos), so
    # For You is useful within minutes; then everything else
    first = set()
    tables = {r[0] for r in lib.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    try:
        if "taste_match" in tables:
            for (cands,) in lib.execute("SELECT cands FROM taste_match"):
                first.update(str(int(x)) for x in json.loads(cands or "[]"))
        first.update(str(v) for (v,) in lib.execute("SELECT video_id FROM favorite"))
        if "rating" in tables:
            first.update(str(v) for (v,) in lib.execute("SELECT video_id FROM rating"))
    except (sqlite3.Error, ValueError):
        pass
    lib.close()
    if first:
        items = [i for i in items if i[0] == "taste"] + [i for i in items if i[0] == "lib" and i[1] in first] + \
                [i for i in items if i[0] == "lib" and i[1] not in first]
    if eng.sample:                       # testing aid: a random sample of the library (+ named files)
        import random
        libs = [i for i in items if i[0] == "lib"]
        keep = [i for i in libs if os.path.basename(i[3]).lower().startswith(tuple(eng.include))] if eng.include else []
        rest = [i for i in libs if i not in keep]
        random.Random(1).shuffle(rest)
        items = [i for i in items if i[0] == "taste"] + keep + rest[:max(0, eng.sample - len(keep))]
    return items


def read_item(eng: Engine, item):
    """Disk stage (one reader): the audio windows in one sequential read, which also pulls the frame
    positions into the OS cache."""
    kind, key, fp, path, upgrade, dur, name = item
    eng.yield_to_user()
    dur2 = dur or probe_duration(eng.ffprobe, path)             # library durations are already known
    audio = decode_audio(eng.ffmpeg, path, dur2)
    return dur2 or dur, audio


def frames_item(eng: Engine, item, dur, audio, models):
    """CPU stage (parallel, data already cached): frames, pace, thumbnail, CLAP spectrograms, measurements."""
    kind, key, fp, path, upgrade, _, name = item
    frames = grab_frames(eng, path, dur, key if kind == "lib" else "t")
    cuts = probe_pace(eng.ffprobe, path, dur)                  # same windows, packet headers only
    thumb = save_thumbnail(eng, int(key), frames) if kind == "lib" and upgrade and audio is not None else None
    small = []
    for im in frames:                                          # the visual model takes 224 px: keep memory small
        im = im.copy()
        im.thumbnail((320, 320))
        small.append(im)
    clapin = models.clap_inputs(audio_windows(audio)) if audio is not None else None
    return item, audio, small, dur, cuts, (audio_stats(audio) if audio is not None else None), thumb, clapin


def analyse(eng: Engine):
    items = pending_items(eng)
    total = len(items)
    eng.set_status(force=True, phase="Analysing videos", done=0, total=total, current="")
    if not items:
        return
    eng.set_status(force=True, phase="Loading AI models", current="first run downloads ~2 GB")
    models = Models()
    store_label_vectors(eng, models)
    eng.set_status(force=True, phase="Analysing videos", current=f"on {models.dev.upper()}")
    from concurrent.futures import ThreadPoolExecutor
    q: queue.Queue = queue.Queue(maxsize=8)       # small read-ahead: bounded memory
    it = iter(items)
    pool = ThreadPoolExecutor(max_workers=FRAME_WORKERS, thread_name_prefix="frames")

    def failed_item(item):
        return item, None, [], None, None, None, None, None

    def cpu_stage(item, dur, audio):
        try:
            return frames_item(eng, item, dur, audio, models)
        except Exception as e:
            print("frames failed", item[3], e, flush=True)
            return item, None, [], dur, None, None, None, None     # retried next run

    def producer():
        # ONE disk reader: on a hard disk 2-3 parallel readers make every file slower (0.36 -> 0.56 -> 0.88 s)
        # because the head jumps between files. Frames, pace and thumbnails then run in parallel from the OS
        # cache while the reader moves on to the next file, and the GPU works on the previous batch.
        while True:
            item = None if eng.should_stop() else next(it, None)
            if item is None:
                q.put(None)
                return
            try:
                dur, audio = read_item(eng, item)
                q.put(pool.submit(cpu_stage, item, dur, audio))
            except Exception as e:
                print("decode failed", item[3], e, flush=True)
                q.put(failed_item(item))

    threading.Thread(target=producer, daemon=True).start()
    done = 0
    batch = []

    def flush():
        if not batch:
            return
        # audio: all windows of the batch in one GPU call
        wins, owners = [], []
        for i, (_, audio, *_rest) in enumerate(batch):
            if audio is not None:
                for w in audio_windows(audio):
                    wins.append(w)
                    owners.append(i)
        a_emb = models.chunked(models.audio, wins, 6) if wins else np.zeros((0, 1))
        clap_parts = [b[7] for b in batch if b[1] is not None]
        c_emb = models.clap_from_inputs(clap_parts) if clap_parts else np.zeros((0, 1))
        imgs, img_owner = [], []
        for i, (_, audio, ims, *_rest) in enumerate(batch):
            if audio is None:
                continue
            for im in ims:
                imgs.append(im)
                img_owner.append(i)
        v_emb = models.chunked(models.images, imgs, 8) if imgs else np.zeros((0, 1))
        now = time.time()
        for i, (item, audio, ims, dur, cuts, dsp, thumb, _) in enumerate(batch):
            kind, key, fp, path, _, _, name = item
            if audio is None:
                failed[0] += 1             # unreadable right now (disk asleep, file in use...): retried next run
                continue
            if thumb:
                eng.db.execute("INSERT INTO thumb_made(video_id,path,at) VALUES(?,?,?) ON CONFLICT(video_id) "
                               "DO UPDATE SET path=excluded.path, at=excluded.at", (int(key), thumb, now))
            a = a_emb[[j for j, o in enumerate(owners) if o == i]]
            v = v_emb[[j for j, o in enumerate(img_owner) if o == i]]
            cl = c_emb[[j for j, o in enumerate(owners) if o == i]]
            a = l2(a.mean(axis=0)) if len(a) else None
            cl = l2(cl.mean(axis=0)) if len(cl) else None
            v = l2(v.mean(axis=0)) if len(v) else None
            eng.db.execute("INSERT INTO feat(kind,key,fp,model,audio,clap,visual,cuts,duration,name,at,dsp) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                           "ON CONFLICT(kind,key) DO UPDATE SET fp=excluded.fp, model=excluded.model, audio=excluded.audio, "
                           "clap=excluded.clap, visual=excluded.visual, cuts=excluded.cuts, duration=excluded.duration, "
                           "name=excluded.name, at=excluded.at, dsp=excluded.dsp",
                           (kind, key, fp, MODEL_TAG, to_blob(a), to_blob(cl), to_blob(v), cuts, dur, name, now,
                            json.dumps(dsp) if dsp else None))
        eng.db.commit()
        batch.clear()
        flushes[0] += 1
        if models.dev == "cuda":                  # hand cached GPU memory back after every batch (keeps the
            models.torch.cuda.empty_cache()       # process's memory total low and flat; costs ~1 ms)
        if flushes[0] % 40 == 0:
            import gc
            gc.collect()

    flushes = [0]
    failed = [0]
    last_rank = time.time()
    try:
        while True:
            got = q.get()
            if got is None:
                break
            if not isinstance(got, tuple):
                got = got.result()                 # the CPU stage never raises (see cpu_stage)
            batch.append(got)
            done += 1
            eng.set_status(done=done, current=os.path.basename(got[0][3]))
            if len(batch) >= 8:
                flush()
            if eng.should_stop():
                continue                           # the producer ends; what was read is still saved below
            # re-rank when the user rated videos (the app asks via rank.request), or every 10 minutes
            if rank_requested(eng, last_rank) or time.time() - last_rank > 600:
                flush()
                rank(eng)
                last_rank = time.time()
                eng.set_status(force=True, phase="Analysing videos")
        flush()                                    # every finished video is saved, also when stopping
    finally:
        pool.shutdown(wait=True)
    if failed[0]:
        print(f"{failed[0]} videos could not be read; they will be retried next time", flush=True)


def rank_requested(eng: Engine, since: float) -> bool:
    try:
        return (eng.stamps / "rank.request").stat().st_mtime > since
    except OSError:
        return False


# ----------------------------------------------------------------------------- taste groups

def store_label_vectors(eng: Engine, models=None):
    """Embed the describing words once (needs the CLAP text tower) and keep them in features.db."""
    row = eng.db.execute("SELECT value FROM meta WHERE key='label_vectors'").fetchone()
    if row:
        try:
            if json.loads(row[0]).get("version") == LABEL_VERSION:
                return
        except ValueError:
            pass
    if models is None:
        models = Models()
    out = {"version": LABEL_VERSION, "facets": {}}
    for facet, items in LABEL_FACETS.items():
        vecs = models.texts([d for _, d in items])
        out["facets"][facet] = {"names": [n for n, _ in items], "vectors": vecs.astype(np.float32).tolist()}
    eng.db.execute("INSERT INTO meta(key,value) VALUES('label_vectors',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (json.dumps(out),))
    eng.db.commit()


def label_vectors(eng: Engine):
    row = eng.db.execute("SELECT value FROM meta WHERE key='label_vectors'").fetchone()
    if not row:
        return None
    d = json.loads(row[0])
    if d.get("version") != LABEL_VERSION:
        return None
    return {f: (v["names"], l2(np.asarray(v["vectors"], dtype=np.float32))) for f, v in d["facets"].items()}


def spherical_kmeans(X, k, rng, iters=60, restarts=10):
    """k-means on the unit sphere (cosine), k-means++ seeding, best of several restarts."""
    best = None
    n = len(X)
    for _ in range(restarts):
        C = [X[rng.integers(n)]]
        for _ in range(1, k):
            d = np.clip(1.0 - np.max(X @ np.stack(C).T, axis=1), 0, None)
            p = d / d.sum() if d.sum() > 0 else None
            C.append(X[rng.choice(n, p=p)])
        C = np.stack(C)
        for _ in range(iters):
            lab = np.argmax(X @ C.T, axis=1)
            newC = np.stack([l2(X[lab == j].mean(axis=0)) if (lab == j).any() else C[j] for j in range(k)])
            if np.allclose(newC, C, atol=1e-5):
                break
            C = newC
        lab = np.argmax(X @ C.T, axis=1)
        inertia = float(np.sum(1.0 - np.max(X @ C.T, axis=1)))
        if best is None or inertia < best[0]:
            best = (inertia, lab, C)
    return best[1], best[2]


def silhouette(X, lab):
    D = np.clip(1.0 - X @ X.T, 0, None)
    ks = np.unique(lab)
    if len(ks) < 2:
        return -1.0
    s = []
    for i in range(len(X)):
        same = lab == lab[i]
        if same.sum() <= 1:
            s.append(0.0)
            continue
        a = D[i, same].sum() / (same.sum() - 1)
        b = min(D[i, lab == k].mean() for k in ks if k != lab[i])
        s.append((b - a) / max(a, b, 1e-9))
    return float(np.mean(s))


def taste_groups(TA, rng):
    """Cluster liked songs by sound; the number of groups (1-8) is picked by silhouette score."""
    n = len(TA)
    if n < 12:
        return np.zeros(n, dtype=int)
    best = (0.02, np.zeros(n, dtype=int))           # one group unless clusters separate clearly
    for k in range(2, min(8, n // 6) + 1):
        lab, _ = spherical_kmeans(TA, k, rng)
        sizes = np.bincount(lab, minlength=k)
        if sizes.min() < 3:
            continue
        sc = silhouette(TA, lab)
        if sc > best[0]:
            best = (sc, lab)
    # renumber: biggest group first
    lab = best[1]
    order = np.argsort(-np.bincount(lab))
    remap = {int(old): new for new, old in enumerate(order)}
    return np.array([remap[int(x)] for x in lab])


def name_groups(eng, lab, TC, dsp):
    """Describe each taste group in words.

    Mood + tone come from plain acoustic measurements (reliable): how loud and busy the music is (calm ... intense)
    and how bright or bass-heavy it sounds. A style word comes from CLAP: the style the group matches clearly more
    than the user's other groups do ("lift" of zero-shot probabilities).
    """
    n = int(lab.max()) + 1
    names = []
    # --- mood & tone from measurements, relative to all of the user's liked videos
    have = [i for i, d in enumerate(dsp) if d]
    if len(have) >= 6:
        keys = ("loud", "activity", "bright", "bass", "dyn")
        Mx = np.array([[dsp[i][k] for k in keys] for i in have], dtype=np.float64)
        mu, sd = Mx.mean(axis=0), Mx.std(axis=0) + 1e-9
        Z = (Mx - mu) / sd
        labh = lab[have]
        zs = [Z[labh == g].mean(axis=0) if (labh == g).any() else np.zeros(len(keys)) for g in range(n)]
        energy = np.array([0.5 * z[0] + 0.5 * z[1] - 0.25 * z[4] for z in zs])
        tone = np.array([z[2] - 0.6 * z[3] for z in zs])
        order = np.argsort(energy)                       # groups are named relative to each other
        for g in range(n):
            if n > 1 and g == order[-1]:
                mood = "Intense"
            elif n > 1 and g == order[0]:
                mood = "Calm"
            else:
                mood = "Upbeat" if energy[g] >= np.median(energy) else "Mellow"
            loud = energy[g] >= np.median(energy)
            shade = "bright" if tone[g] > 0.25 else (("heavy" if loud else "soft") if tone[g] < -0.25 else "")
            names.append([mood, shade])
    else:
        names = [[f"Group {chr(65 + g)}", ""] for g in range(n)]
    # --- style word from CLAP zero-shot, by lift over the user's other groups
    facets = label_vectors(eng)
    if facets and TC is not None and len(TC) == len(lab):
        sty_names, sty_vecs = facets["style"]
        logits = 100.0 * (TC @ sty_vecs.T)
        P = np.exp(logits - logits.max(axis=1, keepdims=True))
        P /= P.sum(axis=1, keepdims=True)
        overall = P.mean(axis=0) + 1e-9
        for g in range(n):
            pg = P[lab == g].mean(axis=0)
            lift = pg / overall
            ok = pg >= 0.5 * pg.max()                       # must also be one of the group's likelier styles
            j = int(np.argmax(np.where(ok, lift, 0)))
            names[g].append(sty_names[j] if lift[j] > 1.15 else "")
    out = []
    for g in range(n):
        mood, shade, *style = names[g]
        style = style[0] if style else ""
        text = mood + (f" & {shade}" if shade else "") + (f" {style}" if style else "")
        while text in out:
            text += " ·"
        out.append(text)
    return out


# ----------------------------------------------------------------------------- ranking

def ensure_rec_tables(eng: Engine):
    """Create the result tables, or rebuild them if an older version had other columns."""
    have = {r[1] for r in eng.db.execute("PRAGMA table_info(rec)")}
    groups = {r[1] for r in eng.db.execute("PRAGMA table_info(taste_group)")}
    if have != {"video_id", "audio", "mix", "why_audio", "why_visual", "ga", "gm", "liked"} or \
            groups != {"id", "name", "size", "members"}:
        eng.db.executescript(REC_SCHEMA)


def rank(eng: Engine):
    """Learn the user's taste and score every analysed library video (taste_model.py explains how). Both modes
    (sound only / everything) are precomputed so switching in the app is instant."""
    import taste_model as tm
    eng.set_status(force=True, phase="Learning your taste")
    t0 = time.time()
    ensure_rec_tables(eng)
    ldb = eng.library_db()
    try:
        d = tm.load(eng.db, ldb, MODEL_TAG, eng.cfg.get("taste_use_favorites", True))
    finally:
        ldb.close()
    y = None
    if d is not None:
        tm.find_twins(d)
        y = tm.song_labels(d)
    if d is None or (not (y > 0).any() and not d.taste):
        eng.db.execute("DELETE FROM rec")
        eng.db.execute("DELETE FROM taste_group")
        eng.db.commit()
        return
    try:
        store_label_vectors(eng)
    except Exception as e:                           # naming is cosmetic; never block ranking
        print("group naming unavailable:", e, flush=True)

    pos = np.where(y > 0)[0]
    loose = [t for t in d.taste if t["twin"] is None]   # liked files that aren't in the library
    m_fill = tm.l2(d.M[pos].mean(axis=0)) if len(pos) else np.zeros(d.M.shape[1], np.float32)

    def loose_vecs(mode):
        if not loose:
            return None
        if mode == "audio":
            return tm.l2(np.stack([np.concatenate([t["A"], 0.6 * t["C"]]) for t in loose]))
        return tm.l2(np.stack([np.concatenate([t["A"], 0.6 * t["C"], 0.8 * t["V"], 0.8 * m_fill]) for t in loose]))

    la, lm = loose_vecs("audio"), loose_vecs("mix")
    s_a, near_a = tm.score(d, y, "audio", extra_pos=la)
    s_m, near_m = tm.score(d, y, "mix", extra_pos=lm)
    pa, pm = tm.percentile(s_a), tm.percentile(s_m)
    like_names = [d.song_name[s] for s in pos] + [t["name"] for t in loose]   # same order as score() uses

    # taste groups: the liked songs clustered by sound
    TA = np.concatenate([d.A[pos]] + ([np.stack([t["A"] for t in loose])] if loose else []))
    TC = np.concatenate([d.song_clap_raw[pos]] + ([np.stack([t["clap"] for t in loose])] if loose else []))
    dsp = [d.song_dsp[s] for s in pos] + [t["dsp"] for t in loose]
    lab = taste_groups(TA, np.random.default_rng(7))
    n_groups = int(lab.max()) + 1 if len(lab) else 1
    group_pa, group_pm, groups = [], [], []
    if n_groups > 1:
        Za, Zm = tm.similarity_space(d, "audio"), tm.similarity_space(d, "mix")
        Pa = np.concatenate([Za[pos]] + ([la] if la is not None else []))
        Pm = np.concatenate([Zm[pos]] + ([lm] if lm is not None else []))
        names = name_groups(eng, lab, TC, dsp)
        for g in range(n_groups):
            sel = np.where(lab == g)[0]
            ka = tm._topk_mean(Za @ Pa[sel].T, 3)
            km = tm._topk_mean(Zm @ Pm[sel].T, 3)
            group_pa.append(tm.percentile(tm._z(ka) + 0.35 * tm._z(s_a)))
            group_pm.append(tm.percentile(tm._z(km) + 0.35 * tm._z(s_m)))
            centre = tm.l2(TA[sel].mean(axis=0))
            order = sel[np.argsort(-(TA[sel] @ centre))]
            groups.append((g, names[g], len(sel), json.dumps([like_names[i] for i in order], ensure_ascii=False)))

    def gjoin(arrs, s):
        return ",".join(f"{a[s]:.1f}" for a in arrs) if arrs else None

    in_folders = {t["twin"] for t in d.taste if t["twin"] is not None}
    rows = []
    for i, v in enumerate(d.vid):
        s = int(d.song_of[i])
        rows.append((int(v), round(float(pa[s]), 1), round(float(pm[s]), 1), like_names[near_a[s]],
                     like_names[near_m[s]], gjoin(group_pa, s), gjoin(group_pm, s), int(s in in_folders)))
    summary = {"liked_songs": int(len(pos)), "disliked_songs": int((y < 0).sum()),
               "twins": sum(1 for t in d.taste if t["twin"] is not None), "loose": len(loose),
               "songs": len(d.keys), "seconds": round(time.time() - t0, 2)}
    # swap in the new results in ONE transaction: if the engine is stopped mid-way, the previous list stays
    eng.db.execute("DELETE FROM rec")
    eng.db.execute("DELETE FROM taste_group")
    eng.db.executemany("INSERT INTO taste_group(id,name,size,members) VALUES(?,?,?,?)", groups)
    eng.db.executemany("INSERT INTO rec(video_id,audio,mix,why_audio,why_visual,ga,gm,liked) VALUES(?,?,?,?,?,?,?,?)", rows)
    for k, v in (("rank_at", str(time.time())), ("taste_count", str(len(pos) + len(loose))),
                 ("group_count", str(n_groups)), ("rank_summary", json.dumps(summary))):
        eng.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    eng.db.commit()
    print("ranked:", json.dumps(summary), flush=True)


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--rank-only", action="store_true")
    ap.add_argument("--prefetch", action="store_true", help="download the models and exit")
    ap.add_argument("--sample", type=int, default=0, help="testing: analyse only N random library videos")
    ap.add_argument("--include", default="", help="testing: comma-separated filename prefixes always sampled")
    ap.add_argument("--parent-pid", type=int, default=0, help="the app's process: exit when it is gone")
    args = ap.parse_args()
    if args.prefetch:
        Models()
        print("models ready", flush=True)
        return
    limit_own_memory()
    lock = single_engine_lock(Path(args.home))
    if lock is None:
        print("another engine is still running; leaving it to finish", flush=True)
        return
    try:
        (Path(args.home) / "engine" / "stop.flag").unlink()      # a stale stop request from an earlier quit
    except OSError:
        pass
    eng = Engine(args)
    try:
        if not args.rank_only:
            analyse(eng)
        if eng.should_stop():
            print("stopped: progress saved, analysis continues next time", flush=True)
            eng.set_status(force=True, state="idle", phase="", current="", error=None)
            return
        rank(eng)
        eng.set_status(force=True, state="idle", phase="", current="", error=None)
    except Exception as e:
        traceback.print_exc()
        eng.set_status(force=True, state="error", error=f"{type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
