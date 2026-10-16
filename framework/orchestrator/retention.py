"""Archive retention.

Every acknowledged queue message is archived (`pgmq.a_<queue>`), and the structured job logs pile up one file per process per
day. Once a day the orchestrator prunes both, on its own thread and its own connection (it never blocks dispatching):

    archived queue messages   rows of pgmq.a_<queue> with archived_at older than the cutoff, for the 15 consumed queues,
                              deleted in batches of BATCH rows (one short transaction each, never one huge one)
    structured job logs       work/logs/services/*/<date>-<pid>.jsonl older than the cutoff (framework.common.slog.prune)

KEPT, always: the dead letters (every `<queue>_dead` table, live and archive: failed jobs), the job history (orch.jobs,
orch.job_events), the processed list (bus.processed), everything not older than the cutoff, the security audit records
(work/logs/audit), and every database other than the one this process is connected to.

The number of days is decided in one place: `framework.common.config.retention_days()` = RETENTION_DAYS (7) or the environment
variable BC_RETENTION_DAYS; 0 keeps everything (nothing is deleted, one `retention.pruned` line with days=0 is still written).

    prune(con=None, days=None, batch=BATCH, dry_run=False) -> {"days", "queues": {queue: rows}, "rows", "log_files", ...}
    Retention(): the thread. First run BC_RETENTION_FIRST_S after start (default 300 s), then every BC_RETENTION_EVERY_S
                 (default 86,400 s). A failed run (database hiccup) is logged (`svc.error`) and retried after RETRY_S.
    start_thread() -> the started Retention; health() is what the orchestrator's /health shows under `retention`.
"""
import os
import threading
import time

from framework.common import config, pg, queues, slog

RETENTION_DAYS = config.RETENTION_DAYS        # the number lives in config; see retention_days() for the environment override
BATCH = 1000                                  # rows per DELETE (each its own short transaction)
PAUSE_S = 0.02                                # a breath between two batches, so the database is never held by one long burst
FIRST_RUN_S = 300                             # the first run: a few minutes after the orchestrator starts
EVERY_S = 86400                               # then every 24 h
RETRY_S = 600                                 # after a failed run (never later than the regular interval)
from framework.common.config import PROTECTED_DBS as PROTECTED  # noqa: E402  (one list, kept in config)
STATEMENT_TIMEOUT_MS = 30000


