"""The Activity page's server side: a hub and the /api/activity routes of the gateway.

    GET /activity                       the page (static/activity.html)
    GET /api/activity/snapshot          the newest jobs with their steps, the queue depths, the jobs moved aside
    GET /api/activity/job/{id}          one job with its steps
    GET /api/activity/stream            live steps as server-sent events: `id: <event_id>`, resume by Last-Event-ID or ?after=
    GET /api/activity/dead              the jobs moved aside
    GET /api/activity/trail/{id}        a job's log trail: the job and the jobs it caused, and the lines of every service about
                                        them in time order (framework/common/slog.py), whitelisted fields only

One LISTEN connection per gateway process (the `Hub`) wakes on every `job_events` notification, reads the new rows into an
in-memory ring and, every 2 s, the depth of every queue. Any number of open streams read the ring; none holds a database
connection. Event ids are in commit order (the orchestrator has one writer thread), so "everything after id N" never misses
or repeats a step.

Nothing here selects a job's payload, result or message content, or the payload of a moved-aside message.
The gateway never imports orchestrator code, so the meta key list below is a copy.
"""
import asyncio
import collections
import json
import os
import threading
import time
import uuid
from datetime import datetime

import psycopg
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from psycopg import sql

from framework.common import config, jobs as bus_jobs, pg, pgmq, queues, slog
from framework.common.pgpool import Pool

router = APIRouter()
POOL = Pool(2)
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

RING = 5000
PREFILL = 1000
QUEUES_EVERY_S = 2.0
KEEPALIVE_S = 15.0
POLL_S = 0.1
NOT_KNOWN = "This job is not known."
UNAVAILABLE = "The activity view is not available right now."

# keep equal to framework/orchestrator/events.py META_KEYS
_META_KEYS = {
    "job.queued": ("type", "target", "by", "parent"),
    "job.dispatched": ("queue", "pgmq_id", "deadline"),
    "job.started": ("read_ct", "pid"),
    "job.progressed": ("step", "done", "total", "eta_s"),
    "job.succeeded": ("ms",),
    "job.failed": ("ms",),
    "job.retrying": ("delay_s", "next_attempt"),
    "job.redelivered": ("read_ct",),
    "message.dead_lettered": ("queue", "dead_pgmq_id", "reads"),
    "job.timed_out": ("limit_s",),
    "job.stopping": (),
    "job.stopped": ("stop_ms", "forced"),
    "job.adopted": ("was",),
    "job.text": (),
    "job.step_dispatched": ("step", "child"),     # a recipe parent's steps
    "job.step_finished": ("step", "child", "state"),
    "job.position": ("pos",),
}

EVENT_SELECT = ("SELECT e.event_id, e.job_id, e.seq, e.type, e.state, e.service, e.step, e.attempt, e.read_ct, "
                "e.error_code, e.user_text, e.meta, e.created_at, j.type "
                "FROM orch.job_events e JOIN orch.jobs j USING (job_id) ")
# page events (type turn.*) belong to the page, not to Activity: never listed here
NOT_TURN = "e.type NOT LIKE 'turn.%%'"
IS_TURN = "turn."

JOB_SELECT = ("SELECT job_id, type, target, state, step, attempt, read_ct, error_code, user_text, created_at, "
              "finished_at, round((extract(epoch FROM finished_at - created_at) * 1000)::numeric, 1)::float8, parent_job_id "
              "FROM orch.jobs ")


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def event_view(row):
    (event_id, job_id, seq, etype, state, service, step, attempt, read_ct, error_code, user_text, meta, created_at,
     job_type) = row
    keep = _META_KEYS.get(etype, ())
    return {"event_id": event_id, "job_id": str(job_id), "seq": seq, "type": etype, "state": state, "service": service,
            "step": step, "attempt": attempt, "read_ct": read_ct, "error_code": error_code, "user_text": user_text,
            "meta": {k: v for k, v in (meta or {}).items() if k in keep}, "at": _iso(created_at), "job_type": job_type}


def job_view(row):
    (job_id, jtype, target, state, step, attempt, read_ct, error_code, user_text, created_at, finished_at, ms,
     parent_job_id) = row
    return {"job_id": str(job_id), "short": str(job_id)[:8], "type": jtype, "target": target, "state": state,
            "parent_job_id": str(parent_job_id) if parent_job_id else None,
            "step": step, "attempt": attempt, "read_ct": read_ct, "error_code": error_code, "user_text": user_text,
            "created_at": _iso(created_at), "finished_at": _iso(finished_at), "ms": ms}


