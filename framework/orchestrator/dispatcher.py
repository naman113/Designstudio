"""The job loop: the only writer of `orch.jobs` and `orch.job_events`.

One thread, one work connection, one queue connection. It holds the Postgres advisory lock `hashtext('orch.dispatcher')`
for life, so a second orchestrator on the same database does not dispatch (its /health says "blocked").

Loop: read `orch_reply` (progress and replies, in pgmq order), read `orch_in` (job submits), dispatch every queued job to
its service's `<svc>_cmd`, and when nothing happened wait for a NOTIFY (at most 1 s, so delayed and expired messages are
found). Every message is applied in ONE transaction with its `bus.processed` row and its archive, so a crash in between
redelivers it and nothing is applied twice.

It handles submit, dispatch, progress and ok/error replies, `job.stop`, orchestrator retries, the sweeper (deadlines and
the Stop fallback), `job.redelivered` / `message.dead_lettered`, a service's death (its commands are made visible at once)
and adoption of open jobs when the orchestrator itself starts.

Replicas: the process that runs a command is recorded (`orch.jobs.runner_pid`, from `progress started`), a replica's
death releases only its own commands (`service_died(name, pid, last)`), and every `control.stop` is also broadcast with
NOTIFY `stop_<service>` so it reaches the replica that runs the job.

Recipes: a multi-step recipe is a parent row (`kind = 'recipe'`) whose steps run in order as child command rows
(`parent_job_id` set). `advance_recipes` starts the steps, gates on `wait_for`, ends the parent, and `handle_stop` / the
sweeper / `adopt` treat a parent like any open job. A parent is never sent to a service queue.

A job that does not answer a Stop is ended by the sweeper's forced stop after STOP_GRACE_S.

Turn jobs (`policy.TURN_TYPES`, empty here, so this path is dormant) may send page events (`turn.*`) and may get a
follow-on (`recipes.FOLLOW_ON`, also empty): the turn stays `running` with `step = 'mesh'` and `target = 'orchestrator'`,
a child job makes the design's 3D view, and `advance_follow_ons` ends the turn (`turn.status`, `turn.done`,
`job.succeeded`) when that child is final.
"""
import json
import os
import queue as stdqueue
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from framework.common import bus_schema, envelope, jobs, pg, pgmq, policy, queues, results, slog
from framework.orchestrator import engine_mode, events, recipes

FINAL = ("succeeded", "failed", "timed_out", "stopped")
OPEN = ("queued", "dispatched", "running", "stopping")
USER_TEXT = {"unknown_type": "This request is not known to the studio.",
             "busy": "This design is busy right now. Try again in a minute.",
             "bad_request": "This request could not be read.",
             "timed_out": "This step took longer than its time limit and was stopped.",
             "stopped": "Stopped.",
             "too_large": "This step's data was too large to hand on, so it was stopped."}
NEVER_RETRIED = ("dead_letter", "interrupted", "timed_out", "unknown_type", "bad_envelope")
APP_NAME = "orchestrator-dispatcher pid=%d"
TURN_EVENTS = ("status", "step", "text", "team")    # the page events a turn job may send (never `done`)
FOLLOW_STEP = "mesh"                                # a turn waiting for its follow-on job has this step
FOLLOW_TYPES = list(recipes.FOLLOW_ON)             # the turn types that may have a follow-on (none here)
FOLLOW_LIMIT_S = policy.wait_s("dual.mesh")   # the follow-on's own limit (its row + 30 s); the turn ends without a 3D view after it


