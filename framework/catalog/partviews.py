"""Make every part's own 3D view ahead, so the first look at any part on the catalogue page is a
cached file. Optional: without it a part's view is made the first time someone opens the part (median 0.02 s to mesh;
a part of the largest STEP file waits 9.4 s once for the file to be read).

    C:\\venvs\\buildcore\\Scripts\\python.exe -m framework.catalog.partviews --warm [--step] [--parts ID,ID] [--jobs N]

It reads the catalogue (Postgres, the database in `.env.buildcore`) for every part's shape sources (signatures.shape_sources)
and, for each part whose view is not made yet with today's rule, asks the geometry service (`geometry.part_view`, through
the orchestrator: the services must run) -- N jobs at a time (default 2: the geometry service has 4 workers and the studio
keeps 2). `--step` makes each part's STEP download too. Idempotent: a second run asks nothing. Prints the counts and the
seconds; a part with no kept shape is named.
"""
import argparse
import concurrent.futures as cf
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _todo(kinds, only=None):
    from framework.catalog import trash
    from framework.catalog.pg import db as pgdb
    from framework.catalog import query
    from framework.common import partview as cpv
    con = pgdb.connect(autocommit=True, connect_timeout=5)
    try:
        hidden = set(trash.trashed("mechanical"))
        ids = [r[0] for r in con.execute("SELECT part_id FROM part_revision ORDER BY part_id").fetchall()]
        if only:
            ids = [p for p in ids if p in only]
        work, none = [], []
        for pid in ids:
            s = query.part_shape(con, pid, hidden)
            if s is None:
                continue                                  # used only by designs in the trash
            if not s["sources"]:
                none.append((pid, s["label"]))
                continue
            for kind in kinds:
                key = cpv.key(kind, s["sources"])
                ext = "json" if kind == "view" else "step"
                if not cpv.fresh(pid, key, ext):
                    work.append((kind, pid, s["label"], s["sources"], key))
        return ids, work, none
    finally:
        con.close()


def _one(item):
    from framework.common import jobcall
    kind, pid, label, sources, key = item
    payload = {"part_id": pid, "sources": sources, "key": key}
    if kind == "step":
        payload["name"] = (label or "part")[:200]
    t0 = time.perf_counter()
    try:
        jobcall.call("geometry.part_view" if kind == "view" else "geometry.part_step", payload, sender="catalogue",
                     timeout_s=300)
        return kind, pid, None, time.perf_counter() - t0
    except jobcall.JobError as exc:
        return kind, pid, getattr(exc, "code", None) or getattr(exc, "user_text", None) or str(exc), time.perf_counter() - t0


def warm(kinds=("view",), only=None, jobs=2, log=print):
    t0 = time.perf_counter()
    ids, work, none = _todo(kinds, only)
    log("parts %d; to make: %d (%s); no shape kept: %d" % (len(ids), len(work), ", ".join(kinds), len(none)))
    for pid, label in none:
        log("  no shape kept: %s (%s)" % (label, pid))
    made, failed, slow = 0, [], []
    with cf.ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        for kind, pid, err, sec in ex.map(_one, work):
            if err:
                failed.append((kind, pid, err))
            else:
                made += 1
                slow.append((sec, kind, pid))
    for kind, pid, err in failed:
        log("  not made: %s %s (%s)" % (kind, pid, err))
    slow.sort(reverse=True)
    if slow:
        log("slowest: " + "; ".join("%s %s %.1f s" % (k, p, s) for s, k, p in slow[:5]))
    log("made %d, failed %d, in %.1f s" % (made, len(failed), time.perf_counter() - t0))
    return {"parts": len(ids), "asked": len(work), "made": made, "failed": len(failed), "no_shape": len(none)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--warm", action="store_true", required=True)
    ap.add_argument("--step", action="store_true", help="also make each part's STEP download")
    ap.add_argument("--parts", default=None, help="only these part ids (comma separated)")
    ap.add_argument("--jobs", type=int, default=2)
    a = ap.parse_args(argv)
    only = set(a.parts.split(",")) if a.parts else None
    r = warm(("view", "step") if a.step else ("view",), only, a.jobs)
    return 1 if r["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
