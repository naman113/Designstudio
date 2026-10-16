"""The query-queue consumer of a service: it answers page reads.

    Service("catalogue", HANDLERS, queries=QUERIES).run()  ->  Answerer(service, QUERIES).start()

A page read is not a job: no `orch.jobs` row, no orchestrator. The asker (framework/common/asker.py) sends a `query`
envelope to the service's query queue (`catalogue_q`); the answerer reads it, runs the handler on a pooled connection,
and sends one `reply` to the asker's `reply_to` queue, deleting the query in the same transaction.

    handler(qctx, params) -> (code: int, source: str | None, body: JSON-able)

The body travels as JSON text in the string field `payload.body` (exactly the bytes the studio serves), or above 64 KB as a
file under work/results/q-<msg_id>.json named in `payload.body_ref`. Never as nested jsonb: jsonb reorders keys and
turns -0.0 into 0. Page reads are DELETED, not archived: they are read-only, high-volume
(the page alone polls /api/state every 3-8 s) and have no audit value.

One waiter connection reads; up to `threads` pooled connections answer. Neither is ever shared.
"""
import json
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

from framework.common import config, envelope, pgmq, queues, slog
from framework.common.pgpool import Pool

BODY_INLINE_MAX = 64 * 1024
ORPHAN_MAX_AGE_S = 600
UNKNOWN = {"error": "This request is not known to the studio."}
FAILED = {"error": "The catalogue could not answer."}


def _log(text):
    sys.stdout.write("%s %s\n" % (time.strftime("%H:%M:%S"), text))
    sys.stdout.flush()


def dump(body):
    """The exact serialisation Starlette's JSONResponse uses (the studio sends these bytes unchanged)."""
    return json.dumps(body, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":"))


def sweep_orphans(max_age_s=ORPHAN_MAX_AGE_S):
    """Delete work/results/q-*.json older than `max_age_s`: bodies of askers that timed out."""
    folder = os.path.join(config.ROOT, "work", "results")
    n = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    now = time.time()
    for name in names:
        if name.startswith("q-") and name.endswith(".json"):
            path = os.path.join(folder, name)
            try:
                if now - os.path.getmtime(path) > max_age_s:
                    os.remove(path)
                    n += 1
            except OSError:
                pass
    return n


