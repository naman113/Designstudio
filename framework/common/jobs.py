"""The job client: submit a job, read it, read its events, wait for it to finish.

Used by the gateway (`/api/jobs/*`) and by the checks. A submit is one message on `orch_in`; the orchestrator is the only
writer of `orch.jobs` and `orch.job_events`, and tells everyone through NOTIFY `job_events` when an event is committed.
"""
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row

from framework.common import config, envelope, pgmq, queues, slog

FINAL = ("succeeded", "failed", "timed_out", "stopped")

JOB_COLUMNS = ("job_id, type, target, state, step, attempt, error_code, user_text, created_at, dispatched_at, "
               "started_at, finished_at, read_ct")
EVENT_COLUMNS = ("event_id, job_id, seq, type, state, service, step, attempt, read_ct, error_code, user_text, meta, "
                 "created_at, tenant_id, account_id")


_CORR = {}                    # job id -> correlation id of the jobs this process submitted (for its answer and stop lines)


def _env_uuid(name):
    """An id from the environment (BC_CORRELATION_ID, set by a caller that groups its jobs), or None."""
    v = os.environ.get(name) or ""
    try:
        return str(uuid.UUID(v)) if v else None
    except ValueError:
        return None


def submit(con, type, payload=None, *, sender, job_id=None, correlation_id=None, deadline=None, parent_job_id=None):
    """Send the job to the orchestrator. Returns the job id (the caller may pass its own). A caller that gives no
    correlation_id gets the environment's (BC_CORRELATION_ID), so everything one request causes is one
    trail; a fresh one is made when there is none. The PARENT is never taken from the environment here: a caller that wants
    one names it (cliclient, jobcall and add_step pass BC_PARENT_JOB_ID themselves)."""
    job_id = job_id or str(uuid.uuid4())
    correlation_id = correlation_id or _env_uuid("BC_CORRELATION_ID")
    env = envelope.new("command", type, payload or {}, sender=sender, job_id=job_id, parent_job_id=parent_job_id,
                       correlation_id=correlation_id, deadline=deadline)
    envelope.validate(env)
    pgmq.send(con, queues.ORCH_IN, env, 0)
    if len(_CORR) > 512:
        _CORR.clear()
    _CORR[job_id] = env["correlation_id"]
    slog.log("client.submit", job_id=job_id, parent_job_id=parent_job_id, correlation_id=env["correlation_id"], type=type)
    return job_id


def stop(con, job_id, *, sender):
    """Ask the orchestrator to stop the job: a queued or unread job stops at once, a running one through
    the service's control queue. Idempotent: a job already in a final state is left alone."""
    env = envelope.new("control", "job.stop", {}, sender=sender, job_id=str(job_id))
    envelope.validate(env)
    pgmq.send(con, queues.ORCH_IN, env, 0)
    slog.log("client.stop", job_id=str(job_id), correlation_id=_CORR.get(str(job_id)))


def _result_of(row):
    """The result exactly as the service made it: parsed from its JSON text (kept in the row, or in the file `result_ref`
    names under work/results/). Never read from a jsonb column, which would change the numbers."""
    text, ref = row.pop("result_text", None), row.get("result_ref")
    if text is None and ref:
        path = os.path.normpath(os.path.join(config.ROOT, ref))
        folder = os.path.normpath(os.path.join(config.ROOT, "work", "results"))
        if os.path.dirname(path) == folder and os.path.isfile(path):
            with open(path, encoding="utf-8", newline="") as fh:
                text = fh.read()
    return None if text is None else envelope.unpack(text)


def get(con, job_id, *, with_result=False):
    """The job's public columns as a dict, or None. `with_result` adds `result` (the submitter's view)."""
    cols = JOB_COLUMNS + (", result_text, result_ref" if with_result else "")
    row = con.cursor(row_factory=dict_row).execute(
        "SELECT %s FROM orch.jobs WHERE job_id = %%s" % cols, (job_id,)).fetchone()
    if row is not None and with_result:
        row["result"] = _result_of(row)
    return row


def events(con, job_id, after_seq=0):
    """The job's events in order, after `after_seq`."""
    return con.cursor(row_factory=dict_row).execute(
        "SELECT %s FROM orch.job_events WHERE job_id = %%s AND seq > %%s ORDER BY seq" % EVENT_COLUMNS,
        (job_id, after_seq)).fetchall()