def _log(msg):
    print("%s orchestrator: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def _eline(event, e, **fields):
    """One structured line about the message `e` (an envelope), written with its job context."""
    with slog.context(e):
        slog.log(event, **fields)


class Dispatcher(threading.Thread):
    def __init__(self, base_port):
        super().__init__(name="dispatcher", daemon=True)
        self.base = base_port
        self.state = "starting"            # starting | running | blocked | error
        self.lock_held = False
        self.jobs_open = 0
        self.recipes_open = 0
        self.late_replies = 0
        self.last_error = ""
        self._depths = {}
        self._halt = threading.Event()
        self.W = None
        self.waiter = None
        self._last_refresh = 0.0
        self._last_sweep = 0.0
        self._died = stdqueue.Queue()      # service names the supervisor saw exit, read by this thread only
        self._adopted = False
        self.stats = {"retried": 0, "timed_out": 0, "stopped": 0, "forced_stops": 0, "released": 0, "adopted": 0}
        self.caps = dict(policy.CAPS)      # the cap of a target; /check/cap changes it for a check, n = 0 restores it
        self._caps_lock = threading.Lock()

    # ---- what /health shows ----------------------------------------------------------------------------------------
    def health(self):
        return {"state": self.state, "lock": self.lock_held, "jobs_open": self.jobs_open,
                "recipes_open": self.recipes_open, "engine_mode": engine_mode.read(), "late_replies": self.late_replies, "last_error": self.last_error, "queues": dict(self._depths),
                "stats": dict(self.stats), "caps": self.get_caps()}

    def get_caps(self):
        with self._caps_lock:
            return dict(self.caps)

    def set_cap(self, service, n):
        """Check hook (thread-safe): the cap of `service` for a check; n = 0 restores the policy value. The dispatcher
        reads it on its next pass (at most 1 s)."""
        with self._caps_lock:
            if n == 0:
                if service in policy.CAPS:
                    self.caps[service] = policy.CAPS[service]
                else:
                    self.caps.pop(service, None)
            else:
                self.caps[service] = n
            return dict(self.caps)

    def stop(self):
        self._halt.set()

    def service_died(self, name, pid=None, last=True, before=None):    # thread-safe; the loop does the work
        self._died.put((name, pid, last, before))

    @contextmanager
    def _tx(self):
        """Every change of the job tables is one transaction, and the structured-log lines of its events are written
        when it has COMMITTED (a rolled-back transaction writes none). Use it for every transaction here."""
        with slog.deferred(), self.W.transaction():
            yield

    # ---- boot: connections, the bus, the lock ------------------------------------------------------------------------
    def _close(self):
        for c in (self.waiter, ):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        try:
            if self.W is not None:
                self.W.close()
        except Exception:
            pass
        self.W = self.waiter = None
        self.lock_held = False

    def _boot(self):
        """Connect, make sure the bus exists, take the lock, LISTEN. Retries every 5 s; returns when running."""
        while not self._halt.is_set():
            try:
                self._close()
                self.W = pg.connect(write=True)
                self.W.execute("SELECT set_config('application_name', %s, false)", (APP_NAME % os.getpid(),))
                bus_schema.ensure(self.W)
                if not self.W.execute("SELECT pg_try_advisory_lock(hashtext('orch.dispatcher'))").fetchone()[0]:
                    if self.state != "blocked":
                        _log("another orchestrator holds the dispatcher lock on this database: not dispatching")
                    self.state = "blocked"
                    self._halt.wait(5)
                    continue
                self.lock_held = True
                self.waiter = pgmq.Waiter()
                self.waiter.listen(queues.ORCH_IN, queues.ORCH_REPLY)
                self.state = "running"
                self.last_error = ""
                self._refresh(force=True)
                _log("dispatcher running (lock held, listening on %s and %s)" % (queues.ORCH_IN, queues.ORCH_REPLY))
                return
            except Exception as exc:
                self.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
                self.state = "starting" if isinstance(exc, psycopg.OperationalError) else "error"
                _log("dispatcher waiting: %s" % self.last_error)
                self._halt.wait(5)

    # ---- the loop -----------------------------------------------------------------------------------------------------
    def run(self):
        slog.setup("orchestrator")
        while not self._halt.is_set():
            self._boot()
            if self._halt.is_set():
                break
            try:
                if not self._adopted:          # once per process: right after the first time the lock is held
                    self.adopt()
                    self._adopted = True
                self._loop()
            except psycopg.OperationalError as exc:
                self.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
                self.state = "error"
                _log("lost the database connection (%s); reconnecting" % type(exc).__name__)
                slog.log("db.reconnect", kind="svc", level="warn", exc=type(exc).__name__)
            except Exception as exc:                      # the dispatcher never dies of one bad message
                self.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
                self.state = "error"
                _log("dispatcher error: %s" % self.last_error)
                slog.log("svc.error", kind="svc", level="error", exc=type(exc).__name__)
                time.sleep(1)
        self._close()

    def _loop(self):
        while not self._halt.is_set():
            n = self._drain(queues.ORCH_REPLY, self.handle_reply)       # replies first: runner_pid is current for deaths
            n += self._drain_died()
            n += self._drain(queues.ORCH_IN, self.handle_submit)
            n += self.advance_recipes()                 # a new step goes out in the same pass
            n += self.advance_follow_ons()              # a turn whose 3D view is made ends when it is
            n += self.dispatch_ready()
            n += self.sweep()
            self._refresh()
            if n == 0:
                self.waiter.wait(policy.FALLBACK_READ_S)

    def _drain(self, queue, fn):
        got = pgmq.read(self.waiter.con, queue, queues.VT_S[queue], 50)
        for pm in got:
            try:
                fn(pm)
            except psycopg.OperationalError:
                raise
            except Exception as exc:
                # a message that cannot be applied is archived and logged (a dead letter).
                _log("could not apply %s message %s: %s: %s" % (queue, pm.pgmq_id, type(exc).__name__, exc))
                try:
                    pgmq.archive(self.W, queue, pm.pgmq_id)
                except psycopg.OperationalError:
                    raise
        return len(got)

    def _refresh(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_refresh < 2.0:
            return
        self._last_refresh = now
        self.jobs_open, self.recipes_open = self.W.execute(
            "SELECT count(*), count(*) FILTER (WHERE kind = 'recipe') FROM orch.jobs "
            "WHERE state IN ('queued','dispatched','running','stopping')").fetchone()
        mine = {queues.ORCH_IN, queues.ORCH_REPLY}
        self._depths = {r["queue_name"]: {"waiting": int(r["queue_length"]), "visible": int(r["queue_visible_length"])}
                        for r in pgmq.metrics_all(self.W) if r["queue_name"] in mine}

    # ---- a submit from orch_in ---------------------------------------------------------------------------------------
    def _applied(self, queue, pm, e):
        cur = self.W.execute(
            "INSERT INTO bus.processed (consumer, msg_id, queue, pgmq_id, job_id, attempt, read_ct, status, "
            "tenant_id, account_id) VALUES ('orchestrator', %s, %s, %s, %s, %s, %s, 'applied', %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING 1",
            (e["msg_id"], queue, pm.pgmq_id, e["job_id"], e["attempt"], pm.read_ct, e["tenant_id"], e["account_id"]))
        return cur.fetchone() is not None

    def _bad(self, queue, pm, why):
        _log("bad envelope %s on %s: %s" % (pm.pgmq_id, queue, why))
        pgmq.archive(self.W, queue, pm.pgmq_id)

    def handle_submit(self, pm):
        e = pm.message
        try:
            envelope.validate(e)
        except envelope.EnvelopeError as exc:
            return self._bad(queues.ORCH_IN, pm, str(exc))
        if e["kind"] != "command":
            if e["type"] == "job.stop":
                return self.handle_stop(pm, e)
            _log("%s on orch_in is archived (not a command)" % e["type"])
            pgmq.archive(self.W, queues.ORCH_IN, pm.pgmq_id)
            return
        with self._tx():
            if not self._applied(queues.ORCH_IN, pm, e):
                pgmq.archive(self.W, queues.ORCH_IN, pm.pgmq_id)
                return
            try:
                mode = None
                if recipes.is_factory(e["type"]):          # read once; the parent keeps it in its payload
                    mode = engine_mode.read()
                    e = dict(e, payload=dict(e["payload"], engine_mode=mode))
                recipe = recipes.resolve(e["type"], mode)
                if e["type"] in recipes.RECIPES:           # a multi-step recipe is a parent row
                    pol = policy.policy(e["type"])
                    if pol is None:
                        raise recipes.UnknownType(e["type"])
                    return self._submit_recipe(pm, e, recipe, pol)
                pol = policy.policy(recipe.steps[0].command)
                if pol is None:
                    raise recipes.UnknownType(e["type"])
            except recipes.UnknownType:
                self._insert_failed(e)
                pgmq.archive(self.W, queues.ORCH_IN, pm.pgmq_id)
                _log("%s %s -> failed (unknown_type)" % (str(e["job_id"])[:8], e["type"]))
                return
            step = recipe.steps[0]
            target = policy.target(step.command)
            row = self.W.execute(
                "INSERT INTO orch.jobs (job_id, parent_job_id, kind, type, target, state, step, attempt, limit_s, "
                "submit_deadline, correlation_id, payload, submitted_by, tenant_id, account_id, convo) "
                "VALUES (%s, %s, 'command', %s, %s, 'queued', %s, 1, %s, %s::timestamptz, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (job_id) DO NOTHING RETURNING 1",
                (e["job_id"], e["parent_job_id"], e["type"], target, step.name, pol.limit_s, e["deadline"],
                 e["correlation_id"], Jsonb(e["payload"]), e["sender"], e["tenant_id"], e["account_id"],
                 self._convo_of(e))).fetchone()
            if row is not None:
                events.emit(self.W, e["job_id"], "job.queued", state="queued", attempt=1, service="orchestrator",
                            step=step.name, meta={"type": e["type"], "target": target, "by": e["sender"]})
                _log("orch %s %s ->queued" % (str(e["job_id"])[:8], e["type"]))
            pgmq.archive(self.W, queues.ORCH_IN, pm.pgmq_id)

    def _submit_recipe(self, pm, e, recipe, pol):
        """The parent row of a multi-step recipe (called inside handle_submit's transaction). Its time counts from
        submit, the wait gate included. `result_text` holds the finished steps' results."""
        if self._insert_recipe_parent(e, pol) is not None:
            _log("orch %s %s ->queued (recipe, %d steps)" % (str(e["job_id"])[:8], e["type"], len(recipe.steps)))
            if e["type"] == "product.erase":
                self._stop_drawings_of((e["payload"] or {}).get("product_id"), e["job_id"])
        pgmq.archive(self.W, queues.ORCH_IN, pm.pgmq_id)

    def _stop_drawings_of(self, pid, erase_id):
        """An erase does not wait for a design's drawings to be made (minutes, and the erase's own wait is 120 s): the
        design's open `drawings.make` jobs are stopped as by the Stop button, and the erase (which waits for them to end)
        goes on. Nothing of this runs for any other job type."""
        if not isinstance(pid, str):
            return
        for (jid,) in self.W.execute("SELECT job_id FROM orch.jobs WHERE type = 'drawings.make' AND payload->>'product_id' = %s "
                                     "AND state IN ('queued','running') AND job_id <> %s", (pid, erase_id)).fetchall():
            job = self._lock_job(jid)
            if job is None or job["state"] in FINAL or job["state"] == "stopping":
                continue
            if job["kind"] == "recipe" and job["state"] == "running":
                self._stop_recipe(job)
            else:
                self._stop_command(job)
            _log("orch %s drawings.make for %s stopped (the design is being erased)" % (str(jid)[:8], pid))

    def _insert_recipe_parent(self, e, pol, meta_extra=None):
        """The parent row of a multi-step recipe and its `job.queued` event (shared by a submit and by `_then`).
        -> a row, or None when the job id already exists."""
        row = self.W.execute(
            "INSERT INTO orch.jobs (job_id, parent_job_id, kind, type, target, state, step, attempt, limit_s, deadline, "
            "submit_deadline, correlation_id, payload, result_text, steps, submitted_by, tenant_id, account_id) "
            "VALUES (%s, %s, 'recipe', %s, NULL, 'queued', NULL, 1, %s, "
            "least(%s::timestamptz, clock_timestamp() + make_interval(secs => %s)), %s::timestamptz, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (job_id) DO NOTHING RETURNING 1",
            (e["job_id"], e["parent_job_id"], e["type"], pol.limit_s, e["deadline"], pol.limit_s, e["deadline"],
             e["correlation_id"], Jsonb(e["payload"]), json.dumps({"steps": {}}), Jsonb({}), e["sender"], e["tenant_id"],
             e["account_id"])).fetchone()
        if row is not None:
            events.emit(self.W, e["job_id"], "job.queued", state="queued", attempt=1, service="orchestrator",
                        meta=dict({"type": e["type"], "target": None, "by": e["sender"]}, **(meta_extra or {})))
        return row

    @staticmethod
    def _convo_of(e):
        """A turn job (policy.TURN_TYPES) remembers its conversation (one running request per conversation)."""
        convo = (e.get("payload") or {}).get("convo") if e["type"] in policy.TURN_TYPES else None
        return convo if isinstance(convo, str) and convo else None

    def _insert_failed(self, e):
        self.W.execute(
            "INSERT INTO orch.jobs (job_id, parent_job_id, kind, type, target, state, attempt, limit_s, submit_deadline, "
            "correlation_id, payload, error_code, user_text, submitted_by, tenant_id, account_id, finished_at) "
            "VALUES (%s, %s, 'command', %s, NULL, 'failed', 1, 0, %s::timestamptz, %s, %s, 'unknown_type', %s, %s, %s, %s, "
            "clock_timestamp()) ON CONFLICT (job_id) DO NOTHING",
            (e["job_id"], e["parent_job_id"], e["type"], e["deadline"], e["correlation_id"], Jsonb(e["payload"]),
             USER_TEXT["unknown_type"], e["sender"], e["tenant_id"], e["account_id"]))
        events.emit(self.W, e["job_id"], "job.failed", state="failed", attempt=1, service="orchestrator",
                    error_code="unknown_type", user_text=USER_TEXT["unknown_type"], meta={"ms": 0})

    # ---- dispatch -----------------------------------------------------------------------------------------------------
    JOB_COLS = ("job_id, parent_job_id, type, target, step, attempt, limit_s, submit_deadline, correlation_id, payload, "
                "tenant_id, account_id")

    def dispatch_ready(self):
        cur = self.W.cursor(row_factory=dict_row)
        jobs = cur.execute("SELECT " + self.JOB_COLS + " FROM orch.jobs WHERE state = 'queued' AND kind = 'command' "
                           "ORDER BY created_at LIMIT 50").fetchall()
        sent = 0
        caps = self.get_caps()
        for job in jobs:
            tcap = policy.TYPE_CAPS.get(job["type"])
            if tcap is not None:                   # drawings use at most 2 of the 4 geometry workers
                if self.W.execute("SELECT count(*) FROM orch.jobs WHERE type = %s AND state IN "
                                  "('dispatched','running','stopping')", (job["type"],)).fetchone()[0] >= tcap:
                    continue
            # a target in CAP_TYPES is capped over the listed types only; its other
            # types are never held, and the running count looks at those types only.
            types = policy.CAP_TYPES.get(job["target"])
            cap = caps.get(job["target"]) if types is None or job["type"] in types else None
            if cap is not None:
                if types is None:
                    running = self.W.execute("SELECT count(*) FROM orch.jobs WHERE target = %s AND state IN "
                                             "('dispatched','running','stopping')", (job["target"],)).fetchone()[0]
                else:
                    running = self.W.execute("SELECT count(*) FROM orch.jobs WHERE target = %s AND type = ANY(%s) "
                                             "AND state IN ('dispatched','running','stopping')",
                                             (job["target"], list(types))).fetchone()[0]
                if running >= cap:
                    continue
            self._dispatch_one(job)
            sent += 1
        for target, types in policy.CAP_TYPES.items():            # every waiting job of a capped line is told its place
            self._renumber_line(cur, target, types)
        return sent

    def _dispatch_one(self, job):
        # a command over the queue's 256 KB limit used to raise out of the loop on every pass
        # (measured: 323 "message too large" errors in 5 minutes, no other job dispatched). It now fails that one job.
        try:
            self._dispatch_one_send(job)
        except ValueError as exc:
            if "message too large" not in str(exc):
                raise
            with self._tx():
                self._finish(job["job_id"], "failed", error_code="too_large", user_text=USER_TEXT["too_large"])
                events.emit(self.W, job["job_id"], "job.failed", state="failed", attempt=job["attempt"],
                            service="orchestrator", step=job["step"], error_code="too_large",
                            user_text=USER_TEXT["too_large"], meta={"detail": str(exc)[:200]})
            _log("orch %s %s queued->failed (too_large: %s)" % (str(job["job_id"])[:8], job["type"], exc))

    def _dispatch_one_send(self, job):
        deadline = datetime.now(timezone.utc) + timedelta(seconds=job["limit_s"])
        if job["submit_deadline"] is not None and job["submit_deadline"] < deadline:
            deadline = job["submit_deadline"]
        cmd = envelope.command_for(job, step=job["step"], deadline=deadline)
        q = queues.cmd(job["target"])
        with self._tx():
            pid = pgmq.send(self.W, q, cmd, 0)
            with slog.context(job):
                slog.log("cmd.sent", queue=q, pgmq_id=pid, target=job["target"], attempt=job["attempt"])
            self.W.execute("UPDATE orch.jobs SET state = 'dispatched', deadline = %s, cmd_queue = %s, cmd_pgmq_id = %s, "
                           "cmd_msg_id = %s, dispatched_at = clock_timestamp(), queue_pos = NULL WHERE job_id = %s",
                           (deadline, q, pid, cmd["msg_id"], job["job_id"]))
            events.emit(self.W, job["job_id"], "job.dispatched", state="dispatched", attempt=job["attempt"],
                        service=job["target"], step=job["step"],
                        meta={"queue": q, "pgmq_id": pid, "deadline": cmd["deadline"]})
        _log("orch %s %s queued->dispatched" % (str(job["job_id"])[:8], job["type"]))

    def _renumber_line(self, cur, target, types):
        """Every waiting job of `target`'s line (only the job types in `types`, when given: a GPU line) whose place
        changed gets queue_pos and a `job.position {pos}` event (one transaction)."""
        moved = cur.execute(
            "SELECT job_id, pos FROM (SELECT job_id, queue_pos, row_number() OVER (ORDER BY created_at, job_id)::integer AS pos "
            "FROM orch.jobs WHERE target = %s AND state = 'queued' AND (%s::text[] IS NULL OR type = ANY(%s::text[]))) l "
            "WHERE queue_pos IS DISTINCT FROM pos ORDER BY pos",
            (target, None if types is None else list(types), None if types is None else list(types))).fetchall()
        if not moved:
            return
        with self._tx():
            for r in moved:
                job = self._lock_job(r["job_id"])
                if job is None or job["state"] != "queued":
                    continue
                self.W.execute("UPDATE orch.jobs SET queue_pos = %s WHERE job_id = %s", (r["pos"], r["job_id"]))
                events.emit(self.W, r["job_id"], "job.position", state="queued", attempt=job["attempt"],
                            service="orchestrator", step=job["step"], meta={"pos": r["pos"]})

    # ---- progress and replies from orch_reply -----------------------------------------------------------------------
    def handle_reply(self, pm):
        e = pm.message
        try:
            envelope.validate(e)
        except envelope.EnvelopeError as exc:
            return self._bad(queues.ORCH_REPLY, pm, str(exc))
        with self._tx():
            if not self._applied(queues.ORCH_REPLY, pm, e):
                pgmq.archive(self.W, queues.ORCH_REPLY, pm.pgmq_id)
                return
            job = self._lock_job(e["job_id"])
            p = e["payload"]
            if e["kind"] == "reply" or p.get("phase") in ("started", "step"):    # a turn's page-event batches are turn.* lines
                _eline("reply.read", e, read_ct=e["read_ct"], queue=queues.ORCH_REPLY, target=e["sender"],
                       status=p.get("status") if e["kind"] == "reply" else p.get("phase"),
                       step=p.get("step") if e["kind"] == "progress" and isinstance(p.get("step"), str) else None)
            if (job is None or job["state"] in FINAL or e["in_reply_to"] is None
                    or str(job["cmd_msg_id"]) != e["in_reply_to"]):
                self.late_replies += 1
                _log("late %s for %s ignored" % (e["kind"], str(e["job_id"])[:8]))
                _eline("reply.late", e, level="warn", read_ct=e["read_ct"], queue=queues.ORCH_REPLY, target=e["sender"])
                pgmq.archive(self.W, queues.ORCH_REPLY, pm.pgmq_id)
                return
            self._apply_reply(job, e)
            pgmq.archive(self.W, queues.ORCH_REPLY, pm.pgmq_id)

    def _lock_job(self, job_id):
        """The job row, locked for update (everything a reply, a stop or a retry needs)."""
        return self.W.cursor(row_factory=dict_row).execute(
            "SELECT job_id, parent_job_id, type, target, state, step, attempt, cmd_queue, cmd_pgmq_id, cmd_msg_id, "
            "limit_s, deadline, submit_deadline, correlation_id, payload, tenant_id, account_id, stop_requested_at, kind "
            "FROM orch.jobs WHERE job_id = %s FOR UPDATE", (job_id,)).fetchone()

    def _apply_reply(self, job, e):
        jid, svc, att = job["job_id"], job["target"], job["attempt"]
        p = e["payload"]
        if e["kind"] == "progress":
            if p.get("phase") == "started":
                rc = e["read_ct"] or 1
                if job["state"] in ("dispatched", "running") and (job["state"] == "dispatched" or rc > 1):
                    if rc > 1:             # the command was read again after an interruption
                        events.emit(self.W, jid, "job.redelivered", state="running", attempt=att, service=svc,
                                    step=job["step"], read_ct=rc, meta={"read_ct": rc})
                    self.W.execute("UPDATE orch.jobs SET state = 'running', started_at = clock_timestamp(), read_ct = %s, "
                                   "runner_pid = %s WHERE job_id = %s", (rc, p.get("pid"), jid))
                    events.emit(self.W, jid, "job.started", state="running", attempt=att, service=svc, step=job["step"],
                                read_ct=rc, meta={"read_ct": rc, "pid": p.get("pid")})
                    _log("orch %s %s %s->running (read %d)" % (str(jid)[:8], job["type"], job["state"], rc))
            elif p.get("phase") == "step":
                events.emit(self.W, jid, "job.progressed", state=job["state"], attempt=att, service=svc,
                            step=p.get("step") or job["step"], read_ct=e["read_ct"],
                            meta={"step": p.get("step"), **{k: p[k] for k in ("done", "total", "eta_s") if p.get(k) is not None}})
            elif p.get("phase") == "events" and job["type"] in policy.TURN_TYPES:
                self._turn_events(job, e)
            elif p.get("phase") == "proc" and job["type"] in policy.TURN_TYPES and isinstance(p.get("proc_json"), str):
                self.W.execute("UPDATE orch.jobs SET turn = coalesce(turn, '{}'::jsonb) || "
                               "jsonb_build_object('proc_json', %s::text) WHERE job_id = %s", (p["proc_json"], jid))
            return
        status = p.get("status")
        if status == "ok":
            if job["type"] in policy.TURN_TYPES:            # a turn job
                if self._begin_follow_on(job, e, p):          # the page's done waits for the follow-on job (FOLLOW_ON)
                    return
                self._turn_done(job, e, p.get("result_json"), p.get("result_ref"))
            # the result is the exact JSON text the service made: kept as text, never as jsonb numbers
            ms = self._finish(jid, "succeeded", result_text=p.get("result_json"), result_ref=p.get("result_ref"))
            events.emit(self.W, jid, "job.succeeded", state="succeeded", attempt=att, service="orchestrator",
                        step=job["step"], read_ct=e["read_ct"], meta={"ms": ms})
            _log("orch %s %s %s->succeeded" % (str(jid)[:8], job["type"], job["state"]))
        elif status == "error":
            err = p.get("error") or {}
            if self._retry(job, err, e):
                return
            if err.get("code") == "dead_letter":
                events.emit(self.W, jid, "message.dead_lettered", state=job["state"], attempt=att, service=svc,
                            step=job["step"], read_ct=e["read_ct"], error_code="dead_letter",
                            meta={"queue": queues.dead(job["cmd_queue"] or queues.cmd(svc)),
                                  "dead_pgmq_id": err.get("dead_pgmq_id"), "reads": e["read_ct"]})
            ms = self._finish(jid, "failed", error_code=err.get("code"), user_text=err.get("user_text"))
            events.emit(self.W, jid, "job.failed", state="failed", attempt=att, service="orchestrator", step=job["step"],
                        read_ct=e["read_ct"], error_code=err.get("code"), user_text=err.get("user_text"),
                        meta={"ms": ms})
            _log("orch %s %s %s->failed (%s)" % (str(jid)[:8], job["type"], job["state"], err.get("code")))
        elif status == "stopped":
            result_text = json.dumps(p["result"]) if isinstance(p.get("result"), dict) else None   # a turn's done
            if job["state"] == "stopping":
                ms = self._stop_ms(job)
                if job["type"] in policy.TURN_TYPES:
                    self._turn_done(job, e, result_text, None)
                self._finish(jid, "stopped", result_text=result_text, user_text=USER_TEXT["stopped"])
                events.emit(self.W, jid, "job.stopped", state="stopped", attempt=att, service="orchestrator",
                            step=job["step"], read_ct=e["read_ct"], user_text=USER_TEXT["stopped"],
                            meta={"stop_ms": ms})
                self.stats["stopped"] += 1
            elif job["deadline"] is not None and job["deadline"] < datetime.now(timezone.utc):
                # the service refused a command read after its deadline, before the sweeper looked
                self._time_out(job, withdraw=False)
            else:
                if job["type"] in policy.TURN_TYPES:
                    self._turn_done(job, e, result_text, None)
                self._finish(jid, "stopped", result_text=result_text, user_text=USER_TEXT["stopped"])
                events.emit(self.W, jid, "job.stopped", state="stopped", attempt=att, service="orchestrator",
                            step=job["step"], read_ct=e["read_ct"], user_text=USER_TEXT["stopped"], meta={})

    # ---- a turn job's page events and its done -----------------------------------------------------------------------
    def _turn_events(self, job, e):
        """One `turn.<ev>` event per item of a batch, in order, in this transaction. The orchestrator never reads the
        data: it is the JSON text the service wrote (`data_json`), kept as a string in `content`."""
        for item in e["payload"].get("events") or []:
            ev = item.get("ev") if isinstance(item, dict) else None
            if ev not in TURN_EVENTS or not isinstance(item.get("data_json"), str):
                _log("turn event of %s skipped (%r)" % (str(job["job_id"])[:8], ev))
                continue
            events.emit(self.W, job["job_id"], "turn." + ev, state=job["state"], attempt=job["attempt"],
                        service="designer", step=job["step"], read_ct=e["read_ct"],
                        meta={"n": item.get("n")}, content={"j": item["data_json"]})

    def _turn_done(self, job, e, result_text, result_ref, extra=None):
        """The turn's answer becomes `turn.done` before the job's final event: the page's done (inline as the JSON
        text, or by reference to a file under work/results/ when it was big)."""
        content = None
        try:
            data = json.loads(result_text) if result_text else None
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("done_json"), str):
            content = {"j": data["done_json"]}
        elif isinstance(data, dict) and isinstance(data.get("done_ref"), str):
            content = {"ref": data["done_ref"]}
        elif result_ref:
            content = {"ref": result_ref}                 # the consumer wrote the result file itself (over 64 KB)
        if content is None:
            return
        if extra:
            content.update(extra)                         # {"mesh_ready": bool}; the relay reads only `j` and `ref`
        events.emit(self.W, job["job_id"], "turn.done", state=job["state"], attempt=job["attempt"], service="designer",
                    step=job["step"], read_ct=e["read_ct"], content=content)

    def _finish(self, jid, state, result_text=None, result_ref=None, error_code=None, user_text=None):
        row = self.W.execute(
            "UPDATE orch.jobs SET state = %s, result_text = %s, result_ref = %s, error_code = %s, user_text = %s, "
            "finished_at = clock_timestamp(), queue_pos = NULL WHERE job_id = %s "
            "RETURNING (extract(epoch FROM clock_timestamp() - created_at) * 1000)::integer",
            (state, result_text, result_ref, error_code, user_text, jid)).fetchone()
        return row[0]

    def _stop_ms(self, job):
        row = self.W.execute("SELECT (extract(epoch FROM clock_timestamp() - stop_requested_at) * 1000)::integer "
                             "FROM orch.jobs WHERE job_id = %s", (job["job_id"],)).fetchone()
        return row[0] if row and row[0] is not None else 0

    # ---- retries --------------------------------------------------------------------------------------------------------
    def _retry(self, job, err, e):
        """A retryable error with tries left: one new delayed command, attempt + 1. True when it was retried."""
        if not err.get("retryable") or err.get("code") in NEVER_RETRIED or job["state"] not in ("dispatched", "running"):
            return False
        pol = policy.policy(job["type"])
        att = job["attempt"]
        if pol is None or att > pol.retries:
            return False
        delay = pol.backoff_s[min(att - 1, len(pol.backoff_s) - 1)]
        deadline = datetime.now(timezone.utc) + timedelta(seconds=delay + job["limit_s"])
        if job["submit_deadline"] is not None and job["submit_deadline"] < deadline:
            deadline = job["submit_deadline"]
        row = dict(job, attempt=att + 1)
        cmd = envelope.command_for(row, step=job["step"], deadline=deadline)
        q = queues.cmd(job["target"])
        events.emit(self.W, job["job_id"], "job.retrying", state="dispatched", attempt=att, service="orchestrator",
                    step=job["step"], read_ct=e["read_ct"], error_code=err.get("code"),
                    meta={"delay_s": delay, "next_attempt": att + 1})
        pid = pgmq.send(self.W, q, cmd, delay)
        with slog.context(job):
            slog.log("cmd.sent", queue=q, pgmq_id=pid, target=job["target"], attempt=att + 1, ms=delay * 1000)
        self.W.execute("UPDATE orch.jobs SET attempt = %s, state = 'dispatched', deadline = %s, cmd_queue = %s, "
                       "cmd_pgmq_id = %s, cmd_msg_id = %s, read_ct = 0, started_at = NULL, runner_pid = NULL WHERE job_id = %s",
                       (att + 1, deadline, q, pid, cmd["msg_id"], job["job_id"]))
        self.stats["retried"] += 1
        _log("orch %s %s retry %d in %d s (%s)" % (str(job["job_id"])[:8], job["type"], att + 1, delay, err.get("code")))
        return True

    # ---- Stop -----------------------------------------------------------------------------------------------------------
    def _withdraw_unread(self, job):
        """Remove the job's command if no service has read it yet. True when it was removed."""
        if not job["cmd_queue"] or job["cmd_pgmq_id"] is None:
            return False
        row = self.W.execute(
            sql.SQL("DELETE FROM pgmq.{} WHERE msg_id = %s AND read_ct = 0 RETURNING msg_id")
            .format(sql.Identifier("q_" + job["cmd_queue"])), (job["cmd_pgmq_id"],)).fetchone()
        return row is not None

    def _send_control_stop(self, job):
        msg = envelope.new("control", "control.stop", {"cmd_msg_id": str(job["cmd_msg_id"])}, sender="orchestrator",
                           job_id=str(job["job_id"]), correlation_id=str(job["correlation_id"]))
        pgmq.send(self.W, queues.ctl(job["target"]), msg, 0)
        # the control queue is read by whichever replica comes first; NOTIFY reaches every replica
        # (same transaction as the send: delivered when it commits)
        self.W.execute("SELECT pg_notify(%s, %s)", ("stop_" + job["target"],
                                                    json.dumps({"j": str(job["job_id"]), "m": str(job["cmd_msg_id"])})))

    def _stopped_now(self, job, forced=False, ms=0):
        self._finish(job["job_id"], "stopped", user_text=USER_TEXT["stopped"])
        meta = {"stop_ms": ms}
        if forced:
            meta["forced"] = True
        events.emit(self.W, job["job_id"], "job.stopped", state="stopped", attempt=job["attempt"],
                    service="orchestrator", step=job["step"], user_text=USER_TEXT["stopped"], meta=meta)
        self.stats["stopped"] += 1
        if forced:
            self.stats["forced_stops"] += 1

    def handle_stop(self, pm, e):
        with self._tx():
            if not self._applied(queues.ORCH_IN, pm, e):
                pgmq.archive(self.W, queues.ORCH_IN, pm.pgmq_id)
                return
            job = self._lock_job(e["job_id"])
            if job is None or job["state"] in FINAL or job["state"] == "stopping":
                _log("stop for %s ignored (%s)" % (str(e["job_id"])[:8], "unknown" if job is None else job["state"]))
            elif job["type"] in FOLLOW_TYPES and job["step"] == FOLLOW_STEP and job["state"] == "running":
                self._finish_follow_on(job, ok=False, stop_child=True)      # the turn is answered; its 3D view is dropped
            elif job["kind"] == "recipe" and job["state"] == "running":
                self._stop_recipe(job)
            else:
                self._stop_command(job)
            pgmq.archive(self.W, queues.ORCH_IN, pm.pgmq_id)

    def _stop_command(self, job):
        """Stop for one open command row (or a queued parent): stopped at once when nothing has read it, else a
        `control.stop` to its service."""
        if job["state"] == "queued":
            self._stopped_now(job)
            _log("orch %s queued->stopped" % str(job["job_id"])[:8])
        elif job["state"] == "dispatched" and self._withdraw_unread(job):
            self._stopped_now(job)
            _log("orch %s dispatched (unread)->stopped" % str(job["job_id"])[:8])
        else:                                                   # read by a service, or running
            self.W.execute("UPDATE orch.jobs SET state = 'stopping', stop_requested_at = clock_timestamp() "
                           "WHERE job_id = %s", (job["job_id"],))
            self._send_control_stop(job)
            events.emit(self.W, job["job_id"], "job.stopping", state="stopping", attempt=job["attempt"],
                        service=job["target"], step=job["step"], meta={})
            _log("orch %s %s->stopping" % (str(job["job_id"])[:8], job["state"]))
        if job["type"] in policy.TURN_TYPES:
            self._stop_turn_children(job)

    def _stop_turn_children(self, job):
        """A Stop, or the turn's wall clock, ends the jobs the turn itself started (its tools
        submit them with the turn as parent_job_id): every open child is stopped by the same rules as a Stop on it, so no
        measuring, pair check, quick look or video keeps a worker or the only GPU slot for a caller that has gone. Not run when
        a turn simply finishes: a job started by a turn goes on for the next one."""
        rows = self.W.execute("SELECT job_id FROM orch.jobs WHERE parent_job_id = %s AND state IN ('queued','dispatched','running') "
                              "ORDER BY created_at", (job["job_id"],)).fetchall()
        for (cid,) in rows:
            child = self._lock_job(cid)
            if child is None or child["state"] not in ("queued", "dispatched", "running"):
                continue
            if child["kind"] == "recipe" and child["state"] == "running":
                self._stop_recipe(child)
            else:
                self._stop_command(child)
            _log("orch %s turn %s: child %s %s stopped with it" % (str(job["job_id"])[:8], job["type"], str(cid)[:8], child["type"]))

    def _open_child(self, parent_id):
        """The parent's newest child when it is still open, locked; else None."""
        row = self.W.execute("SELECT job_id FROM orch.jobs WHERE parent_job_id = %s ORDER BY created_at DESC LIMIT 1",
                             (parent_id,)).fetchone()
        child = self._lock_job(row[0]) if row else None
        return child if child is not None and child["state"] in OPEN else None

    def _stop_recipe(self, job):
        """Stop on a running parent. The parent goes `stopping`, its open child is stopped by the same rules, and
        `advance_recipes` ends the parent `stopped` once that child is final. No further step starts."""
        self.W.execute("UPDATE orch.jobs SET state = 'stopping', stop_requested_at = clock_timestamp() WHERE job_id = %s",
                       (job["job_id"],))
        events.emit(self.W, job["job_id"], "job.stopping", state="stopping", attempt=job["attempt"],
                    service="orchestrator", step=job["step"], meta={})
        _log("orch %s %s step %s running->stopping" % (str(job["job_id"])[:8], job["type"], job["step"]))
        child = self._open_child(job["job_id"])
        if child is not None:
            self._stop_command(child)

    # ---- the sweeper: deadlines and the Stop fallback ---------------------------------------------------------------
    def _time_out(self, job, withdraw=True):
        self._finish(job["job_id"], "timed_out", error_code="timed_out", user_text=USER_TEXT["timed_out"])
        if withdraw and job["cmd_queue"] and job["cmd_pgmq_id"] is not None:
            pgmq.delete(self.W, job["cmd_queue"], job["cmd_pgmq_id"])
        self._send_control_stop(job)
        events.emit(self.W, job["job_id"], "job.timed_out", state="timed_out", attempt=job["attempt"],
                    service="orchestrator", step=job["step"], error_code="timed_out", user_text=USER_TEXT["timed_out"],
                    meta={"limit_s": job["limit_s"]})
        if job["type"] in policy.TURN_TYPES:
            self._stop_turn_children(job)
        self.stats["timed_out"] += 1
        _log("orch %s %s %s->timed_out (limit %d s)" % (str(job["job_id"])[:8], job["type"], job["state"], job["limit_s"]))

    def sweep(self):
        """At most every SWEEP_EVERY_S: time out jobs past their deadline, force-stop jobs whose service did not answer."""
        now = time.monotonic()
        if now - self._last_sweep < policy.SWEEP_EVERY_S:
            return 0
        self._last_sweep = now
        done = 0
        cur = self.W.cursor(row_factory=dict_row)
        for r in cur.execute("SELECT job_id FROM orch.jobs WHERE kind = 'recipe' AND state IN ('queued','running','stopping') "
                             "AND deadline < clock_timestamp() ORDER BY deadline LIMIT 50").fetchall():
            with self._tx():                 # a parent past its deadline, before its children's own
                job = self._lock_job(r["job_id"])
                if job is None or job["state"] not in ("queued", "running", "stopping"):
                    continue
                if job["deadline"] is None or job["deadline"] >= datetime.now(timezone.utc):
                    continue
                self._time_out_recipe(job)
                done += 1
        late = cur.execute("SELECT job_id FROM orch.jobs WHERE state IN ('dispatched','running') AND kind = 'command' "
                           "AND deadline < clock_timestamp() AND NOT (type = ANY(%s::text[]) AND step = %s) "
                           "ORDER BY deadline LIMIT 50", (FOLLOW_TYPES, FOLLOW_STEP)).fetchall()      # a turn at its follow-on is advance_follow_ons
        for r in late:
            with self._tx():
                job = self._lock_job(r["job_id"])
                if job is None or job["state"] not in ("dispatched", "running"):
                    continue
                if job["deadline"] is None or job["deadline"] >= datetime.now(timezone.utc):
                    continue
                self._time_out(job)
                done += 1
        stuck = cur.execute("SELECT job_id FROM orch.jobs WHERE state = 'stopping' AND kind = 'command' AND stop_requested_at < "
                            "clock_timestamp() - make_interval(secs => %s) LIMIT 50", (policy.STOP_GRACE_S,)).fetchall()
        for r in stuck:
            with self._tx():
                job = self._lock_job(r["job_id"])
                if job is None or job["state"] != "stopping":
                    continue
                if job["cmd_queue"] and job["cmd_pgmq_id"] is not None:
                    pgmq.delete(self.W, job["cmd_queue"], job["cmd_pgmq_id"])
                self._stopped_now(job, forced=True, ms=self._stop_ms(job))
                _log("orch %s stopping->stopped (forced, no answer in %d s)" % (str(job["job_id"])[:8], policy.STOP_GRACE_S))
                done += 1
        return done

    # ---- multi-step recipes ------------------------------------------------------------------------------------------
    def _time_out_recipe(self, job):
        """The parent's time (from submit) ran out: it ends `timed_out`, and its open child is stopped."""
        self._finish(job["job_id"], "timed_out", result_text=self._steps_text(job["job_id"]), error_code="timed_out",
                     user_text=USER_TEXT["timed_out"])
        events.emit(self.W, job["job_id"], "job.timed_out", state="timed_out", attempt=job["attempt"],
                    service="orchestrator", step=job["step"], error_code="timed_out", user_text=USER_TEXT["timed_out"],
                    meta={"limit_s": job["limit_s"]})
        self.stats["timed_out"] += 1
        _log("orch %s %s step %s ->timed_out (limit %d s)" % (str(job["job_id"])[:8], job["type"], job["step"],
                                                              job["limit_s"]))
        child = self._open_child(job["job_id"])
        if child is not None:
            self._stop_command(child)

    def _steps_text(self, jid):
        row = self.W.execute("SELECT result_text FROM orch.jobs WHERE job_id = %s", (jid,)).fetchone()
        return row[0] if row else None

    def advance_recipes(self):
        """Every loop pass: gate and start queued parents, record finished steps, start the next one, end the parent.
        A parent that answered before its last step (`trailing`) keeps getting its remaining steps, in order, after it
        is final. One transaction per parent change. Returns the number of parents that changed."""
        rows = self.W.cursor(row_factory=dict_row).execute(
            "SELECT job_id FROM orch.jobs WHERE kind = 'recipe' AND (state IN ('queued','running','stopping') OR trailing_steps) "
            "ORDER BY created_at LIMIT 50").fetchall()
        n = 0
        for r in rows:
            with self._tx():
                n += self._advance_one(r["job_id"])
        return n

    @staticmethod
    def _recipe_of(P):
        """The parent's recipe; a `dual.<op>` factory is resolved with the engine mode stored at submit."""
        if P["type"] not in recipes.RECIPES:
            return None
        return recipes.resolve(P["type"], (P["payload"] or {}).get("engine_mode"))

    def _advance_one(self, jid):
        cur = self.W.cursor(row_factory=dict_row)
        P = cur.execute("SELECT job_id, type, state, step, attempt, correlation_id, payload, result_text, steps, trailing_steps, "
                        "deadline, created_at, tenant_id, account_id FROM orch.jobs WHERE job_id = %s AND kind = 'recipe' "
                        "FOR UPDATE", (jid,)).fetchone()
        if P is None:
            return 0
        R = self._recipe_of(P)
        if P["state"] in FINAL:
            if not P["trailing_steps"]:
                return 0
            if R is None:
                self._set_trailing(P, False)
                return 1
            return self._advance_running(P, R)
        if P["state"] not in ("queued", "running", "stopping"):
            return 0
        if R is None:
            return self._fail_recipe(P, "unknown_type", USER_TEXT["unknown_type"])
        if P["state"] == "queued":
            if R.wait_for and self._must_wait(P, R):
                if (datetime.now(timezone.utc) - P["created_at"]).total_seconds() < R.wait_limit_s:
                    return 0
                return self._fail_recipe(P, "busy", USER_TEXT["busy"])
            return self._start_step(P, R, 0)
        return self._advance_running(P, R)

    def _must_wait(self, P, R):
        """True while an older open job (not this parent's own child) carries the same value under `R.wait_for`."""
        val = (P["payload"] or {}).get(R.wait_for)
        if val is None:
            return False
        val = val if isinstance(val, str) else json.dumps(val)
        # a design's drawings only READ the design, so a change of the design (a load, an upload) never waits for them: they
        # are waited for by the next drawings job of the design (one at a time) and by the erase (which stops them first)
        return self.W.execute(
            "SELECT count(*) FROM orch.jobs j WHERE j.state IN ('queued','dispatched','running','stopping') "
            "AND j.payload->>%s::text = %s::text AND j.job_id <> %s AND j.parent_job_id IS DISTINCT FROM %s AND j.created_at < %s "
            "AND (%s IN ('drawings.make', 'product.erase') OR (j.type <> 'drawings.make' AND NOT EXISTS ("
            "SELECT 1 FROM orch.jobs pp WHERE pp.job_id = j.parent_job_id AND pp.type = 'drawings.make')))",
            (R.wait_for, val, P["job_id"], P["job_id"], P["created_at"], P["type"])).fetchone()[0] > 0

    @staticmethod
    def _done_of(P):
        """The finished steps: {name: {state, job_id, error_code, user_text, result}} (the `steps` column)."""
        d = P.get("steps")
        return d if isinstance(d, dict) else {}

    @staticmethod
    def _args_results(done):
        """What a step's `args` sees: `results[name]` = the step's unpacked result ({} when it failed or was skipped) and
        `results["__steps__"][name]` = {state, job_id, error_code, user_text}."""
        res = {n: (results.unpack(st["result"]) if st.get("result") else {}) for n, st in done.items()}
        res["__steps__"] = {n: {k: st.get(k) for k in ("state", "job_id", "error_code", "user_text")}
                            for n, st in done.items()}
        return res

    def _save_steps(self, P, done):
        """Keep the steps record. `result_text = {"steps": {name: result}}` is kept up to date while the parent is
        not final; a final parent's result is its answer (or the steps record), never rewritten here."""
        P["steps"] = done
        if P["state"] in FINAL:
            self.W.execute("UPDATE orch.jobs SET steps = %s WHERE job_id = %s", (Jsonb(done), P["job_id"]))
            return
        P["result_text"] = json.dumps({"steps": {n: st.get("result") for n, st in done.items()}})
        self.W.execute("UPDATE orch.jobs SET steps = %s, result_text = %s WHERE job_id = %s",
                       (Jsonb(done), P["result_text"], P["job_id"]))

    def _set_trailing(self, P, value):
        P["trailing_steps"] = value
        self.W.execute("UPDATE orch.jobs SET trailing_steps = %s WHERE job_id = %s", (value, P["job_id"]))

    def _fail_recipe(self, P, code, text, trailing_from=None):
        ms = self._finish(P["job_id"], "failed", result_text=P["result_text"], error_code=code, user_text=text)
        events.emit(self.W, P["job_id"], "job.failed", state="failed", attempt=P["attempt"], service="orchestrator",
                    step=P["step"], error_code=code, user_text=text, meta={"ms": ms})
        _log("orch %s %s step %s %s->failed (%s)" % (str(P["job_id"])[:8], P["type"], P["step"], P["state"], code))
        P["state"] = "failed"
        if trailing_from is not None:            # steps that log still run after the failure
            self._set_trailing(P, True)
        return 1

    @staticmethod
    def _answer_idx(R):
        """The step whose result answers the parent: the last one with answers=True that is not a stand-in or a
        log-only step."""
        idx = [i for i, s in enumerate(R.steps) if s.answers and not s.fallback and s.on_fail != "log"]
        return idx[-1] if idx else len(R.steps) - 1

    def _answer_lost(self, R, done):
        """The answering step failed and no stand-in step is still to come."""
        a = done.get(R.steps[self._answer_idx(R)].name)
        if not a or a["state"] in ("succeeded", "skipped"):
            return None
        if any(s.fallback and s.name not in done for s in R.steps):
            return None
        return a

    def _start_step(self, P, R, i):
        final = P["state"] in FINAL
        done = dict(self._done_of(P))
        while i < len(R.steps):
            step = R.steps[i]
            if step.name in done:                       # recorded on an earlier pass (skipped, or a step already run)
                i += 1
                continue
            try:
                args = step.args(P["payload"] or {}, self._args_results(done))
                if args is not None and not isinstance(args, dict):
                    raise TypeError("step arguments are not a dict")
            except Exception as exc:
                _log("orch %s %s step %s: arguments could not be built (%s)" % (str(P["job_id"])[:8], P["type"], step.name,
                                                                              type(exc).__name__))
                if final:
                    self._set_trailing(P, False)
                    return 1
                return self._fail_recipe(P, "bad_request", USER_TEXT["bad_request"])
            if args is None:                            # not needed (a fresh file, no second engine ...)
                done[step.name] = {"state": "skipped", "job_id": None, "error_code": None, "user_text": None, "result": None}
                self._save_steps(P, done)
                if not final:
                    events.emit(self.W, P["job_id"], "job.step_finished", state=P["state"], attempt=P["attempt"],
                                service="orchestrator", step=step.name,
                                meta={"step": step.name, "child": None, "state": "skipped"})
                    lost = self._answer_lost(R, done)
                    if lost is not None:
                        return self._fail_recipe(P, lost["error_code"] or lost["state"],
                                                 lost["user_text"] or USER_TEXT["stopped"], trailing_from=i + 1)
                i += 1
                continue
            pol = policy.policy(step.command)
            target = policy.target(step.command)
            child = uuid.uuid4()
            self.W.execute(
                "INSERT INTO orch.jobs (job_id, parent_job_id, kind, type, target, state, step, attempt, limit_s, "
                "submit_deadline, correlation_id, payload, submitted_by, tenant_id, account_id) "
                "VALUES (%s, %s, 'command', %s, %s, 'queued', %s, 1, %s, %s, %s, %s, 'orchestrator', %s, %s)",
                (child, P["job_id"], step.command, target, step.name, pol.limit_s, P["deadline"], P["correlation_id"],
                 Jsonb(args), P["tenant_id"], P["account_id"]))
            events.emit(self.W, child, "job.queued", state="queued", attempt=1, service="orchestrator", step=step.name,
                        meta={"type": step.command, "target": target, "by": "orchestrator", "parent": str(P["job_id"])})
            if not final:
                self.W.execute("UPDATE orch.jobs SET state = 'running', step = %s, "
                               "started_at = coalesce(started_at, clock_timestamp()) WHERE job_id = %s",
                               (step.name, P["job_id"]))
                events.emit(self.W, P["job_id"], "job.step_dispatched", state="running", attempt=P["attempt"],
                            service="orchestrator", step=step.name, meta={"step": step.name, "child": str(child)})
            _log("orch %s %s step %s %s->%s" % (str(P["job_id"])[:8], P["type"], step.name, P["state"],
                                                "trailing" if final else "running"))
            return 1
        return self._recipe_complete(P, R, done)

    def _recipe_complete(self, P, R, done):
        """No step is left. A trailing parent is done; a running one ends: `succeeded` (result = the steps' record),
        or `failed` when its answering step failed and nothing stood in for it."""
        if P["state"] in FINAL:
            self._set_trailing(P, False)
            return 1
        lost = self._answer_lost(R, done)
        if lost is not None:
            return self._fail_recipe(P, lost["error_code"] or lost["state"], lost["user_text"] or USER_TEXT["stopped"])
        ms = self._finish(P["job_id"], "succeeded", result_text=P["result_text"])
        events.emit(self.W, P["job_id"], "job.succeeded", state="succeeded", attempt=P["attempt"],
                    service="orchestrator", step=P["step"], meta={"ms": ms})
        _log("orch %s %s step %s running->succeeded" % (str(P["job_id"])[:8], P["type"], P["step"]))
        self._then(P, R)
        return 1

    def _then(self, P, R):
        """The parent just succeeded. When `recipes.THEN` names a job for it (a product load, an upload, a
        registration: the design's drawings), queue it as a top-level job with the trigger's correlation id, in this same
        transaction. One change, one job: a drawings job of the same design that is still `queued` has not started its
        first step, so it will draw the newest state and this change is folded into it (one running and one waiting at
        most, never three). The hook never fails the trigger: it runs in a savepoint and a failure logs one line."""
        rule = recipes.THEN.get(P["type"])
        if rule is None:
            return
        try:
            with self.W.transaction():
                a = self._done_of(P).get(R.steps[self._answer_idx(R)].name) or {}
                res = results.unpack(a["result"]) if a.get("result") else {}
                nxt = rule(P["payload"] or {}, res if isinstance(res, dict) else {})
                if nxt is None:
                    return
                ctype, cpayload = nxt
                pid = cpayload.get("product_id")
                waiting = self.W.execute("SELECT job_id FROM orch.jobs WHERE type = %s AND state = 'queued' "
                                         "AND payload->>'product_id' = %s ORDER BY created_at LIMIT 1",
                                         (ctype, pid)).fetchone()
                if waiting is not None:
                    _log("orch %s %s: %s for %s already waiting (%s), folded" % (
                        str(P["job_id"])[:8], P["type"], ctype, pid, str(waiting[0])[:8]))
                    return
                pol = policy.policy(ctype)
                child = uuid.uuid4()
                e = {"job_id": child, "parent_job_id": None, "type": ctype, "correlation_id": P["correlation_id"],
                     "deadline": datetime.now(timezone.utc) + timedelta(seconds=pol.limit_s), "sender": "orchestrator",
                     "payload": dict(cpayload, after=str(P["job_id"])), "tenant_id": P["tenant_id"],
                     "account_id": P["account_id"]}
                self._insert_recipe_parent(e, pol, {"after": str(P["job_id"])[:8]})
                _log("orch %s %s: %s for %s queued (%s)" % (str(P["job_id"])[:8], P["type"], ctype, pid, str(child)[:8]))
        except Exception as exc:                    # a rule or a row that cannot be made never blocks the trigger
            _log("orch %s %s: the follow-up job was not made (%s)" % (str(P["job_id"])[:8], P["type"], type(exc).__name__))

    def _advance_running(self, P, R):
        cur = self.W.cursor(row_factory=dict_row)
        final = P["state"] in FINAL
        C = cur.execute("SELECT job_id, state, step, error_code, user_text, result_text, result_ref FROM orch.jobs "
                        "WHERE parent_job_id = %s ORDER BY created_at DESC LIMIT 1", (P["job_id"],)).fetchone()
        if C is None:                          # a running parent always has a child; a stopping one without is stopped
            if final:
                self._set_trailing(P, False)
                return 1
            if P["state"] == "stopping":
                return self._stop_recipe_now(P)
            return self._start_step(P, R, 0)
        if C["state"] not in FINAL:
            return 0                           # the open child is the dispatcher's: its reply or the sweeper ends it
        names = [s.name for s in R.steps]
        if C["step"] not in names:
            if final:
                self._set_trailing(P, False)
                return 1
            return self._fail_recipe(P, "interrupted", USER_TEXT["bad_request"])
        idx = names.index(C["step"])
        done = dict(self._done_of(P))
        if C["step"] not in done:              # a restart between the child's end and this pass lands here: once only
            if not final:
                events.emit(self.W, P["job_id"], "job.step_finished", state=P["state"], attempt=P["attempt"],
                            service="orchestrator", step=C["step"],
                            meta={"step": C["step"], "child": str(C["job_id"]), "state": C["state"]})
            done[C["step"]] = {"state": C["state"], "job_id": str(C["job_id"]), "error_code": C["error_code"],
                               "user_text": C["user_text"],
                               "result": jobs._result_of(dict(result_text=C["result_text"], result_ref=C["result_ref"]))}
            self._save_steps(P, done)
        if final:                              # trailing steps: the parent is not touched, the next step goes out
            return self._start_step(P, R, idx + 1)
        if P["state"] == "stopping":
            return self._stop_recipe_now(P)
        step = R.steps[idx]
        ans = self._answer_idx(R)
        last = len(R.steps) - 1
        if C["state"] == "succeeded" and (step.fallback or (idx == ans and (ans < last or recipes.is_factory(P["type"]) or R.answer_with_last
                                                                    or R.answer_record))):
            return self._answer(P, R, idx, C)  # answer at once (a dual recipe's result IS its answering step's); the rest trail
        if C["state"] == "succeeded" or (step.on_fail == "log" and not step.fallback):
            return self._start_step(P, R, idx + 1)          # the next step, or the end
        if idx == ans or step.fallback:        # the answering step or its stand-in failed: stand-in still to come?
            lost = self._answer_lost(R, done)
            if lost is None:
                return self._start_step(P, R, idx + 1)
            return self._fail_recipe(P, lost["error_code"] or lost["state"], lost["user_text"] or USER_TEXT["stopped"],
                                     trailing_from=idx + 1 if idx < last else None)
        return self._fail_recipe(P, C["error_code"] or C["state"], C["user_text"] or USER_TEXT["stopped"])

    def _answer(self, P, R, idx, C):
        """The parent answers as soon as its answering step succeeded; its result IS that child's result (the packed
        text or file, as `jobs.get(..., with_result=True)` reads it). Later steps are trailing."""
        if R.answer_record:                    # the answer is the steps record, not the step's own result
            ms = self._finish(P["job_id"], "succeeded", result_text=P["result_text"])
        else:
            ms = self._finish(P["job_id"], "succeeded", result_text=C["result_text"], result_ref=C["result_ref"])
        if idx + 1 < len(R.steps):
            self._set_trailing(P, True)
        events.emit(self.W, P["job_id"], "job.succeeded", state="succeeded", attempt=P["attempt"],
                    service="orchestrator", step=P["step"], meta={"ms": ms})
        _log("orch %s %s step %s running->succeeded (answered; %d step(s) trail)" % (
            str(P["job_id"])[:8], P["type"], R.steps[idx].name, len(R.steps) - idx - 1))
        self._then(P, R)
        return 1

    def _stop_recipe_now(self, P):
        ms = self._stop_ms(P)
        self._finish(P["job_id"], "stopped", result_text=P["result_text"], user_text=USER_TEXT["stopped"])
        events.emit(self.W, P["job_id"], "job.stopped", state="stopped", attempt=P["attempt"], service="orchestrator",
                    step=P["step"], user_text=USER_TEXT["stopped"], meta={"stop_ms": ms})
        self.stats["stopped"] += 1
        _log("orch %s %s step %s stopping->stopped" % (str(P["job_id"])[:8], P["type"], P["step"]))
        return 1                                    # the callers add the result to a counter

    # ---- the follow-on job of a turn ---------------------------------------------------------------------------------
    def _begin_follow_on(self, job, e, p):
        """The turn answered. When `recipes.FOLLOW_ON` names a job for the answer (a render: its 3D view), the turn is
        not finished: its answer is kept on the row, it goes to step `mesh` under the orchestrator (no service slot is held),
        the page hears "Meshing <id> for the viewer", and the follow-on job is queued as a child of the turn. -> True when
        the follow-on took over (the caller does not finish the turn)."""
        rule = recipes.FOLLOW_ON.get(job["type"])
        if rule is None:
            return False
        try:
            data = json.loads(p.get("result_json")) if p.get("result_json") else None
            nxt = rule(data) if isinstance(data, dict) else None
        except Exception as exc:                      # a rule that cannot decide never blocks the answer
            _log("follow-on of %s skipped (%s)" % (str(job["job_id"])[:8], type(exc).__name__))
            return False
        if nxt is None:
            return False
        ctype, cpayload = nxt
        jid = job["job_id"]
        self.W.execute("UPDATE orch.jobs SET target = 'orchestrator', step = %s, result_text = %s, result_ref = %s, "
                       "cmd_queue = NULL, cmd_pgmq_id = NULL WHERE job_id = %s",
                       (FOLLOW_STEP, p.get("result_json"), p.get("result_ref"), jid))
        render = cpayload.get("product_id") or ""
        events.emit(self.W, jid, "turn.status", state="running", attempt=job["attempt"], service="orchestrator",
                    step=FOLLOW_STEP, content={"j": json.dumps({"label": "Meshing %s for the viewer" % render, "state": "run"})})
        child = self._insert_child(job, ctype, cpayload)
        _log("orch %s %s answered; follow-on %s %s" % (str(jid)[:8], job["type"], ctype, str(child)[:8]))
        return True

    def _insert_child(self, job, ctype, cpayload):
        """Queue `ctype` as a child of `job` (submitted by the orchestrator): a multi-step recipe as a recipe parent row, any
        other type as a command row. The engine mode is read once, as at a submit."""
        mode = engine_mode.read() if recipes.is_factory(ctype) else None
        payload = dict(cpayload, engine_mode=mode) if mode else dict(cpayload)
        child = uuid.uuid4()
        if ctype in recipes.RECIPES:
            pol = policy.policy(ctype)
            self.W.execute(
                "INSERT INTO orch.jobs (job_id, parent_job_id, kind, type, target, state, step, attempt, limit_s, deadline, "
                "submit_deadline, correlation_id, payload, result_text, steps, submitted_by, tenant_id, account_id) "
                "VALUES (%s, %s, 'recipe', %s, NULL, 'queued', NULL, 1, %s, clock_timestamp() + make_interval(secs => %s), NULL, "
                "%s, %s, %s, %s, 'orchestrator', %s, %s)",
                (child, job["job_id"], ctype, pol.limit_s, pol.limit_s, job["correlation_id"], Jsonb(payload),
                 json.dumps({"steps": {}}), Jsonb({}), job["tenant_id"], job["account_id"]))
            target, step = None, None
        else:
            command = recipes.resolve(ctype).steps[0]
            pol = policy.policy(command.command)
            target = policy.target(command.command)
            self.W.execute(
                "INSERT INTO orch.jobs (job_id, parent_job_id, kind, type, target, state, step, attempt, limit_s, "
                "submit_deadline, correlation_id, payload, submitted_by, tenant_id, account_id) "
                "VALUES (%s, %s, 'command', %s, %s, 'queued', %s, 1, %s, NULL, %s, %s, 'orchestrator', %s, %s)",
                (child, job["job_id"], ctype, target, command.name, pol.limit_s, job["correlation_id"], Jsonb(payload),
                 job["tenant_id"], job["account_id"]))
            step = command.name
        events.emit(self.W, child, "job.queued", state="queued", attempt=1, service="orchestrator", step=step,
                    meta={"type": ctype, "target": target, "by": "orchestrator", "parent": str(job["job_id"])})
        return child

    def _follow_on_child(self, jid):
        return self.W.cursor(row_factory=dict_row).execute(
            "SELECT job_id, kind, state, created_at, extract(epoch FROM clock_timestamp() - created_at) AS age FROM orch.jobs "
            "WHERE parent_job_id = %s AND submitted_by = 'orchestrator' ORDER BY created_at DESC LIMIT 1", (jid,)).fetchone()

    def advance_follow_ons(self):
        """Every loop pass: a turn at step `mesh` ends when its follow-on job is final (or has run past
        FOLLOW_LIMIT_S: the child is stopped and the turn ends without its 3D view). One transaction per turn."""
        rows = self.W.cursor(row_factory=dict_row).execute(
            "SELECT job_id FROM orch.jobs WHERE type = ANY(%s::text[]) AND state = 'running' AND step = %s LIMIT 50",
            (FOLLOW_TYPES, FOLLOW_STEP)).fetchall()
        n = 0
        for r in rows:
            with self._tx():
                job = self._lock_job(r["job_id"])
                if job is None or job["state"] != "running" or job["step"] != FOLLOW_STEP:
                    continue
                child = self._follow_on_child(job["job_id"])
                if child is None:
                    self._finish_follow_on(job, ok=False)
                elif child["state"] in FINAL:
                    self._finish_follow_on(job, ok=child["state"] == "succeeded")
                elif child["age"] > FOLLOW_LIMIT_S:
                    self._finish_follow_on(job, ok=False, stop_child=True)
                else:
                    continue
                n += 1
        return n

    def _finish_follow_on(self, job, ok, stop_child=False):
        """The turn ends as answered: "Meshed <id>" (only when the follow-on succeeded), `turn.done` (`mesh_ready`), the job
        `succeeded`; with `stop_child` the open follow-on job is stopped by the usual rules (a Stop during the wait, or its
        limit)."""
        jid = job["job_id"]
        row = self.W.execute("SELECT result_text, result_ref FROM orch.jobs WHERE job_id = %s", (jid,)).fetchone()
        result_text, result_ref = (row[0], row[1]) if row else (None, None)
        if stop_child:
            child = self._follow_on_child(jid)
            if child is not None and child["state"] not in FINAL:
                c = self._lock_job(child["job_id"])
                if c is not None and c["state"] in OPEN:
                    if c["kind"] == "recipe" and c["state"] == "running":
                        self._stop_recipe(c)
                    else:
                        self._stop_command(c)
        try:
            render = (json.loads(result_text) or {}).get("render") if result_text else None
        except ValueError:
            render = None
        if ok and render:
            events.emit(self.W, jid, "turn.status", state="running", attempt=job["attempt"], service="orchestrator",
                        step=FOLLOW_STEP, content={"j": json.dumps({"label": "Meshed %s" % render, "state": "done"})})
        self._turn_done(job, {"read_ct": None}, result_text, result_ref, extra={"mesh_ready": bool(ok)})
        ms = self._finish(jid, "succeeded", result_text=result_text, result_ref=result_ref)
        events.emit(self.W, jid, "job.succeeded", state="succeeded", attempt=job["attempt"], service="orchestrator",
                    step=job["step"], meta={"ms": ms})
        _log("orch %s %s follow-on %s -> succeeded" % (str(jid)[:8], job["type"], "done" if ok else "not made"))

    # ---- a service's death and the orchestrator's own restart -------------------------------------------------------
    def _drain_died(self):
        n = 0
        while True:
            try:
                name, pid, last, before = self._died.get_nowait()
            except stdqueue.Empty:
                return n
            n += 1
            self._service_gone(name, pid, last, before)

    def _service_gone(self, name, pid=None, last=True, before=None):
        """A service process is gone: make its in-flight commands visible at once, end its stopping jobs.
        `last` (no other replica of the service lives, or the orchestrator's adoption): every command of the service.
        Otherwise only the commands whose `runner_pid` is `pid`; a replica without a known process
        releases nothing (its commands come back when their visibility runs out).
        `before` (epoch seconds; containers): only `running` commands started before that moment, i.e. by the OLD process;
        the new process may have read commands already and every container process is pid 7, so nothing is released by pid or by name."""
        if not last and pid is None:
            _log("service %s: a replica without a known process ended; its commands return when their time runs out"
                 % name)
            return
        mine = "" if last else " AND runner_pid = %s"
        args = (name,) if last else (name, pid)
        if before is not None:
            mine += " AND started_at < to_timestamp(%s)"
            args = args + (before,)
        cur = self.W.cursor(row_factory=dict_row)
        released = stopped = 0
        with self._tx():
            rows = cur.execute("SELECT job_id, cmd_queue, cmd_pgmq_id FROM orch.jobs WHERE target = %s AND "
                               "state IN (" + ("'running'" if before is not None else "'dispatched','running'") + ") AND cmd_pgmq_id IS NOT NULL" + mine +
                               " ORDER BY created_at", args).fetchall()
            by_queue = {}
            for r in rows:
                by_queue.setdefault(r["cmd_queue"], []).append(r["cmd_pgmq_id"])
            for q, ids in by_queue.items():
                released += pgmq.set_vt(self.W, q, ids, 0)
            for r in cur.execute("SELECT job_id FROM orch.jobs WHERE target = %s AND state = 'stopping'" + mine +
                                 " ORDER BY created_at", args).fetchall():
                job = self._lock_job(r["job_id"])
                if job is None or job["state"] != "stopping":
                    continue
                if job["cmd_queue"] and job["cmd_pgmq_id"] is not None:
                    pgmq.delete(self.W, job["cmd_queue"], job["cmd_pgmq_id"])
                self._stopped_now(job, forced=True, ms=self._stop_ms(job))
                stopped += 1
        self.stats["released"] += released
        if last:
            _log("service %s died: released %d, stopped %d" % (name, released, stopped))
        else:
            _log("service %s replica pid %s died: released %d, stopped %d" % (name, pid, released, stopped))

    def adopt(self):
        """Once, right after the lock is held: every open job gets `job.adopted`, and every service counts as died."""
        with self._tx():
            rows = self.W.cursor(row_factory=dict_row).execute(
                "SELECT job_id FROM orch.jobs WHERE state IN ('dispatched','running','stopping') ORDER BY created_at"
            ).fetchall()
            for r in rows:
                job = self._lock_job(r["job_id"])
                if job is None or job["state"] not in ("dispatched", "running", "stopping"):
                    continue
                events.emit(self.W, job["job_id"], "job.adopted", state=job["state"], attempt=job["attempt"],
                            service="orchestrator", step=job["step"], meta={"was": job["state"]})
                self.stats["adopted"] += 1
        _log("adopted %d open job(s)" % len(rows))
        if os.environ.get("BC_SUPERVISE") == "containers":
            # the services outlive the orchestrator's container; what they run is theirs. A service that died meanwhile
            # gets its commands back from the restart rule (`before` above) or when their visibility runs out.
            _log("containers mode: the services' commands are not released")
            return
        for name in queues.SERVICES:
            self._service_gone(name)
