"""Console tools for Kanade (run from source).

    python cli.py sync-catalog            download the AnimeThemes catalogue
    python cli.py sample [N]              identify N random archive files and print a table (no library changes)
    python cli.py scan                    full incremental scan + matching (console progress)
    python cli.py stats                   library statistics
    python cli.py review                  list Needs Review items
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
import time

from kanade import catalog, config, db, matcher, scanner, search
from kanade.parser import VIDEO_EXTS


def progress(done, total, msg):
    print(f"\r  {done}/{total or '?'}  {msg[:90]:<90}", end="", flush=True)


def cmd_sync():
    n = catalog.sync(progress)
    print(f"\nCatalogue synced: {n} anime, {db.q1('SELECT COUNT(*) n FROM cat_video')['n']} videos")


def cmd_sample(n=20, seed=None):
    root = config.settings().get("archive_root")
    files = [os.path.join(dp, f) for dp, _, fs in os.walk(root) for f in fs
             if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
    rnd = random.Random(seed)
    picks = rnd.sample(files, n)
    # work in a throw-away DB that shares the catalogue
    main = db.db_path()
    tmpdir = tempfile.mkdtemp(prefix="kanade-sample-")
    db.set_db_path(__import__("pathlib").Path(tmpdir) / "sample.db")
    db.init()
    conn = db.connect()
    conn.execute("ATTACH DATABASE ? AS src", (str(main),))
    conn.execute("INSERT INTO cat_anime SELECT * FROM src.cat_anime")
    conn.execute("INSERT INTO cat_video SELECT * FROM src.cat_video")
    conn.execute("INSERT INTO meta SELECT * FROM src.meta WHERE key='catalog_synced_at'")
    conn.commit()
    conn.execute("DETACH DATABASE src")
    with db.tx() as c:
        for p in picks:
            st = os.stat(p)
            vals = scanner._row_values(p, root, st.st_size, st.st_mtime, None)
            c.execute("""INSERT INTO video(path,rel_path,filename,folder,folder_year,folder_season,size,mtime,
                         p_prefix,p_title,p_type,p_number,p_part,p_version,p_tags,date_added,status)
                         VALUES(:path,:rel_path,:filename,:folder,:folder_year,:folder_season,:size,:mtime,
                         :p_prefix,:p_title,:p_type,:p_number,:p_part,:p_version,:p_tags,0,'pending')""", vals)
    t0 = time.time()
    matcher.match_pending()
    rows = db.q("SELECT rel_path, title, title_english, type, number, version, song_title, artists, match_source, "
                "confidence, status FROM v_video ORDER BY rel_path")
    print("| # | File | Matched title (romaji) | English | Theme | Song - Artist | Source | Conf |")
    print("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        theme = f"{r['type']}{r['number']}" + (f"v{r['version']}" if (r['version'] or 1) > 1 else "")
        song = f"{r['song_title'] or ''} - {r['artists'] or ''}" if r["song_title"] else ""
        print(f"| {i} | `{r['rel_path']}` | {r['title']} | {r['title_english'] or ''} | {theme} | {song} | "
              f"{r['match_source'] or '-'} | {r['confidence'] or 0:.2f}{'' if r['status'] == 'matched' else ' (review)'} |")
    print(f"\nmatched in {time.time() - t0:.1f}s")


def cmd_scan():
    from kanade import jobs
    jm = jobs.manager()
    jm.submit("scan")
    time.sleep(1)
    while True:
        st = jm.status()
        p, t = st["pipeline"], st["thumbs"]
        print(f"\r[{p['phase'] or p['state']}] {p['done']}/{p['total'] or '?'} {p['current'][:50]:<50} "
              f"| thumbs {t['done']}/{t['total']}", end="", flush=True)
        if p["state"] != "running" and jm._q.empty():
            break
        time.sleep(1)
    print("\n", jm.status()["last_scan"])
    cmd_stats()


def cmd_stats():
    from kanade import library
    print(library.counts())
    for r in db.q("SELECT match_source, status, COUNT(*) n, ROUND(AVG(confidence),3) c FROM video "
                  "GROUP BY 1,2 ORDER BY n DESC"):
        print(dict(r))


def cmd_review():
    for r in db.q("SELECT rel_path, match_source, confidence, suggestion FROM video WHERE status='review' "
                  "AND missing=0 ORDER BY rel_path"):
        print(r["rel_path"], r["match_source"], r["confidence"], (r["suggestion"] or "")[:160])


if __name__ == "__main__":
    config.setup_logging(console=False)
    db.init()
    args = sys.argv[1:] or ["stats"]
    cmd = args[0]
    if cmd == "sync-catalog":
        cmd_sync()
    elif cmd == "sample":
        cmd_sample(int(args[1]) if len(args) > 1 else 20, int(args[2]) if len(args) > 2 else None)
    elif cmd == "scan":
        cmd_scan()
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "review":
        cmd_review()
    elif cmd == "reindex":
        search.reindex(None)
    else:
        print(__doc__)