def wait(listen_con, job_id, timeout_s=30.0):
    """Block until the job reaches a final state (woken by NOTIFY job_events, re-checked at least every second).
    Returns the job (get()); raises TimeoutError."""
    job_id = str(job_id)
    listen_con.execute("LISTEN job_events")
    t0 = time.monotonic()
    end = t0 + timeout_s
    while True:
        job = get(listen_con, job_id)
        if job is not None and job["state"] in FINAL:
            return _answered(listen_con, job, t0)
        left = end - time.monotonic()
        if left <= 0:
            raise TimeoutError("job %s did not finish within %s s" % (job_id, timeout_s))
        hit = False
        for n in listen_con.notifies(timeout=min(left, 1.0), stop_after=1):
            try:
                body = json.loads(n.payload)
            except ValueError:
                continue
            hit = body.get("j") == job_id and body.get("st") in FINAL
        # the query runs after the loop: notifies() holds the connection's lock until the loop has ended
        if hit:
            job = get(listen_con, job_id)
            if job is not None:
                return _answered(listen_con, job, t0)


def _answered(con, job, t0):
    """The line of a client that saw its job end (state and the time it waited); returns the job. A job another
    process submitted has its correlation id read from its row (one small query)."""
    jid = str(job["job_id"])
    corr = _CORR.get(jid)
    if corr is None:
        try:
            row = con.execute("SELECT correlation_id FROM orch.jobs WHERE job_id = %s", (jid,)).fetchone()
            corr = str(row[0]) if row else None
        except Exception:
            corr = None
    slog.log("client.answer", job_id=jid, correlation_id=corr, state=job["state"],
             ms=round((time.monotonic() - t0) * 1000))
    return job


TREE_MAX = 500


def tree(con, job_id):
    """The job and every job it caused (its recipe's steps, a request's catalogue / geometry / engines jobs, a
    follow-on), oldest first, each with its `depth` below the job asked for (0 = that job). The job's ancestors are not
    included. At most TREE_MAX rows, down to 10 levels. Columns: job_id, parent_job_id, correlation_id, type, target, state,
    created_at, finished_at, depth. An unknown job gives []."""
    return con.cursor(row_factory=dict_row).execute(
        "WITH RECURSIVE t AS ("
        " SELECT job_id, parent_job_id, correlation_id, type, target, state, created_at, finished_at, 0 AS depth"
        " FROM orch.jobs WHERE job_id = %s"
        " UNION ALL"
        " SELECT j.job_id, j.parent_job_id, j.correlation_id, j.type, j.target, j.state, j.created_at, j.finished_at,"
        " t.depth + 1 FROM orch.jobs j JOIN t ON j.parent_job_id = t.job_id WHERE t.depth < 10)"
        " SELECT * FROM t ORDER BY created_at, job_id LIMIT %s", (str(job_id), TREE_MAX)).fetchall()


def trail(con, job_id):
    """(tree rows, structured-log lines) of a job: the lines of every service about the job and the jobs it caused,
    and every line that carries their correlation ids, in time order. The window runs from 5 s before the job to 30 s after
    the last job ended (to now while one is open). ([], []) for an unknown job. A caller that holds a pooled connection reads
    the tree with it, gives it back, and only then calls `trail_lines(rows)` (reading the log files takes a while)."""
    rows = tree(con, job_id)
    return rows, trail_lines(rows)


def trail_lines(rows):
    """The log lines of the jobs in `rows` (the result of `tree`): no database access, only the log files, and only the
    files modified since the first job began."""
    if not rows:
        return []
    ids = {str(r["job_id"]) for r in rows}
    corrs = {str(r["correlation_id"]) for r in rows}
    since = rows[0]["created_at"] - timedelta(seconds=5)
    ends = [r["finished_at"] for r in rows if r["finished_at"] is not None]
    open_ = any(r["finished_at"] is None for r in rows)
    until = datetime.now(timezone.utc) if (open_ or not ends) else max(ends) + timedelta(seconds=30)
    return slog.trail(ids, corrs, since, until, modified_since=since)
