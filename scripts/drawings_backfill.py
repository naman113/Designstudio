"""Give every design its drawings: the backfill.

    python scripts/drawings_backfill.py --missing [--dry-run] [--max-open 2] [--only id,id] [--limit N]

  --missing   the designs with no drawings set, or whose last try failed, or whose set lacks what a finished job leaves
  --stale     the designs whose set no longer matches them (design or rules)
              (both may be given: the union)
  --all       every design (a current set is made again)
  --dry-run   list the chosen designs (smallest first, with their size and state) and submit nothing
  --exclude   design ids or glob patterns (comma separated, e.g. "bracket-*") that are left out
  --retry     also try again the designs whose last job failed on time or size and whose geometry and rules have not changed
              since (by default they are listed, not resubmitted)
  --max-open  how many drawings.make jobs this command keeps open at once (default 2, at most 2). The orchestrator's own caps are the
              floor of the throttle: at most one overview job and one parts job run at a time (policy.TYPE_CAPS), so a backfill
              never takes more than 2 of the 4 geometry workers and a 3D view job of another design starts at once.

It submits `drawings.make {product_id, reason: "backfill"}` straight to the orchestrator (sender cli), smallest design first (the
bytes of the files its manifest names), skips a design that already has an open drawings.make, and waits for each to end. A
second run with --missing finds nothing to do. Ctrl+C stops submitting and leaves the running jobs alone.

At the end it writes work/drawings/backfill-<YYYY-MM-DD-HHMM>.json (per design: state before and after, sheets by kind, the parts
that could not be drawn with their plain reasons, the bought-in parts, seconds, the job id) and prints the totals.
Refuses the databases named in BC_PROTECTED_DBS.
"""
import argparse
import fnmatch
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.common import config, derived, drawfp, jobs, pg  # noqa: E402

MAX_OPEN_LIMIT = 2                         # never more than 2 open parents (their 5,400 s clock runs while they wait behind the caps)
TIME_OR_SIZE = ("timed_out", "too_long", "too_big", "deadline")      # error codes of a job that ran out of time or room
LOCK = os.path.join(ROOT, "work", "drawings", "backfill.lock")
SKIP = os.path.join(ROOT, "work", "drawings", "backfill-skip.json")   # {pid: {fp, code, at}}: failed on time or size at this fingerprint
from framework.common.config import PROTECTED_DBS as PROTECTED  # noqa: E402  (one list for every guard)
OPEN = ("queued", "dispatched", "running", "stopping")
PRODUCTS = derived.PRODUCTS
STATIC_DRAW = os.path.join(ROOT, "framework", "web", "static", "files", "drawings")


def _get(path, timeout=60):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (config.port_of("gateway"), path), timeout=timeout) as r:
        return json.loads(r.read())


