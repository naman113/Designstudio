"""The consumer base every worker service runs.

    Consumer(service, handlers, pool).start()

One queue thread holds the process's queue connection (a `pgmq.Waiter`) and reads `<svc>_cmd`; up to `pool` handler
threads run the commands; one extender thread keeps the visibility of running commands alive (+30 s every 10 s) and
refreshes the queue depths shown by /health. A handler is `fn(ctx, payload) -> dict | Result`.

Per command: validate; send `progress started`; run the handler inside `service.job(...)`; then the FINAL TRANSACTION
on one pool connection: the `bus.processed` row (a conflict means another delivery already applied it), the handler's
effects, the reply on `reply_to`, and the archive of the command. If the archive finds no row (the orchestrator
withdrew the message, or another delivery archived it first) everything rolls back, so an effect, its reply and the
ack commit together or not at all.

Control: the control queue `<svc>_ctl` (read before `<svc>_cmd` on every loop: `control.stop` and
`control.ping`), the stop set, the redelivery rules (read 6 or more is a dead letter, a "fail" type is never run twice,
an expired deadline or a stopped job is answered `stopped` without running), nack with backoff for an unexpected
exception, the dead letter on read 5, and the detection of a withdrawn command.

Replicas: several processes of one service read the same queues, so a type listed in
`policy.SINGLE` runs under the named lock of `framework/common/single.py` (one at a time across replicas), a Stop reaches
every replica through the NOTIFY channel `stop_<svc>`, `idle()` / `idle_s` tell the orchestrator when an extra replica
has had nothing to do, and `service.retire` makes the replica stop reading, finish its job and exit.
"""
import json
import os
import sys
import threading
import time
import traceback
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

from psycopg.types.json import Jsonb

from framework.common import config, envelope, pgmq, policy, queues, single, slog
from framework.common.pgpool import Pool

STOP_SET_TTL_S = 120
RESULT_INLINE_MAX = 64 * 1024          # a bigger result text goes to work/results/<job_id>.json


class ServiceError(Exception):
    def __init__(self, code, user_text, *, retryable=False, detail=""):
        super().__init__("%s: %s" % (code, detail or user_text))
        self.code, self.user_text, self.retryable, self.detail = code, user_text, retryable, detail


class Stopped(Exception):
    def __init__(self, result=None):
        super().__init__("stopped")
        self.result = result                  # what a stopped job still answers, or None


@dataclass
class Result:
    data: dict
    effects: Optional[Callable] = None        # effects(cur): runs inside the final transaction, only on ok


class _Rollback(Exception):
    pass


class Ctx:
    """What a handler sees of its command."""

    def __init__(self, consumer, env, read_ct, pm_id):
        self._consumer = consumer
        self.envelope = env
        self.job_id = env["job_id"]
        self.attempt = env["attempt"]
        self.read_ct = read_ct
        self.deadline = env["deadline"]
        self.cancelled = threading.Event()
        self._pgmq_id = pm_id
        self._stop_hooks = []

    def progress(self, step):
        self._consumer._send_progress(self.envelope, "step", self.read_ct, step=step)

    def progress_count(self, step, done, total, eta_s=None):    # a step with a count and an estimate
        extra = {"done": done, "total": total}
        if eta_s is not None:
            extra["eta_s"] = eta_s
        self._consumer._send_progress(self.envelope, "step", self.read_ct, step=step, extra=extra)

    def progress_raw(self, phase, extra):     # a progress message with its own phase and fields
        self._consumer._send_progress(self.envelope, phase, self.read_ct, extra=extra)

    def check(self):
        if self.cancelled.is_set():
            raise Stopped()

    def sleep(self, s):
        end = time.monotonic() + float(s)
        while True:
            self.check()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.1, left))

    def on_stop(self, fn):                    # The queue thread calls the hooks on control.stop
        self._stop_hooks.append(fn)