def queue_view(m):
    name = m["queue_name"]
    oldest = m.get("oldest_msg_age_sec")
    return {"queue": name, "waiting": m["queue_length"], "visible": m["queue_visible_length"],
            "total": m["total_messages"], "oldest_s": oldest, "dead": name.endswith("_dead")}


class Hub:
    """One LISTEN connection, a ring of recent steps, the queue depths. Started by its first user."""

    def __init__(self):
        self.ring = collections.deque(maxlen=RING)
        self.lock = threading.Lock()
        self.last_id = 0
        self.queues = []
        self.queues_at = 0.0
        self.scaling = []                  # the newest 30 scaling decisions
        self.scaling_last = 0
        self.started = threading.Event()
        self.reconnects = 0
        self.last_error = ""
        self._thread = None
        self._start_lock = threading.Lock()

    # -- start -----------------------------------------------------------------------------------------------------
    def ensure(self):
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="activity-hub", daemon=True)
                self._thread.start()
        self.started.wait(5.0)

    # -- the hub thread --------------------------------------------------------------------------------------------
    def _connect(self):
        con = pg.connect()
        con.execute("LISTEN job_events")
        return con

    def _prefill(self, con):
        last = con.execute("SELECT coalesce(max(event_id), 0) FROM orch.job_events").fetchone()[0]
        rows = con.execute(EVENT_SELECT + "WHERE " + NOT_TURN + " AND e.event_id <= %s ORDER BY e.event_id DESC LIMIT %s",
                           (last, PREFILL)).fetchall()
        rows.reverse()
        with self.lock:
            self.ring.clear()
            for r in rows:
                self.ring.append(event_view(r))
            self.last_id = last                    # past the turn events too: nothing before it is read again

    def _fetch(self, con):
        while True:
            rows = con.execute(EVENT_SELECT + "WHERE e.event_id > %s ORDER BY e.event_id LIMIT 1000",
                               (self.last_id,)).fetchall()
            if not rows:
                return
            with self.lock:
                for r in rows:
                    if not r[3].startswith(IS_TURN):       # advance over every row, keep only Activity's
                        self.ring.append(event_view(r))
                self.last_id = rows[-1][0]
            if len(rows) < 1000:
                return

    def _refresh_queues(self, con):
        wanted = set(queues.ALL)
        views = [queue_view(m) for m in pgmq.metrics_all(con) if m["queue_name"] in wanted]
        views.sort(key=lambda v: v["queue"])
        self.queues = views
        self.queues_at = time.monotonic()
        self._refresh_scaling(con)

    def _refresh_scaling(self, con):
        try:
            rows = con.execute(
                "SELECT event_id, service, action, copies_before, copies_after, replica, reason, created_at "
                "FROM orch.scale_events WHERE base_port = %s ORDER BY event_id DESC LIMIT 30",
                (config.base_port(),)).fetchall()
        except psycopg.errors.UndefinedTable:
            self.scaling, self.scaling_last = [], 0
            return
        self.scaling = [{"event_id": r[0], "service": r[1], "label": config.LABELS.get(r[1], r[1]), "action": r[2],
                         "before": r[3], "after": r[4], "replica": r[5], "reason": r[6], "at": _iso(r[7])}
                        for r in rows]
        self.scaling_last = rows[0][0] if rows else 0

    def _run(self):
        con = None
        first = True
        while True:
            try:
                if con is None:
                    con = self._connect()                # LISTEN first, then read: nothing falls in between
                    if first:
                        self._prefill(con)
                        first = False
                    else:
                        self.reconnects += 1
                    self._fetch(con)
                    self._refresh_queues(con)
                    self.started.set()
                for _ in con.notifies(timeout=1.0, stop_after=1):
                    break                                # wake on the first NOTIFY (or after 1 s); no query in this loop
                for _ in con.notifies(timeout=0):
                    pass                                 # take what else is waiting: the read below covers it all
                self._fetch(con)
                if time.monotonic() - self.queues_at >= QUEUES_EVERY_S:
                    self._refresh_queues(con)
            except psycopg.Error as exc:
                self.last_error = type(exc).__name__
                try:
                    if con is not None:
                        con.close()
                except Exception:
                    pass
                con = None
                self.started.set()
                time.sleep(1.0)
            except Exception as exc:                      # never let the hub thread die
                self.last_error = type(exc).__name__
                time.sleep(1.0)

    # -- readers ---------------------------------------------------------------------------------------------------
    def ring_since(self, after_id):
        """The steps after `after_id` from the ring, or None when the ring does not reach back that far."""
        with self.lock:
            if not self.ring:
                return [] if self.started.is_set() and self.last_id == 0 else None
            if self.ring[-1]["event_id"] <= after_id:
                return []
            if after_id < self.ring[0]["event_id"] - 1:
                return None
            out = []
            for ev in reversed(self.ring):
                if ev["event_id"] <= after_id:
                    break
                out.append(ev)
            out.reverse()
            return out

    def since(self, after_id, limit=2000):
        got = self.ring_since(after_id)
        if got is not None:
            return got[:limit]
        with POOL.conn() as con:
            rows = con.execute(EVENT_SELECT + "WHERE e.event_id > %s AND " + NOT_TURN + " ORDER BY e.event_id LIMIT %s",
                               (after_id, limit)).fetchall()
        return [event_view(r) for r in rows]