def design_bytes(pid):
    """The total bytes of the files the design's manifest names (the order key: smallest first)."""
    man = os.path.join(PRODUCTS, pid + ".json")
    try:
        with open(man, encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        return 0
    seen, total = set(), os.path.getsize(man)
    for e in m.get("parts", []):
        f = e.get("file")
        if not f or f in seen:
            continue
        seen.add(f)
        p = f if os.path.isabs(f) else os.path.join(os.path.dirname(man), f)
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def set_facts(pid):
    """What the design's current set holds, read from its index.json: sheets by kind, failed parts, bought in, drawable parts."""
    try:
        with open(os.path.join(STATIC_DRAW, pid, "index.json"), encoding="utf-8") as fh:
            idx = json.load(fh)
    except (OSError, ValueError):
        return None
    cur = next((s for s in idx.get("sets", []) if s["set"] == idx.get("current")), None)
    if not cur:
        return None
    try:
        with open(os.path.join(PRODUCTS, pid + ".json"), encoding="utf-8") as fh:
            refs = {p["ref"] for p in json.load(fh).get("parts", []) if p.get("ref")}
    except (OSError, ValueError):
        refs = set()
    bought = set(cur.get("bought_in", []))
    kinds = {}
    for sh in cur.get("sheets", []):
        kinds[sh["kind"]] = kinds.get(sh["kind"], 0) + 1
    drawable = len(refs - bought) if len(refs) > 1 else len(refs)
    return {"set": cur["set"], "made_at": cur.get("made_at"), "sheets": kinds, "failed": cur.get("failed", []), "parts_pending": bool(cur.get("parts_pending")),
            "bought_in": sorted(bought), "parts": len(refs), "drawable": drawable,
            "wrong": sum(int(sh.get("wrong") or 0) for sh in cur.get("sheets", []))}


def incomplete(pid):
    """A set that says `current` but lacks something a finished job would have left: no views sheet, no assembly drawing (and no
    note why) for a design of several parts, or fewer part drawings than drawable parts without every miss listed (a hand-over
    that was lost leaves exactly that)."""
    f = set_facts(pid)
    if f is None:
        return False
    if f["parts_pending"] or not f["sheets"].get("views"):
        return True
    noted = {x.get("ref") for x in f["failed"]}
    if f["parts"] > 1 and not f["sheets"].get("assembly") and not noted & {"*assembly", "ASSEMBLY"}:
        return True
    misses = len([x for x in f["failed"] if x.get("ref") not in ("*assembly", "ASSEMBLY", "*")])
    return "*" not in noted and f["sheets"].get("part", 0) + misses < f["drawable"]


def _pid_alive(pid):
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:                                   # noqa: BLE001
        return False


def take_lock():
    """One backfill at a time: a lock file holding this process's pid; a stale one (its process is gone) is replaced."""
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    try:
        held = open(LOCK, encoding="utf-8").read().strip()
    except OSError:
        held = ""
    if held and held != str(os.getpid()) and _pid_alive(held):
        return held
    with open(LOCK, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    return None


def drop_lock():
    try:
        if open(LOCK, encoding="utf-8").read().strip() == str(os.getpid()):
            os.remove(LOCK)
    except OSError:
        pass


def _skips():
    try:
        with open(SKIP, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _fp(pid):
    try:
        return drawfp.fingerprint(os.path.join(PRODUCTS, pid + ".json"))
    except (OSError, ValueError):
        return None


def state_of(pid):
    try:
        return _get("/api/drawings/%s/status" % pid)
    except Exception:                                   # noqa: BLE001
        return None


def designs():
    prods = _get("/api/products")
    rows = prods if isinstance(prods, list) else (prods or {}).get("products", [])
    out = []
    for p in rows:
        pid = p.get("product_id") or p.get("id")
        if pid and os.path.isfile(os.path.join(PRODUCTS, pid + ".json")):
            out.append(pid)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--missing", action="store_true")
    ap.add_argument("--stale", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-open", type=int, default=2)
    ap.add_argument("--only", default="")
    ap.add_argument("--exclude", default="")
    ap.add_argument("--retry", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    if not (a.missing or a.stale or a.all):
        ap.error("choose --missing, --stale (or both) or --all")
    if pg.dbname() in PROTECTED:
        print("refusing: %s is a protected database (BC_PROTECTED_DBS)" % pg.dbname())
        return 2
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    exclude = [x.strip() for x in a.exclude.split(",") if x.strip()]
    a.max_open = max(1, min(MAX_OPEN_LIMIT, a.max_open))
    if not a.dry_run:
        held = take_lock()
        if held:
            print("another backfill is running (process %s, lock %s); not starting a second one" % (held, os.path.relpath(LOCK, ROOT)))
            return 3
    try:
        return run(a, only, exclude)
    finally:
        if not a.dry_run:
            drop_lock()


def run(a, only, exclude):
    con = pg.connect(write=True)
    chosen, skipped, left_out = [], [], []
    skips = _skips()
    for pid in designs():
        if only and pid not in only:
            continue
        if any(fnmatch.fnmatch(pid, g) for g in exclude):
            left_out.append(pid)
            continue
        st = state_of(pid)
        if st is None:
            print("%s: no answer from the studio, skipped" % pid)
            continue
        open_job = con.execute("SELECT 1 FROM orch.jobs WHERE type = 'drawings.make' AND payload->>'product_id' = %s AND state = ANY(%s) LIMIT 1",
                               (pid, list(OPEN))).fetchone()
        if open_job:
            continue
        want = (a.all or (a.missing and (st["state"] in ("none", "failed") or (st["state"] == "current" and incomplete(pid)))) or (a.stale and st["state"] == "stale"))
        if want and not a.retry and pid in skips and skips[pid].get("fp") == _fp(pid):
            skipped.append((pid, skips[pid].get("code")))          # failed on time or size, nothing changed since: listed, not resubmitted
            continue
        if want:
            chosen.append((design_bytes(pid), pid, st["state"]))
    chosen.sort()
    if a.limit:
        chosen = chosen[:a.limit]
    print("%d designs to do (%s), smallest first" % (len(chosen), "all" if a.all else " and ".join(w for w, on in (("missing", a.missing), ("out of date", a.stale)) if on)))
    for b, pid, state in chosen:
        print("  %-62s %11s bytes  %s" % (pid, "{:,}".format(b), state))
    for pid, code in skipped:
        print("  not tried again: %s (its last job ended %s and nothing it is made from has changed; --retry to force)" % (pid, code))
    if left_out:
        print("  left out (--exclude): %s" % ", ".join(left_out))
    if a.dry_run or not chosen:
        return 0
    free_before = _free_bytes()
    t_all = time.monotonic()
    report, pending, queue = {}, {}, list(chosen)
    interrupted = False
    try:
        while queue or pending:
            while queue and len(pending) < max(1, a.max_open) and not interrupted:
                _b, pid, state = queue.pop(0)
                jid = jobs.submit(con, "drawings.make", {"product_id": pid, "reason": "backfill"}, sender="cli")
                pending[str(jid)] = {"pid": pid, "before": state, "t0": time.monotonic()}
            time.sleep(1.0)
            for jid in list(pending):
                row = con.execute("SELECT state, error_code FROM orch.jobs WHERE job_id = %s", (jid,)).fetchone()
                if row and row[0] in jobs.FINAL:
                    info = pending.pop(jid)
                    secs = time.monotonic() - info["t0"]
                    facts = set_facts(info["pid"])
                    after = state_of(info["pid"]) or {}
                    if row[0] != "succeeded" and row[1] in TIME_OR_SIZE:
                        skips[info["pid"]] = {"fp": _fp(info["pid"]), "code": row[1], "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                        with open(SKIP, "w", encoding="utf-8") as fh:
                            json.dump(skips, fh, indent=1)
                    report[info["pid"]] = {"job_id": jid, "job_state": row[0], "error_code": row[1], "state_before": info["before"],
                                           "state_after": after.get("state"), "seconds": round(secs, 1), "set": facts}
                    if facts:
                        failed = ["%s (%s)" % (f.get("ref"), f.get("why")) for f in facts["failed"]]
                        print("%s: %s, views %d, assembly %d, parts %d of %d%s, %.0f s" % (
                            info["pid"], row[0], facts["sheets"].get("views", 0), facts["sheets"].get("assembly", 0),
                            facts["sheets"].get("part", 0), facts["drawable"], ", could not be drawn: " + ", ".join(failed) if failed else "", secs))
                    else:
                        print("%s: %s (no set), %.0f s" % (info["pid"], row[0], secs))
            if not pending and not queue:
                break
    except KeyboardInterrupt:
        interrupted = True
        print("interrupted: nothing more is submitted; %d jobs keep running" % len(pending))
    wall = time.monotonic() - t_all
    tot = {"designs": len(report), "views": 0, "assembly": 0, "parts_drawn": 0, "drawable": 0, "failed_parts": 0, "bought_in": 0, "wrong": 0}
    for r in report.values():
        f = r["set"]
        if f:
            tot["views"] += f["sheets"].get("views", 0)
            tot["assembly"] += f["sheets"].get("assembly", 0)
            tot["parts_drawn"] += f["sheets"].get("part", 0)
            tot["drawable"] += f["drawable"]
            tot["failed_parts"] += len([x for x in f["failed"] if x.get("ref") not in ("*assembly", "ASSEMBLY")])
            tot["bought_in"] += len(f["bought_in"])
            tot["wrong"] += f["wrong"]
    out = os.path.join(ROOT, "work", "drawings", "backfill-%s.json" % time.strftime("%Y-%m-%d-%H%M"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"totals": dict(tot, wall_s=round(wall, 1)), "free_bytes_before": free_before, "free_bytes_after": _free_bytes(),
                   "not_tried_again": [{"pid": p, "code": c} for p, c in skipped], "left_out": left_out, "designs": report}, fh, indent=1)
    print("totals: %s; wall time %.0f s (%.1f min)" % (tot, wall, wall / 60))
    print("report written to %s" % os.path.relpath(out, ROOT))
    con.close()
    return 0 if not interrupted else 130


def _free_bytes():
    import shutil
    return shutil.disk_usage(ROOT).free


if __name__ == "__main__":
    sys.exit(main())