class Answerer:
    def __init__(self, service, handlers, *, queue=queues.CATALOGUE_Q, threads=4):
        self.service = service
        self.handlers = dict(handlers)
        self.queue = queue
        self.threads = threads
        self._lock = threading.Lock()
        self._busy = 0
        self._stop = threading.Event()
        self._stats = {"answered": 0, "errors": 0, "late": 0, "bad": 0, "refs": 0}
        self._ms = deque(maxlen=200)
        self._depths = {}
        self._depth_t = 0.0
        self._waiter = None
        self._pool = None
        self._exec = None
        self._thread = None
        self._state = "new"

    # ---- lifecycle -------------------------------------------------------------------------------------------------
    def start(self):
        self._waiter = pgmq.Waiter()
        self._waiter.listen(self.queue)
        self._pool = Pool(self.threads)
        self._exec = ThreadPoolExecutor(self.threads, thread_name_prefix="answer-%s" % self.service.name)
        self._state = "running"
        try:
            sweep_orphans()
        except Exception:
            pass
        self._thread = threading.Thread(target=self._loop, name="answerer-%s" % self.service.name, daemon=True)
        self._thread.start()

    def close(self, timeout_s=5):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        if self._exec is not None:
            self._exec.shutdown(wait=False)
        for obj in (self._waiter, self._pool):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._state = "closed"

    # ---- the queue thread ------------------------------------------------------------------------------------------
    def _loop(self):
        last_sweep = time.monotonic()
        while not self._stop.is_set():
            try:
                if time.monotonic() - last_sweep > 600:
                    last_sweep = time.monotonic()
                    sweep_orphans()
                if self.service.hung():                  # a hung service answers nothing (the checks rely on this)
                    time.sleep(0.2)
                    continue
                with self._lock:
                    free = self.threads - self._busy
                if free <= 0:
                    self._waiter.wait(0.2)
                    continue
                got = self._waiter.read_first([(self.queue, queues.VT_S[self.queue], free)], 1.0)
                if got:
                    for m in got[1]:
                        with self._lock:
                            self._busy += 1
                        self._exec.submit(self._guard, m)
            except Exception as exc:
                _log("answerer %s loop: %s: %s" % (self.service.name, type(exc).__name__, str(exc)[:160]))
                time.sleep(0.5)

    def _guard(self, m):
        try:
            self._answer(m)
        except Exception as exc:
            _log("answerer %s answer failed: %s: %s" % (self.service.name, type(exc).__name__, str(exc)[:160]))
            with self._lock:
                self._stats["errors"] += 1
        finally:
            with self._lock:
                self._busy -= 1

    # ---- one query -------------------------------------------------------------------------------------------------
    def _drop(self, m, kind, why):
        with self._pool.conn() as con:
            pgmq.delete(con, self.queue, m.pgmq_id)
        with self._lock:
            self._stats[kind] += 1
        if kind == "bad":
            _log("answerer bad query %s: %s" % (m.pgmq_id, why))

    def _answer(self, m):
        try:
            env = envelope.validate(m.message)
        except envelope.EnvelopeError as exc:
            return self._drop(m, "bad", exc)
        if env["kind"] != "query":
            return self._drop(m, "bad", "kind %s is not a query" % env["kind"])
        if env["reply_to"] not in (queues.GW_REPLY, queues.CHECK_REPLY):
            return self._drop(m, "bad", "reply_to not allowed")
        if env["deadline"]:
            try:
                if datetime.fromisoformat(env["deadline"]) < datetime.now(timezone.utc):
                    return self._drop(m, "late", "past its deadline")
            except ValueError:
                pass
        fn = self.handlers.get(env["type"])
        code, source, body, ms, failed = 400, None, UNKNOWN, 0, False
        if fn is not None:
            t0 = time.perf_counter()
            try:
                with self._pool.conn() as con:
                    qctx = SimpleNamespace(con=con, sender=env["sender"], deadline=env["deadline"], envelope=env)
                    code, source, body = fn(qctx, env["payload"])
            except Exception as exc:
                failed = True
                code, source, body = 500, None, FAILED
                _log("answerer %s failed: %s: %s" % (env["type"], type(exc).__name__, str(exc)[:160]))
            ms = int(round((time.perf_counter() - t0) * 1000))
        text = dump(body)
        ref = None
        if len(text.encode("utf-8")) > BODY_INLINE_MAX:
            ref = "work/results/q-%s.json" % env["msg_id"]
            self._write(ref, text)
            text = None
            with self._lock:
                self._stats["refs"] += 1
        answer = envelope.new("reply", env["type"],
                              {"status": "ok", "code": code, "source": source, "body": text, "body_ref": ref,
                               "handler_ms": ms, "reply_tag": env["payload"].get("reply_tag")},
                              sender=self.service.name, correlation_id=env["correlation_id"], in_reply_to=env["msg_id"],
                              read_ct=m.read_ct, deadline=env["deadline"])
        with self._pool.conn() as con:
            with con.transaction():
                pgmq.send(con, env["reply_to"], answer)
                pgmq.delete(con, self.queue, m.pgmq_id)
        with self._lock:
            self._stats["errors" if failed else "answered"] += 1
            self._ms.append(ms)
        slog.log("query.done", kind="query", level="error" if failed else "info", correlation_id=env["correlation_id"],
                 type=env["type"], status="error" if failed else ("ok" if code < 400 else "refused"), ms=ms, n=code)
        if ms > 500 or failed:
            _log("%s q %s %d %d ms" % (self.service.name, env["type"], code, ms))

    @staticmethod
    def _write(rel, text):
        path = os.path.join(config.ROOT, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)

    # ---- reporting -------------------------------------------------------------------------------------------------
    def stats(self):
        with self._lock:
            ms = sorted(self._ms)
            out = dict(self._stats, state=self._state, threads=self.threads,
                       p50_ms=ms[len(ms) // 2] if ms else None)
        return out

    def depths(self):
        now = time.monotonic()
        if now - self._depth_t >= 5.0 or not self._depths:
            self._depth_t = now
            try:
                with self._pool.conn() as con:
                    rows = pgmq.metrics_all(con)
                self._depths = {r["queue_name"]: {"waiting": int(r["queue_length"]),
                                                  "visible": int(r["queue_visible_length"])}
                                for r in rows if r["queue_name"] == self.queue}
            except Exception:
                pass
        return dict(self._depths)