HUB = Hub()


def _dead_list(con, limit=50):
    out = []
    for dq in queues.DEAD:
        rows = con.execute(
            sql.SQL("SELECT msg_id, enqueued_at, message->>'dead_at', message->'envelope'->>'job_id', "
                    "message->'envelope'->>'type', message->>'read_ct', message->'error'->>'code' "
                    "FROM pgmq.{} ORDER BY msg_id DESC LIMIT %s").format(sql.Identifier("q_" + dq)),
            (limit,)).fetchall()
        for msg_id, enq, dead_at, job_id, jtype, read_ct, code in rows:
            out.append({"queue": dq[:-len("_dead")], "dead_pgmq_id": msg_id, "dead_at": dead_at or _iso(enq),
                        "job_id": job_id, "type": jtype, "read_ct": int(read_ct) if read_ct and read_ct.isdigit() else None,
                        "error_code": code})
    out.sort(key=lambda d: d["dead_at"] or "", reverse=True)
    return out[:limit]


def _limit(value, default, top):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(top, n))


def _sse(event, data, event_id=None):
    head = "id: %d\n" % event_id if event_id is not None else ""
    return "%sevent: %s\ndata: %s\n\n" % (head, event, json.dumps(data, separators=(",", ":")))


# ---- routes ---------------------------------------------------------------------------------------------------------
@router.get("/activity")
def page():
    return FileResponse(os.path.join(STATIC, "activity.html"), headers={"Cache-Control": "no-store"})


