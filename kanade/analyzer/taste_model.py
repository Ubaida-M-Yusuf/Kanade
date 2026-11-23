"""Kanade taste model: learns what the user likes from likes, dislikes, favourites and liked folders.

Everything works on SONGS (all versions / resolutions of one opening count once), described by four blocks:

  * A: MERT music embedding (rhythm, timbre, harmony), centred on the library and normalised. Centring removes
    the direction every song shares, which otherwise makes all songs look ~0.96 similar (measured: held-out
    favourites rose from the 31st to the 67th percentile with centring alone).
  * C: CLAP music embedding (genre / mood / vocals), centred the same way.
  * V: SigLIP 2 image embedding averaged over six frames (art style, colours, era of the animation).
  * M: the anime's genres and tags (weighted by rarity), release year and format, and OP vs ED.
  * R: the singers / bands (sound + look + genre mode). Only artists of rated songs get a column, so a like makes
    other songs by the same artist rise and a dislike makes them sink (measured on real later likes: songs liked
    later share an artist with an earlier like three times as often as other songs do).

Liked folders contain copies of openings that are usually also in the library (as AnimeThemes files): such a
"twin" is found by name (done by the app: taste_match table) and confirmed by sound, and the library copy is used,
so the model learns from the same kind of files it ranks (YouTube copies sound and look different).

The score of a song combines
  1. a small learned model (regularised logistic regression on all four blocks): positives = liked songs,
     disliked songs count strongly against, every other song counts weakly against (most of a big library isn't
     to one person's taste); and
  2. similarity to the closest liked songs (keeps distinct tastes apart, works from the very first like),
     minus similarity to the closest disliked songs.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field

import numpy as np

# tuned on held-out liked songs (see RECOMMENDATIONS.md); the evaluation script lives with the engine tests
PARAMS = {
    "w_a": 1.0, "w_c": 0.6, "w_v": 1.0, "w_m": 1.2,     # block weights inside the learned model
    "w_r": 2.0,                                          # artist block (sound + look + genre mode only)
    "w_explicit": 3.0,                                   # a like / favourite weighs this much more than a liked-folder file
    "year_w": 1.0,                                       # weight of the release year inside the story block
    "lam": 3e-3,                                         # L2 strength of the learned model
    "unl_ratio": 1.0,                                    # total weight of unrated songs vs liked songs
    "neg_w": 3.0,                                        # weight of one dislike (as strong as a like in the app)
    "knn_k": 3,
    # final blend (z-scores). With 3+ liked songs the learned model alone ranked best (similarity added nothing in
    # the held-out test); similarity is the ranking until then. Similarity to disliked songs always counts against.
    "mix_model": 1.0, "mix_knn": 0.0, "mix_neg": 0.6,
    "twin_min": 0.60,                                    # centred MERT cosine to accept a named twin (measured: same
                                                         # recording 0.68-0.96, other songs of the series <= 0.44)
    "twin_audio_only": 0.93,                             # ... to accept a twin found by sound alone
}
TAG_MIN_SERIES = 12           # tags used by fewer series are too rare to learn from
YEAR_LO, YEAR_HI, YEAR_BIN = 1960, 2030, 3
FORMATS = ["TV", "TV_SHORT", "MOVIE", "OVA", "ONA", "SPECIAL"]


def l2(v):
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(n, 1e-8)


def from_blob(b, d=None):
    if b is None:
        return None if d is None else np.zeros(d, np.float32)
    return np.frombuffer(b, dtype=np.float16).astype(np.float32)


@dataclass
class Data:
    vid: np.ndarray                       # analysed library video ids
    song_of: np.ndarray                   # video index -> song index
    keys: list                            # song keys
    A: np.ndarray                         # song blocks (n_song, d), centred + normalised
    C: np.ndarray
    V: np.ndarray
    M: np.ndarray
    song_name: list
    song_anime: np.ndarray                # anime id per song (-1 unknown)
    song_clap_raw: np.ndarray             # uncentred CLAP (same space as the describing words)
    song_dsp: list
    mean: dict                            # library means per block (to centre liked files)
    song_meta: list = field(default_factory=list)
    song_artists: list = field(default_factory=list)   # set of lower-case artist names per song
    song_mood: np.ndarray = None          # (n_song, 527) AudioSet probabilities, NaN rows where not measured
    taste: list = field(default_factory=list)   # liked files: dict(path, name, A, C, V, clap, dsp, twin)
    video_rating: dict = field(default_factory=dict)   # video id -> (value, at)
    favs: dict = field(default_factory=dict)            # video id -> added_at
    twin_cands: dict = field(default_factory=dict)      # taste path -> [video ids] (from the app's name matching)


def split_artists(text):
    """'Saori Hayami (as Yachiyo), ryo' -> ['saori hayami', 'ryo'] (same rule as the app's artist links)."""
    out = []
    for a in re.split(r",\s*", text or ""):
        a = re.sub(r"\s*\((?:as|cv|from)[^)]*\)\s*$", "", a, flags=re.I).strip().lower()
        if a:
            out.append(a)
    return out


def _meta_matrix(song_meta, year_w=None):
    """genres / tags (rarity-weighted), year (smoothed bins), format, OP/ED -> L2-normalised rows."""
    n = len(song_meta)
    series_tags = {}
    for m in song_meta:
        if m["anime"] >= 0:
            series_tags[m["anime"]] = set(m["genres"]) | {"t:" + t for t in m["tags"]}
    df = {}
    for s in series_tags.values():
        for t in s:
            df[t] = df.get(t, 0) + 1
    n_series = max(1, len(series_tags))
    vocab = sorted(t for t, c in df.items() if c >= (3 if not t.startswith("t:") else TAG_MIN_SERIES))
    idx = {t: i for i, t in enumerate(vocab)}
    idf = np.array([math.log(n_series / df[t]) for t in vocab], np.float32)
    nb = (YEAR_HI - YEAR_LO) // YEAR_BIN + 1
    G = np.zeros((n, len(vocab)), np.float32)
    Y = np.zeros((n, nb), np.float32)
    F = np.zeros((n, len(FORMATS) + 1), np.float32)
    T = np.zeros((n, 2), np.float32)
    centres = YEAR_LO + YEAR_BIN * (np.arange(nb) + 0.5)
    for i, m in enumerate(song_meta):
        for t in m["genres"]:
            if t in idx:
                G[i, idx[t]] = idf[idx[t]]
        for t in m["tags"]:
            k = "t:" + t
            if k in idx:
                G[i, idx[k]] = 0.7 * idf[idx[k]]
        if m["year"]:
            Y[i] = np.exp(-0.5 * ((centres - m["year"]) / 2.5) ** 2)
        f = m["format"]
        F[i, FORMATS.index(f) if f in FORMATS else len(FORMATS)] = 1.0
        T[i, 0 if m["type"] == "OP" else 1] = 1.0
    # sub-blocks: content tags matter most, then era; format and OP/ED a little
    yw = PARAMS["year_w"] if year_w is None else year_w
    return l2(np.concatenate([l2(G) * 1.0, l2(Y) * yw, F * 0.25, T * 0.25], axis=1)).astype(np.float32)


def load(features_db: sqlite3.Connection, library_db: sqlite3.Connection, model_tag: str, use_favorites=True) -> Data | None:
    fcols = {r[1] for r in features_db.execute("PRAGMA table_info(feat)")}
    mood_col = "mood" if "mood" in fcols else "NULL"
    rows = features_db.execute(f"SELECT kind, key, audio, clap, visual, name, dsp, {mood_col} FROM feat "
                               "WHERE model=? AND audio IS NOT NULL", (model_tag,)).fetchall()
    lib = [r for r in rows if r[0] == "lib"]
    if not lib:
        return None
    meta = {}
    artists = {}
    for vid, aid, typ, num, part, title, year, ayear, fmt, genres, tags, arts in library_db.execute(
            "SELECT v.id, v.anime_id, v.type, v.number, v.part, v.title, v.year, v.anime_year, a.format, "
            "v.anime_genres, v.anime_tags, v.artists FROM v_video v LEFT JOIN anime a ON a.id=v.anime_id WHERE v.missing=0"):
        meta[vid] = (aid, typ, num, part, title, ayear or year, fmt, genres, tags)
        artists[vid] = arts
    lib = [r for r in lib if int(r[1]) in meta]
    vid = np.array([int(r[1]) for r in lib])
    A = np.stack([from_blob(r[2]) for r in lib])
    dc = next((len(from_blob(r[3])) for r in lib if r[3]), 512)
    dv = next((len(from_blob(r[4])) for r in lib if r[4]), 768)
    C = np.stack([from_blob(r[3], dc) for r in lib])
    V = np.stack([from_blob(r[4], dv) for r in lib])
    mean = {"A": A.mean(axis=0), "C": C.mean(axis=0), "V": V.mean(axis=0)}
    Ac, Cc, Vc = l2(A - mean["A"]), l2(C - mean["C"]), l2(V - mean["V"])
    # songs: every version / resolution of one theme (same series, OP/ED, number, part)
    keys, key_idx, song_of = [], {}, np.zeros(len(lib), np.int64)
    for i, v in enumerate(vid):
        aid, typ, num, part = meta[v][:4]
        k = (aid, typ, num, part or "") if aid is not None else ("v", int(v))
        if k not in key_idx:
            key_idx[k] = len(keys)
            keys.append(k)
        song_of[i] = key_idx[k]
    n = len(keys)

    def songmean(X):
        out = np.zeros((n, X.shape[1]), np.float32)
        np.add.at(out, song_of, X)
        return l2(out)

    song_meta, song_name, song_anime, song_dsp = [None] * n, [""] * n, np.full(n, -1), [None] * n
    song_artists = [set() for _ in range(n)]
    for i, v in enumerate(vid):
        s = song_of[i]
        song_artists[s].update(split_artists(artists.get(int(v))))
        if song_meta[s] is None:
            aid, typ, num, part, title, year, fmt, genres, tags = meta[v]
            try:
                g = json.loads(genres or "[]")
                t = json.loads(tags or "[]")
            except ValueError:
                g, t = [], []
            song_meta[s] = {"anime": aid if aid is not None else -1, "genres": g, "tags": t, "year": year,
                            "format": fmt, "type": typ}
            song_name[s] = f"{title} {typ or ''}{num or ''}{part or ''}".strip()
            song_anime[s] = aid if aid is not None else -1
            try:
                song_dsp[s] = json.loads(lib[i][6]) if lib[i][6] else None
            except ValueError:
                pass
    d = Data(vid=vid, song_of=song_of, keys=keys, A=songmean(Ac), C=songmean(Cc), V=songmean(Vc),
             M=_meta_matrix(song_meta), song_name=song_name, song_anime=song_anime,
             song_clap_raw=songmean(l2(C)), song_dsp=song_dsp, mean=mean, song_meta=song_meta, song_artists=song_artists)
    # AudioSet class probabilities per song (mean of its versions; NaN where not measured)
    mood = np.full((n, 527), np.nan, np.float32)
    acc = {}
    for i, r in enumerate(lib):
        if r[7] is not None:
            acc.setdefault(int(song_of[i]), []).append(from_blob(r[7]))
    for sidx, vs in acc.items():
        mood[sidx] = np.mean(vs, axis=0)
    d.song_mood = mood
    for kind, key, a, c, v, name, dsp, md in rows:
        if kind != "taste":
            continue
        a, c, v = from_blob(a), from_blob(c, dc), from_blob(v, dv)
        try:
            dspd = json.loads(dsp) if dsp else None
        except ValueError:
            dspd = None
        d.taste.append({"path": key, "name": name, "mood": from_blob(md) if md is not None else None,
                        "A": l2(a - mean["A"]), "C": l2(c - mean["C"]),
                        "V": l2(v - mean["V"]), "clap": l2(c), "dsp": dspd, "twin": None})
    tables = {r[0] for r in library_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "rating" in tables:
        d.video_rating = {v: (val, at) for v, val, at in library_db.execute("SELECT video_id, value, at FROM rating")}
    if use_favorites:
        d.favs = {v: at or 0 for v, at in library_db.execute("SELECT video_id, added_at FROM favorite")}
    if "taste_match" in tables:
        for path, cands in library_db.execute("SELECT path, cands FROM taste_match"):
            try:
                d.twin_cands[path] = [int(x) for x in json.loads(cands or "[]")]
            except ValueError:
                pass
    return d


def find_twins(d: Data, p=PARAMS):
    """Liked file -> the library song that is the same opening (name candidates confirmed by sound)."""
    vid_index = {int(v): i for i, v in enumerate(d.vid)}
    for t in d.taste:
        t["twin"] = None
        cands = [d.song_of[vid_index[v]] for v in d.twin_cands.get(t["path"], []) if v in vid_index]
        if cands:
            cands = sorted(set(cands))
            sims = d.A[cands] @ t["A"]
            j = int(np.argmax(sims))
            if sims[j] >= p["twin_min"]:
                t["twin"] = int(cands[j])
                t["twin_sim"] = float(sims[j])
                continue
        sims = d.A @ t["A"]                        # no name match: only an (almost) identical recording counts
        j = int(np.argmax(sims))
        if sims[j] >= p["twin_audio_only"]:
            t["twin"] = int(j)
            t["twin_sim"] = float(sims[j])


def song_labels(d: Data):
    """Per song: 2 liked in the app (like / favourite), 1 in a liked folder, -1 disliked, 0 unrated. The newest
    rating of any version wins; otherwise a favourite or a liked-folder twin makes the song liked."""
    n = len(d.keys)
    vid_index = {int(v): i for i, v in enumerate(d.vid)}
    latest = {}
    for v, (val, at) in d.video_rating.items():
        if v in vid_index and val:
            s = int(d.song_of[vid_index[v]])
            if s not in latest or (at or 0) > latest[s][1]:
                latest[s] = (val, at or 0)
    y = np.zeros(n, np.int8)
    for t in d.taste:
        if t["twin"] is not None:
            y[t["twin"]] = 1
    for v in d.favs:
        if v in vid_index:
            y[d.song_of[vid_index[v]]] = 2
    for s, (val, _) in latest.items():
        y[s] = 2 if val > 0 else -1
    return y


def _z(x):
    sd = x.std()
    return (x - x.mean()) / (sd if sd > 1e-9 else 1.0)


def _topk_mean(S, k):
    k = max(1, min(k, S.shape[1]))
    if S.shape[1] == k:
        return S.mean(axis=1)
    return np.partition(S, -k, axis=1)[:, -k:].mean(axis=1)


def train_logreg(X, y, w, lam, iters=80):
    """Weighted L2-regularised logistic regression (LBFGS, CPU). Returns the decision values for X."""
    import torch
    torch.set_num_threads(4)
    Xt = torch.from_numpy(np.ascontiguousarray(X, dtype=np.float32))
    yt = torch.from_numpy(y.astype(np.float32))
    wt = torch.from_numpy((w / w.sum()).astype(np.float32))
    W = torch.zeros(X.shape[1], requires_grad=True)
    b = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([W, b], lr=1.0, max_iter=iters, line_search_fn="strong_wolfe")

    def closure():
        opt.zero_grad()
        z = Xt @ W + b
        loss = (wt * torch.nn.functional.binary_cross_entropy_with_logits(z, yt, reduction="none")).sum() + lam * (W * W).sum()
        loss.backward()
        return loss

    opt.step(closure)
    with torch.no_grad():
        return (Xt @ W + b).numpy(), W.numpy().copy()


def artist_block(d: Data, y):
    """One column per artist of a rated song (like or dislike); a song's artists share 1 between them."""
    names = sorted(set().union(*[d.song_artists[s] for s in np.where(y != 0)[0]])) if d.song_artists else []
    col = {a: i for i, a in enumerate(names)}
    R = np.zeros((len(d.keys), len(names)), np.float32)
    if names:
        for s, arts in enumerate(d.song_artists):
            ks = [col[a] for a in arts if a in col]
            if ks:
                R[s, ks] = 1.0 / math.sqrt(len(arts))
    return R


def features(d: Data, mode, p=PARAMS, y=None):
    blocks = [d.A * p["w_a"], d.C * p["w_c"]]
    if mode == "mix":
        blocks += [d.V * p["w_v"], d.M * p["w_m"]]
        if y is not None and p.get("w_r"):
            blocks.append(artist_block(d, y) * p["w_r"])
    return np.concatenate(blocks, axis=1)


def taste_vector(t, mode, p=PARAMS, M_fill=None):
    blocks = [t["A"] * p["w_a"], t["C"] * p["w_c"]]
    if mode == "mix":
        blocks += [t["V"] * p["w_v"], (M_fill if M_fill is not None else np.zeros(0, np.float32)) * p["w_m"]]
    return np.concatenate(blocks)


def similarity_space(d: Data, mode):
    """For 'sounds like' similarity: sound only, or sound + look + story (each block weighted equally)."""
    if mode == "audio":
        return l2(np.concatenate([d.A, 0.6 * d.C], axis=1))
    return l2(np.concatenate([d.A, 0.6 * d.C, 0.8 * d.V, 0.8 * d.M], axis=1))


def score(d: Data, y, mode, p=PARAMS, extra_pos=None):
    """Raw score per song (higher = more to the user's taste) plus the index of the closest liked item.

    extra_pos: liked files without a library twin, as vectors in the similarity space (similarity only)."""
    pos = np.where(y > 0)[0]
    neg = np.where(y < 0)[0]
    Z = similarity_space(d, mode)
    P = Z[pos] if len(pos) else np.zeros((0, Z.shape[1]), np.float32)
    if extra_pos is not None and len(extra_pos):
        P = np.concatenate([P, extra_pos])
    if not len(P):
        return np.zeros(len(d.keys), np.float32), np.zeros(len(d.keys), np.int64)
    S = Z @ P.T
    knn = _topk_mean(S, p["knn_k"])
    nearest = S.argmax(axis=1)
    trained = len(pos) >= 3
    out = (p["mix_knn"] if trained else 1.0) * _z(knn)
    if len(neg):
        out -= p["mix_neg"] * _z(_topk_mean(Z @ Z[neg].T, 1))
    if trained:
        X = features(d, mode, p, y)
        unl = y == 0
        w = np.zeros(len(y), np.float64)
        w[pos] = np.where(y[pos] >= 2, p["w_explicit"], 1.0)   # what the user rated in the app counts most
        w[unl] = p["unl_ratio"] * len(pos) / max(1, unl.sum())
        w[neg] = p["neg_w"]                      # a dislike counts like a like, in the other direction
        dec, _ = train_logreg(X, (y > 0).astype(np.int8), w, p["lam"])
        out += p["mix_model"] * _z(dec)
    return out.astype(np.float32), nearest


# ----------------------------------------------------------------------------- song types (calm ... intense)
# Each song's type comes from its OWN sound: an AudioSet classifier (trained on human-labelled clips) says how much
# it sounds like exciting / angry music, metal, punk, rock, drums vs tender / sad music, lullaby, piano, ambient.
# Checked on well-known songs: 21 of 23 on the right side (e.g. Death Note OP2 +3.5, One Punch Man OP1 +3.7,
# Clannad ED1 -4.7, Violet Evergarden ED1 -4.6). Songs analysed before the classifier existed get their energy
# predicted from their music embeddings (ridge regression trained on the measured songs).
TYPES = ["Intense", "Energetic", "Mellow", "Calm"]
TYPE_EDGES = (1.5, 0.0, -2.5)              # energy >= 1.5 intense, >= 0 energetic, >= -2.5 mellow, else calm
MOOD_HIGH = ["Exciting music", "Angry music", "Heavy metal", "Punk rock", "Grunge", "Rock music", "Progressive rock",
             "Rock and roll", "Drum kit", "Electric guitar", "Yell", "Scary music"]
MOOD_LOW = ["Tender music", "Sad music", "Lullaby", "Ambient music", "Acoustic guitar", "Piano", "Music for children",
            "Christmas music", "Jingle, tinkle", "Soul music", "New-age music"]


def energy_from_mood(P, labels):
    idx = {n: i for i, n in enumerate(labels)}
    hi = P[:, [idx[n] for n in MOOD_HIGH if n in idx]].sum(axis=1)
    lo = P[:, [idx[n] for n in MOOD_LOW if n in idx]].sum(axis=1)
    return np.log((hi + 1e-3) / (lo + 1e-3))


def _ridge(X, y, lam=1.0):
    Xm, ym = X.mean(axis=0), y.mean()
    Xc = (X - Xm).astype(np.float64)
    w = np.linalg.solve(Xc.T @ Xc + lam * np.eye(X.shape[1]), Xc.T @ (y - ym))
    return lambda Z: (Z - Xm) @ w + ym


def song_types(d: Data, labels):
    """-> (type index per song, type index per liked file without a library copy, info dict); (None, None, info)
    when too few songs have been measured yet."""
    if labels is None or d.song_mood is None:
        return None, None, {"types": "unavailable"}
    meas = ~np.isnan(d.song_mood[:, 0])
    if meas.sum() < 200:
        return None, None, {"types": f"waiting ({int(meas.sum())} songs measured)"}
    e = np.full(len(d.keys), np.nan)
    e[meas] = energy_from_mood(d.song_mood[meas], labels)
    X = np.concatenate([d.A, d.C], axis=1)
    loose = [t for t in d.taste if t["twin"] is None]
    info = {"types_measured": int(meas.sum()), "types_predicted": int((~meas).sum())}
    if (~meas).any() or any(t["mood"] is None for t in loose):
        mi = np.where(meas)[0]
        rng = np.random.default_rng(0)
        test = rng.random(len(mi)) < 0.2                    # check the prediction on measured songs first
        f = _ridge(X[mi[~test]], e[mi[~test]], lam=3.0)
        if test.sum() > 20:
            info["types_prediction_corr"] = round(float(np.corrcoef(f(X[mi[test]]), e[mi[test]])[0, 1]), 3)
        f = _ridge(X[mi], e[mi], lam=3.0)
        ref = np.sort(e[mi])

        def calibrated(Z):                                   # predictions shrink to the middle: match the spread
            p = f(Z)
            ranks = np.searchsorted(np.sort(f(X[mi])), p) / len(mi)
            return ref[np.clip((ranks * (len(ref) - 1)).astype(int), 0, len(ref) - 1)]
        miss = np.where(~meas)[0]
        if len(miss):
            e[miss] = calibrated(X[miss])
    else:
        calibrated = None
    loose_e = []
    for t in loose:
        if t["mood"] is not None:
            loose_e.append(float(energy_from_mood(t["mood"][None, :], labels)[0]))
        elif calibrated is not None:
            loose_e.append(float(calibrated(np.concatenate([t["A"], t["C"]])[None, :])[0]))
        else:
            loose_e.append(0.0)

    def to_type(x):
        x = np.asarray(x, dtype=np.float64)
        return np.select([x >= TYPE_EDGES[0], x >= TYPE_EDGES[1], x >= TYPE_EDGES[2]], [0, 1, 2], 3)
    t_song = to_type(e)
    info["types_share"] = {TYPES[k]: round(float((t_song == k).mean()), 3) for k in range(len(TYPES))}
    return t_song, list(to_type(loose_e)) if loose_e else [], info


def percentile(x):
    order = x.argsort().argsort()
    return 100.0 * order / max(len(x) - 1, 1)
