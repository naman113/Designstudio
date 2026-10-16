"""What the catalogue service does once at its start, in a background thread.

1. Catch-up: when the Postgres copy lags the SQLite catalogue (writes missed while Postgres was down, or a SQLite file
   copied in), `sync.catch_up` replays the differences (a full resync only if that is not enough). One line in
   work/logs/catalogue_sync.log (verb `start-catch-up`), one on the service's own output. The service answers from the first
   second: the thread never holds up start-up, and until the copy is current the pages read as before (svc_queries:
   SQLite, or "search unavailable").
2. Shape signatures still missing: parts without `d2_v1` that have a shape to read (pg/signatures.d2_targets) get one
   `geometry.signature` job, and the answer is written under the mirror's advisory lock. The catch-up itself passes a
   provider that measures nothing (`moments_v1` needs no shape), so the lock is never held while a job runs and this
   process never loads the kernel. A part with no shape to read is left to the back-fill command and counted in the line.

3. The part records' pass: framework/catalog/pg/partcards.start_loop, waiting for 1 and 2, then every 10 minutes.

Never raises; only the first replica runs it (`BC_REPLICA` 1); `BC_PG_OFF` turns it off.
"""
import os
import threading
import time

MAX_TARGETS = 2000                       # geometry.signature's own limit per job


def _say(text):
    print("catalogue start: %s" % text, flush=True)


def _no_shapes(targets):
    return {}


def fill_missing_d2(timeout_s=1050):
    """One `geometry.signature` job per 2000 parts that lack `d2_v1` and have a candidate shape; the answers are written
    with `signatures.backfill(part_ids, d2_provider)` (only rows still missing). -> {"missing", "asked", "written"}."""
    from framework.catalog.pg import db as pgdb
    from framework.catalog.pg import signatures
    from framework.common import jobcall
    con = pgdb.connect(autocommit=True, connect_timeout=2)
    try:
        missing = sorted(signatures._missing(con, "d2_v1"))
        targets = [(p, c) for p, c in signatures.d2_targets(con, missing) if c]
    finally:
        con.close()
    out = {"missing": len(missing), "asked": len(targets), "written": 0}
    for i in range(0, len(targets), MAX_TARGETS):
        chunk = targets[i:i + MAX_TARGETS]
        res = jobcall.call("geometry.signature", {"targets": [[p, [[a, b] for a, b in c]] for p, c in chunk]},
                           sender="catalogue", timeout_s=timeout_s)
        d2 = (jobcall.read_result_file(res) or {}).get("d2_v1") or {}
        con = pgdb.connect(autocommit=True, connect_timeout=2)
        try:
            if not signatures._lock(con, wait_s=120):
                break
            try:
                r = signatures.backfill(con, only_missing=True, part_ids=[p for p, _c in chunk],
                                        d2_provider=lambda _t, d2=d2: d2)
                out["written"] += r["with_geometry"]
            finally:
                con.execute("SELECT pg_advisory_unlock(hashtext('buildcore_sync'))")
        finally:
            con.close()
    return out


def ensure_catalogue_file():
    """A fresh install: make the empty catalogue file (schema applied) when it is missing, so no read finds it absent.
    Idempotent; never raises."""
    try:
        from framework.catalog import ingest
        if not os.path.exists(ingest.DB):
            ingest.connect().close()
            _say("made an empty catalogue file")
    except Exception as exc:
        try:
            _say("could not make the catalogue file: %s: %s" % (type(exc).__name__, str(exc)[:200]))
        except Exception:
            pass


def run_once():
    """The start-up work (see the module doc). -> a dict for the log line. Never raises."""
    from framework.catalog.pg import sync
    t0 = time.perf_counter()
    out = {"current_before": None, "caught_up": None, "current_after": None, "d2": None}
    ensure_catalogue_file()
    try:
        out["current_before"] = sync.is_current(sync._default_sqlite())
        if out["current_before"]:
            sync._log("start-catch-up", [], "ok (already current)", sync._ms(t0))
        else:
            out["caught_up"] = sync.catch_up(d2_provider=_no_shapes, verb="start-catch-up")
        out["current_after"] = sync.is_current(sync._default_sqlite())
    except Exception as exc:                                   # catch_up never raises; is_current never raises
        out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    try:
        if out["current_after"]:
            out["d2"] = fill_missing_d2()
    except Exception as exc:
        out["d2"] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    out["ms"] = int((time.perf_counter() - t0) * 1000)
    return out


def _thread_body():
    try:
        out = run_once()
        if out["current_before"]:
            first = "the shared catalogue was current"
        elif out["caught_up"]:
            first = "the shared catalogue lagged and caught up"
        else:
            first = "the shared catalogue is not current and could not be brought current (see work/logs/catalogue_sync.log)"
        d2 = out.get("d2")
        if isinstance(d2, dict) and "error" not in d2:
            second = "shape signatures: %d missing, %d asked, %d written" % (d2["missing"], d2["asked"], d2["written"])
        elif isinstance(d2, dict):
            second = "shape signatures: not filled (%s)" % d2["error"]
        else:
            second = "shape signatures: not looked at"
        _say("%s; in sync now: %s; %s (%d ms)" % (first, "yes" if out["current_after"] else "no", second, out["ms"]))
    except Exception as exc:                                   # never take the service down
        try:
            _say("failed: %s: %s" % (type(exc).__name__, str(exc)[:200]))
        except Exception:
            pass


def start(replica=1):
    """Make the catalogue file when missing (synchronously, before any request), then start the start-up work in a
    daemon thread (first replica only). -> the thread, or None."""
    ensure_catalogue_file()
    from framework.common.config import pg_off
    if pg_off() or int(replica or 1) != 1:
        return None
    t = threading.Thread(target=_thread_body, name="catalogue-start-catch-up", daemon=True)
    t.start()
    _start_part_records(t, replica)
    return t


def _start_part_records(after, replica):
    """The part records' pass (exact sizes and records of what is missing or stale)
    after the start-up work, then every PARTCARDS_EVERY_S, in its own daemon thread (framework/catalog/pg/partcards.py)."""
    try:
        from framework.catalog.pg import partcards
        partcards.start_loop(after=after, replica=replica)
    except Exception as exc:                                   # never take the service down
        _say("part records not started: %s: %s" % (type(exc).__name__, str(exc)[:200]))