@router.get("/api/activity/snapshot")
def snapshot(limit: int = 50):
    limit = _limit(limit, 50, 200)
    HUB.ensure()
    try:
        with POOL.conn() as con:
            with con.transaction():
                con.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                job_rows = con.execute(JOB_SELECT + "ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
                # the steps of every recipe listed come too, even when they fall outside `limit`
                kid_rows = (con.execute(JOB_SELECT + "WHERE parent_job_id = ANY(%s) AND NOT job_id = ANY(%s) ORDER BY created_at",
                                        ([r[0] for r in job_rows], [r[0] for r in job_rows])).fetchall() if job_rows else [])
                job_rows = list(job_rows) + list(kid_rows)
                ids = [r[0] for r in job_rows]
                ev_rows = (con.execute(EVENT_SELECT + "WHERE e.job_id = ANY(%s) AND " + NOT_TURN + " ORDER BY e.event_id",
                                       (ids,)).fetchall() if ids else [])
                last = con.execute("SELECT coalesce(max(event_id), 0) FROM orch.job_events").fetchone()[0]
                dead = _dead_list(con)
    except psycopg.Error:
        return JSONResponse({"error": UNAVAILABLE}, status_code=503)
    return {"last_event_id": last, "jobs": [job_view(r) for r in job_rows], "events": [event_view(r) for r in ev_rows],
            "queues": HUB.queues, "dead": dead, "services_url": "/services", "scaling": HUB.scaling}


@router.get("/api/activity/job/{job_id}")
def one_job(job_id: str):
    try:
        uuid.UUID(job_id)
    except ValueError:
        return JSONResponse({"error": NOT_KNOWN}, status_code=404)
    try:
        with POOL.conn() as con:
            row = con.execute(JOB_SELECT + "WHERE job_id = %s", (job_id,)).fetchone()
            ev_rows = (con.execute(EVENT_SELECT + "WHERE e.job_id = %s AND " + NOT_TURN + " ORDER BY e.seq",
                                   (job_id,)).fetchall() if row else [])
    except psycopg.Error:
        return JSONResponse({"error": UNAVAILABLE}, status_code=503)
    if row is None:
        return JSONResponse({"error": NOT_KNOWN}, status_code=404)
    return {"job": job_view(row), "events": [event_view(r) for r in ev_rows]}


# ---- a job's log trail -----------------------------------------------------------------------------------------------
TRAIL_MAX = 3000
TRAIL_SERVICES = tuple(config.ORDER)
# exactly these keys leave the gateway: never a queue, a message id, a process id, a product name or any text
TRAIL_KEYS = ("step", "op", "state", "status", "error_code", "ms", "attempt", "read_ct", "n")


def trail_line(l, tree_by_id):
    job_id = l.get("job_id")
    row = tree_by_id.get(job_id)
    out = {"at": l.get("ts"), "service": l.get("service"), "job_id": job_id, "short": job_id[:8] if job_id else None,
           "depth": row["depth"] if row else 0, "job_type": row["type"] if row else l.get("type"),
           "target": row["target"] if row else l.get("target"), "event": l.get("event")}
    for k in TRAIL_KEYS:
        out[k] = l.get(k)
    return out


@router.get("/api/activity/trail/{job_id}")
def trail(job_id: str):
    try:
        uuid.UUID(job_id)
    except ValueError:
        return JSONResponse({"error": NOT_KNOWN}, status_code=404)
    try:
        with POOL.conn() as con:
            rows = bus_jobs.tree(con, job_id)
            views = ({str(r[0]): job_view(r) for r in con.execute(
                JOB_SELECT + "WHERE job_id = ANY(%s)", ([r["job_id"] for r in rows],)).fetchall()} if rows else {})
    except psycopg.Error:
        return JSONResponse({"error": UNAVAILABLE}, status_code=503)
    if not rows:
        return JSONResponse({"error": NOT_KNOWN}, status_code=404)
    lines = bus_jobs.trail_lines(rows)             # the log files are read after the pooled connection went back
    by_id = {str(r["job_id"]): r for r in rows}
    jobs_out = []
    for r in rows:
        v = dict(views.get(str(r["job_id"])) or {})
        v["depth"] = r["depth"]
        jobs_out.append(v)
    truncated = len(lines) > TRAIL_MAX
    out_lines = [trail_line(l, by_id) for l in lines[:TRAIL_MAX]]
    counts = {s: 0 for s in TRAIL_SERVICES}
    for l in lines:
        counts[l.get("service")] = counts.get(l.get("service"), 0) + 1
    return {"job": jobs_out[0] if not any(v["job_id"] == job_id for v in jobs_out)
            else next(v for v in jobs_out if v["job_id"] == job_id),
            "jobs": jobs_out, "lines": out_lines, "services": counts,
            "first_at": lines[0].get("ts") if lines else None, "last_at": lines[-1].get("ts") if lines else None,
            "truncated": truncated}


@router.get("/api/activity/dead")
def dead(limit: int = 50):
    limit = _limit(limit, 50, 200)
    try:
        with POOL.conn() as con:
            return _dead_list(con, limit)
    except psycopg.Error:
        return JSONResponse({"error": UNAVAILABLE}, status_code=503)


@router.get("/api/activity/stream")
async def stream(request: Request):
    raw = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    try:
        after = max(0, int(raw))
    except ValueError:
        after = 0
    await run_in_threadpool(HUB.ensure)

    async def gen():
        nonlocal after
        yield "retry: 2000\n\n"
        sent_at = time.monotonic()
        queues_at = 0.0
        scaling_sent = 0
        batch = await run_in_threadpool(HUB.since, after)
        while True:
            for ev in batch:
                yield _sse("ev", ev, ev["event_id"])
                after = ev["event_id"]
                sent_at = time.monotonic()
            now = time.monotonic()
            if now - queues_at >= QUEUES_EVERY_S and HUB.queues:
                queues_at = now
                yield _sse("queues", HUB.queues)
                sent_at = now
            elif now - sent_at >= KEEPALIVE_S:
                yield ": keepalive\n\n"
                sent_at = now
            if HUB.scaling_last > scaling_sent:
                scaling_sent = HUB.scaling_last
                yield _sse("scaling", HUB.scaling)
                sent_at = now
            await asyncio.sleep(POLL_S)
            if await request.is_disconnected():
                return
            batch = HUB.ring_since(after)
            if batch is None:                             # far behind the ring: read the database
                batch = await run_in_threadpool(HUB.since, after)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