def _log(msg):
    print("%s orchestrator: retention: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def pruned_queues():
    """The queues whose archives are pruned: the consumed ones. A dead-letter queue (`<queue>_dead`) is never touched."""
    return tuple(q for q in queues.ALL if not q.endswith("_dead"))


def prune(con=None, days=None, batch=BATCH, dry_run=False, pause=PAUSE_S):
    """Delete the archived queue messages older than `days` days (None = config.retention_days(); 0 = nothing) and the
    structured log files of the same age. `con` is an autocommit connection (None = a new one, closed after). The cutoff is
    taken once from the database clock. `dry_run` counts and deletes nothing (log files are counted by name date only
    through the delete, so a dry run reports log_files = None). Raises on a database error (the caller logs and retries)."""
    days = config.retention_days() if days is None else int(days)
    out = {"days": days, "queues": {}, "rows": 0, "batches": 0, "max_batch": 0, "log_files": 0, "dry_run": bool(dry_run),
           "ms": 0}
    t0 = time.monotonic()
    if days <= 0:
        return out
    own = con is None
    if own:
        con = pg.connect(write=True)
    try:
        name = pg.dbname()
        if name in PROTECTED:
            raise RuntimeError("refusing: %s is not a v2 database" % name)
        con.execute("SET statement_timeout = %d" % STATEMENT_TIMEOUT_MS)
        cutoff = con.execute("SELECT now() - make_interval(days => %s::integer)", (days,)).fetchone()[0]
        out["cutoff"] = cutoff.isoformat(timespec="seconds")
        for q in pruned_queues():
            table = "pgmq.a_%s" % q               # q comes from queues.ALL (lowercase [a-z0-9_]), never from outside
            if con.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
                continue
            if dry_run:
                n = con.execute("SELECT count(*) FROM %s WHERE archived_at < %%s" % table, (cutoff,)).fetchone()[0]
            else:
                n = 0
                while True:
                    cur = con.execute(
                        "DELETE FROM %s WHERE msg_id IN (SELECT msg_id FROM %s WHERE archived_at < %%s "
                        "ORDER BY archived_at LIMIT %%s)" % (table, table), (cutoff, int(batch)))
                    got = cur.rowcount
                    if got <= 0:
                        break
                    n += got
                    out["batches"] += 1
                    out["max_batch"] = max(out["max_batch"], got)
                    if got < batch:
                        break
                    if pause:
                        time.sleep(pause)
            if n:
                out["queues"][q] = n
                out["rows"] += n
    finally:
        if own:
            con.close()
    if dry_run:
        out["log_files"] = None
    else:
        out["log_files"] = slog.prune(keep_days=days)
    out["ms"] = int((time.monotonic() - t0) * 1000)
    return out


class Retention(threading.Thread):
    """The daily run. Its own daemon thread and its own short connection per run; the dispatcher never waits for it."""

    def __init__(self, first_s=None, every_s=None):
        super().__init__(name="retention", daemon=True)
        self.first_s = _seconds("BC_RETENTION_FIRST_S", FIRST_RUN_S) if first_s is None else first_s
        self.every_s = _seconds("BC_RETENTION_EVERY_S", EVERY_S) if every_s is None else every_s
        self.retry_s = min(RETRY_S, self.every_s)
        self._halt = threading.Event()
        self.last = None             # the result of the last good run
        self.last_run = None         # epoch seconds of the last good run
        self.last_error = ""
        self.runs = 0
        self.failures = 0

    def stop(self):
        self._halt.set()

    def health(self):
        days = config.retention_days()
        last = None
        if self.last is not None:
            last = {"rows": self.last["rows"], "log_files": self.last["log_files"], "queues": dict(self.last["queues"]),
                    "cutoff": self.last.get("cutoff"), "ms": self.last["ms"]}
        return {"keep_days": days, "first_s": self.first_s, "every_s": self.every_s, "runs": self.runs,
                "failures": self.failures, "last_run": (time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.last_run))
                                                        if self.last_run else None), "last": last,
                "last_error": self.last_error}

    def run_once(self):
        """One pass; True when it went well. Never raises (a database hiccup is logged and the next time retries)."""
        try:
            res = prune()
        except Exception as exc:
            self.failures += 1
            self.last_error = type(exc).__name__
            _log("run failed (%s); retrying in %g s" % (type(exc).__name__, self.retry_s))
            slog.log("svc.error", kind="svc", level="error", exc=type(exc).__name__)
            return False
        self.runs += 1
        self.last, self.last_run, self.last_error = res, time.time(), ""
        slog.log("retention.pruned", kind="svc", n=res["rows"], files=res["log_files"], days=res["days"], ms=res["ms"])
        _log("days=%d archive rows deleted=%d (%s) log files deleted=%d in %d ms"
             % (res["days"], res["rows"], ", ".join("%s %d" % kv for kv in sorted(res["queues"].items())) or "none",
                res["log_files"], res["ms"]))
        return True

    def run(self):
        wait = self.first_s
        while not self._halt.wait(wait):
            try:
                ok = self.run_once()
            except Exception:             # the thread never dies
                ok = False
            wait = self.every_s if ok else self.retry_s


def _seconds(var, default):
    raw = (os.environ.get(var) or "").strip()
    try:
        n = float(raw)
        return n if n > 0 else default
    except ValueError:
        return default


def start_thread():
    t = Retention()
    t.start()
    return t