def _log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Consumer:
    def __init__(self, service, handlers, pool=1):
        self.service = service
        self.svc = service.name
        self.handlers = handlers
        self.pool_size = max(1, int(pool))
        self.cmd_q = queues.cmd(self.svc)
        self.ctl_q = queues.ctl(self.svc)
        self._pool = Pool(self.pool_size + 1)
        self._exec = ThreadPoolExecutor(max_workers=self.pool_size, thread_name_prefix="handler-%s" % self.svc)
        self._lock = threading.Lock()
        self._inflight = 0
        self._running = {}                       # pgmq_id -> (queue, Ctx)
        self._depths = {}
        self._stats = {"state": "starting", "pool": self.pool_size, "read": 0, "ok": 0, "error": 0, "stopped": 0,
                       "redelivered": 0, "nacked": 0, "dead": 0, "interrupted": 0, "withdrawn": 0}
        self._stop_set = {}                      # job_id -> monotonic time a control.stop arrived (TTL STOP_SET_TTL_S)
        self._stop = threading.Event()
        self._waiter = None
        self._threads = []
        self._last_busy = time.monotonic()       # the last time a command was read or finished (idle_s)
        self._tl = threading.local()             # the handler thread's start time and last logged step (log lines)

    # ---- public --------------------------------------------------------------------------------------------------
    def start(self):
        for target, name in ((self._queue_loop, "queue"), (self._extender_loop, "extender")):
            t = threading.Thread(target=target, name="%s-%s" % (name, self.svc), daemon=True)
            t.start()
            self._threads.append(t)

    def close(self, timeout_s=10):
        self._stop.set()
        self._exec.shutdown(wait=True, cancel_futures=True)
        if self._waiter is not None:
            self._waiter.close()
        self._pool.close()
        self._stats["state"] = "stopped"

    def stats(self):
        with self._lock:
            out = dict(self._stats)
            out["stop_set"] = len(self._stop_set)
            out["idle_s"] = 0.0 if self._inflight else round(time.monotonic() - self._last_busy, 1)
            out["replica"] = self.service.replica
        if self.service.draining and out["state"] == "running":
            out["state"] = "draining"
        return out

    def idle(self):
        """No command in flight."""
        with self._lock:
            return self._inflight == 0

    def depths(self):
        with self._lock:
            return {k: dict(v) for k, v in self._depths.items()}

    # ---- the queue thread --------------------------------------------------------------------------------------------
    def _queue_loop(self):
        # the queue connection; retried until Postgres answers (a service runs without jobs while it is down)
        while not self._stop.is_set() and self._waiter is None:
            try:
                w = pgmq.Waiter()
                w.listen(self.cmd_q, self.ctl_q)
                w.subscribe("stop_" + self.svc, self._on_stop_notify)       # Stop reaches every replica
                self._waiter = w
            except Exception as exc:
                self._set_state("error")
                _log("%s consumer: waiting for the database (%s)" % (self.svc, type(exc).__name__))
                self._stop.wait(5)
        self._set_state("running")
        while not self._stop.is_set():
            try:
                self._waiter.poll()                                           # stop broadcasts that arrived meanwhile
                free = self.pool_size - self._inflight
                want = [(self.ctl_q, queues.VT_S[self.ctl_q], 10)]          # control first, on every loop
                take = not self.service.draining and free > 0
                if take:
                    want.append((self.cmd_q, queues.VT_S[self.cmd_q], free))
                # look once, then wait for a NOTIFY: the draining flag is checked before every read, so a command that
                # arrives after a drain request is never taken (read_first's own wait would read on regardless)
                got = self._waiter.read_first(want, 0)
                if not got:
                    self._waiter.wait(policy.FALLBACK_READ_S if take else 0.05)
                    continue
                if got[0] == self.ctl_q:
                    for pm in got[1]:
                        self._control(pm)
                    continue
                for pm in got[1]:
                    with self._lock:
                        self._last_busy = time.monotonic()
                        self._inflight += 1
                        self._stats["read"] += 1
                        if pm.read_ct > 1:
                            self._stats["redelivered"] += 1
                    self._exec.submit(self._handle, pm)
            except Exception as exc:
                _log("%s consumer: queue loop error: %s: %s" % (self.svc, type(exc).__name__, exc))
                self._stop.wait(1)

    # ---- control messages ---------------------------------------------------------------------------
    def _control(self, pm):
        e = pm.message
        try:
            envelope.validate(e)
            typ = e["type"]
            if typ == "control.stop":
                self._on_control_stop(e["job_id"], e)
            elif typ == "control.ping":
                slog.log("ctl.ping", kind="svc", correlation_id=e["correlation_id"], queue=self.ctl_q)
                if e["reply_to"] is not None:
                    pong = envelope.new("control", "control.pong",
                                        {"status": "ok", "service": self.svc, "pid": os.getpid()}, sender=self.svc,
                                        correlation_id=e["correlation_id"], in_reply_to=e["msg_id"])
                    with self._pool.conn() as con:
                        pgmq.send(con, e["reply_to"], pong)
            else:
                _log("%s control: unknown message %s ignored" % (self.svc, typ))
        except envelope.EnvelopeError as exc:
            _log("%s control: bad message %s: %s" % (self.svc, pm.pgmq_id, exc))
        except Exception as exc:
            _log("%s control: %s: %s" % (self.svc, type(exc).__name__, exc))
        try:
            with self._pool.conn() as con:
                pgmq.archive(con, self.ctl_q, pm.pgmq_id)
        except Exception as exc:
            _log("%s control: could not archive %s: %s" % (self.svc, pm.pgmq_id, type(exc).__name__))

    def _on_stop_notify(self, payload):
        """The broadcast twin of `control.stop`: `{"j": job_id, "m": cmd_msg_id}`."""
        try:
            job_id = json.loads(payload)["j"]
        except Exception:
            _log("%s stop broadcast: bad payload %r" % (self.svc, str(payload)[:80]))
            return
        self._on_control_stop(job_id)

    def _on_control_stop(self, job_id, e=None):
        now = time.monotonic()
        with self._lock:
            self._stop_set[job_id] = now
            for j in [j for j, t in self._stop_set.items() if now - t > STOP_SET_TTL_S]:
                del self._stop_set[j]
            ctxs = [c for (_, c) in self._running.values() if c.job_id == job_id]
        for c in ctxs:
            first = not c.cancelled.is_set()            # the queue message and the broadcast may both arrive
            c.cancelled.set()
            if first and c._stop_hooks:
                threading.Thread(target=self._run_hooks, args=(c,), name="stop-hooks", daemon=True).start()
        _log("%s control.stop %s (%s)" % (self.svc, str(job_id)[:8], "running here" if ctxs else "not running here"))
        with slog.context(e):                   # the queue message carries the correlation id, the broadcast does not
            slog.log("ctl.stop", job_id=job_id, queue=self.ctl_q, status="running_here" if ctxs else "not_here")

    @staticmethod
    def _run_hooks(ctx):
        for fn in list(ctx._stop_hooks):
            try:
                fn()
            except Exception as exc:
                _log("stop hook failed: %s: %s" % (type(exc).__name__, exc))

    def _in_stop_set(self, job_id):
        with self._lock:
            t = self._stop_set.get(job_id)
            return t is not None and time.monotonic() - t <= STOP_SET_TTL_S

    def _set_state(self, state):
        with self._lock:
            self._stats["state"] = state

    # ---- one command -------------------------------------------------------------------------------------------------
    def _send_progress(self, cmd, phase, read_ct, step=None, extra=None):
        env = envelope.progress(cmd, phase, sender=self.svc, read_ct=read_ct, step=step, extra=extra)
        with self._pool.conn() as con:
            pgmq.send(con, cmd["reply_to"], env)
        if phase == "started":                   # one line per step; a count update of the same step (frames) is not one
            with slog.context(cmd):               # a render's progress comes from its own thread: the line names the job
                slog.log("cmd.started", read_ct=read_ct)
        elif phase == "step" and step and step != getattr(self._tl, "last_step", None):
            self._tl.last_step = step
            with slog.context(cmd):
                slog.log("cmd.progress", step=step, read_ct=read_ct)

    def _bad(self, pm, why):
        """An envelope that cannot be read: a dead letter `bad_envelope`, no reply (there is no valid reply_to)."""
        _log("%s bad envelope %s: %s" % (self.svc, pm.pgmq_id, why))
        slog.log("msg.bad", kind="svc", level="error", queue=self.cmd_q, pgmq_id=pm.pgmq_id, exc="EnvelopeError")
        try:
            with self._pool.conn() as con:
                with con.transaction():
                    self._send_dead(con, pm, "bad_envelope", why)
                    pgmq.archive(con, self.cmd_q, pm.pgmq_id)
            with self._lock:
                self._stats["dead"] += 1
        except Exception as exc:
            _log("%s could not dead-letter bad message %s: %s" % (self.svc, pm.pgmq_id, type(exc).__name__))

    def _send_dead(self, con, pm, code, detail):
        """The dead-letter record on `<cmd queue>_dead`; returns its pgmq id."""
        rec = {"dead_v": 1, "queue": self.cmd_q, "pgmq_id": pm.pgmq_id, "read_ct": pm.read_ct,
               "dead_at": envelope.now_iso(), "error": {"code": code, "detail": str(detail)[:500]},
               "envelope": pm.message}
        try:
            return pgmq.send(con, queues.dead(self.cmd_q), rec)
        except ValueError:                     # an envelope too big to keep in a record
            rec["envelope"] = {"note": "the original was too large to keep here"}
            return pgmq.send(con, queues.dead(self.cmd_q), rec)

    @staticmethod
    def _expired(e):
        try:
            return e["deadline"] is not None and datetime.fromisoformat(e["deadline"]) < datetime.now(timezone.utc)
        except (TypeError, ValueError):
            return False

    def _handle(self, pm):
        t0 = time.perf_counter()
        status = "?"
        e = pm.message
        held = ExitStack()               # the service counts the job as running until its final transaction has committed
        self._tl.t0, self._tl.last_step = t0, None
        try:
            # 1. validate; an invalid envelope is a dead letter `bad_envelope` (no reply)
            try:
                envelope.validate(e)
            except envelope.EnvelopeError as exc:
                status = "bad_envelope"
                return self._bad(pm, str(exc))
            if e["reply_to"] is None:
                status = "bad_envelope"
                return self._bad(pm, "reply_to: missing on a command")
            held.enter_context(self.service.job(e["type"], e["job_id"]))
            held.enter_context(slog.context(e, queue=self.cmd_q, pgmq_id=pm.pgmq_id))      # this thread's job context
            rc = pm.read_ct
            slog.log("cmd.read", read_ct=rc)
            pol = policy.policy(e["type"])
            # 2. read_ct > MAX_READS: read 5 crashed the process; dead letter without running
            if rc > policy.MAX_READS:
                status = "dead_letter"
                slog.log("cmd.refused", level="warn", status="max_reads", read_ct=rc)
                return self._final(pm, e, "dead_letter", None, None,
                                   dead=("max_reads", "the service stopped while working on it"))
            # 3. a redelivered command of a "fail" type is never run again
            if rc > 1 and pol is not None and pol.redeliver == "fail":
                status = "interrupted"
                slog.log("cmd.refused", level="warn", status="interrupted", read_ct=rc)
                return self._final(pm, e, "interrupted", envelope.reply_error(
                    e, "interrupted", policy.INTERRUPTED_TEXT.get(e["type"], "The work was interrupted. Please try again."),
                    sender=self.svc, read_ct=rc,
                    retryable=False, detail="read %d of a command that is never run twice" % rc), None)
            # 4. the deadline has passed, or a control.stop arrived first: answer stopped without running
            if self._expired(e):
                status = "timed_out"
                slog.log("cmd.refused", level="warn", status="timed_out", read_ct=rc)
                return self._final(pm, e, "timed_out", envelope.reply_stopped(e, sender=self.svc, read_ct=rc), None)
            if self._in_stop_set(e["job_id"]):
                status = "stopped"
                slog.log("cmd.refused", level="warn", status="stopped", read_ct=rc)
                return self._final(pm, e, "stopped", envelope.reply_stopped(e, sender=self.svc, read_ct=rc), None)
            # 5. progress started, then register the job and the message with the extender
            self._send_progress(e, "started", rc)
            ctx = Ctx(self, e, rc, pm.pgmq_id)
            with self._lock:
                self._running[pm.pgmq_id] = (self.cmd_q, ctx)
            # 5b. a type that writes a file database or a file in place runs one at a time across every
            # replica: the named lock is held from here to the end of the final transaction
            res = policy.single(e["type"])
            gate = (single.hold(res, ctx=ctx, service=self.svc, replica=self.service.replica, envelope=e)
                    if res else nullcontext())
            try:
                with gate:
                    try:
                        status, reply, effects = self._run(ctx, e)
                    finally:
                        with self._lock:
                            self._running.pop(pm.pgmq_id, None)
                    # 7. an unexpected exception: nack with backoff, or the dead letter on read MAX_READS
                    if status == "exception":
                        exc = reply
                        if rc < policy.MAX_READS:
                            delay = policy.nack_delay_s(rc)
                            with self._pool.conn() as con:
                                pgmq.set_vt(con, self.cmd_q, pm.pgmq_id, delay)
                            with self._lock:
                                self._stats["nacked"] += 1
                            status = "nack"
                            slog.log("cmd.nack", level="warn", read_ct=rc, ms=delay * 1000, exc=type(exc).__name__)
                            _log("%s %s nack read %d, again in %d s: %s" % (self.svc, str(e["job_id"])[:8], rc, delay,
                                                                           type(exc).__name__))
                            return
                        status = "dead_letter"
                        return self._final(pm, e, "dead_letter", None, None, dead=("handler_exception", repr(exc)))
                    # the final transaction
                    self._final(pm, e, status, reply, effects)
            except Stopped:                  # stopped or timed out while waiting for the one-at-a-time lock
                with self._lock:
                    self._running.pop(pm.pgmq_id, None)
                status = "stopped"
                self._final(pm, e, "stopped", envelope.reply_stopped(e, sender=self.svc, read_ct=rc), None)
        except Exception as exc:
            status = "lost"
            _log("%s %s %s handler thread error: %s: %s" % (self.svc, str(e.get("job_id"))[:8], e.get("type"),
                                                           type(exc).__name__, exc))
        finally:
            held.close()
            with self._lock:
                self._inflight -= 1
                self._last_busy = time.monotonic()
            _log("%s %s %s read %d -> %s %d ms" % (self.svc, str(e.get("job_id"))[:8], e.get("type"), pm.read_ct,
                                                    status, round((time.perf_counter() - t0) * 1000)))

    def _run(self, ctx, e):
        """Run the handler. Returns (status, reply envelope, effects); status "exception" returns the exception itself."""
        handler = self.handlers.get(e["type"])
        rc = ctx.read_ct
        if handler is None:
            return "error", envelope.reply_error(e, "unknown_type", "This request is not known to the studio.",
                                                 sender=self.svc, read_ct=rc, detail="no handler for %s" % e["type"]), None
        try:
            out = handler(ctx, e["payload"])
            effects = None
            if isinstance(out, Result):
                out, effects = out.data, out.effects
            ref = None
            if out is not None:
                text = envelope.pack(out)
                if len(text) > RESULT_INLINE_MAX:
                    ref = self._write_result(e["job_id"], text)
                    out = None
            return "ok", envelope.reply_ok(e, out, sender=self.svc, read_ct=rc, result_ref=ref), effects
        except ServiceError as exc:
            return "error", envelope.reply_error(e, exc.code, exc.user_text, sender=self.svc, read_ct=rc,
                                                 retryable=exc.retryable, detail=exc.detail), None
        except Stopped as exc:
            return "stopped", envelope.reply_stopped(e, sender=self.svc, read_ct=rc, result=exc.result), None
        except Exception as exc:
            _log("%s %s %s handler raised %s" % (self.svc, str(e["job_id"])[:8], e["type"], type(exc).__name__))
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()
            return "exception", exc, None

    @staticmethod
    def _write_result(job_id, text):
        """The result's exact JSON text, written as a temp file and replaced atomically (once-only by its path)."""
        rel = "work/results/%s.json" % job_id
        path = os.path.join(config.ROOT, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
        return rel

    def _final(self, pm, e, status, reply, effects, dead=None):
        """Processed row, effects, reply, archive: one transaction; no archive row means roll back.
        `dead=(code, detail)` makes it a dead letter (8.4): the record goes to `<q>_dead`, the reply is `dead_letter`."""
        try:
            with self._pool.conn() as con:
                with con.transaction():
                    if dead is not None:
                        dead_id = self._send_dead(con, pm, dead[0], dead[1])
                        reply = envelope.reply_error(
                            e, "dead_letter", "This step failed 5 times in a row and was moved aside.",
                            sender=self.svc, read_ct=pm.read_ct, retryable=False, detail=dead[1], dead_pgmq_id=dead_id)
                    cur = con.execute(
                        "INSERT INTO bus.processed (consumer, msg_id, queue, pgmq_id, job_id, attempt, read_ct, status, "
                        "reply, tenant_id, account_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT DO NOTHING RETURNING 1",
                        (self.svc, e["msg_id"], self.cmd_q, pm.pgmq_id, e["job_id"], e["attempt"], pm.read_ct, status,
                         Jsonb(reply), e["tenant_id"], e["account_id"]))
                    if cur.fetchone() is None:
                        raise _Rollback("already applied by another delivery")
                    if effects is not None and status == "ok":
                        with con.cursor() as ecur:
                            effects(ecur)
                    pgmq.send(con, e["reply_to"], reply)
                    if not pgmq.archive(con, self.cmd_q, pm.pgmq_id):
                        raise _Rollback("the command was withdrawn")
        except _Rollback as why:
            with self._lock:
                self._stats["withdrawn"] += 1
            _log("%s %s %s dropped: %s" % (self.svc, str(e["job_id"])[:8], e["type"], why))
            slog.log("cmd.withdrawn", level="warn", read_ct=pm.read_ct)
            return
        # the lines of a command that was applied are written after its transaction committed
        t0 = getattr(self._tl, "t0", None)
        err = ((reply or {}).get("payload") or {}).get("error") if isinstance(reply, dict) else None
        code = err.get("code") if isinstance(err, dict) else None
        if dead is not None:
            slog.log("cmd.dead_letter", level="error", read_ct=pm.read_ct, error_code=dead[0])
        slog.log("cmd.done", level="info" if status == "ok" else "warn", status=status, read_ct=pm.read_ct,
                 ms=None if t0 is None else round((time.perf_counter() - t0) * 1000), error_code=code)
        with self._lock:
            key = {"dead_letter": "dead", "timed_out": "stopped"}.get(status, status)
            self._stats[key if key in self._stats else "error"] += 1

    # ---- the extender thread -------------------------------------------------------------------------------------------
    def _extender_loop(self):
        last_extend = last_depth = 0.0
        while not self._stop.wait(1.0):
            now = time.monotonic()
            try:
                if now - last_extend >= policy.EXTEND_EVERY_S:
                    last_extend = now
                    with self._lock:
                        mine = [(q, i, c) for i, (q, c) in self._running.items()]
                    if mine:
                        with self._pool.conn() as con:
                            for q, i, c in mine:
                                found = pgmq.set_vt(con, q, i, policy.EXTEND_BY_S)
                                if not found and not c.cancelled.is_set():
                                    # the orchestrator withdrew the command: stop at the next check; the final
                                    # transaction's archive finds no row and rolls back (counted as withdrawn there)
                                    c.cancelled.set()
                                    _log("%s %s command withdrawn: stopping" % (self.svc, str(c.job_id)[:8]))
                if now - last_depth >= 5.0 or not self._depths:
                    last_depth = now
                    self._refresh_depths()
            except Exception as exc:
                _log("%s extender: %s: %s" % (self.svc, type(exc).__name__, exc))

    def _refresh_depths(self):
        mine = {self.cmd_q, self.ctl_q} | ({queues.CATALOGUE_Q} if self.svc == "catalogue" else set())
        with self._pool.conn() as con:
            rows = pgmq.metrics_all(con)
        out = {r["queue_name"]: {"waiting": int(r["queue_length"]), "visible": int(r["queue_visible_length"])}
               for r in rows if r["queue_name"] in mine}
        with self._lock:
            self._depths = out
