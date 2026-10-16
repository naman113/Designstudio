"""The client side of a page read: send one query, wait for its answer.

    asker = Asker()                                   # the gateway; a check uses Asker(reply_to="svc_check_reply", sender="cli")
    a = asker.ask("catalogue.q.product", {"product_id": "flange"})     # -> Answer(code, source, text, handler_ms, ms)

`text` is the exact JSON text the service produced (the studio sends it as it is). The answer's reader thread uses a
conditional read, `{"payload": {"reply_tag": TAG}}`, so two askers on one database (a second studio, a check) each read only
their own answers. Answers whose asker died are swept after 60 s. A page read that has no answer in time raises
`Unavailable` (5 s; 8 s for the search and the catalogue page's search, which run with a 5 s statement timeout).
"""
import os
import secrets
import threading
import time
import uuid
from collections import deque, namedtuple
from datetime import datetime, timedelta, timezone

import psycopg

from framework.common import config, envelope, pgmq, queues, slog
from framework.common.pgpool import Pool

Answer = namedtuple("Answer", "code source text handler_ms ms")
DEFAULT_TIMEOUT_S = 5.0
TIMEOUT_S = {"catalogue.q.search": 8.0, "catalogue.q.browse": 8.0}   # the catalogue page's search, as the search box
SWEEP_EVERY_S = 30.0
ORPHAN_AGE_S = 60
FILE_RETRY_S = 3.0


class Unavailable(Exception):
    """The service did not answer in time, or the queue could not be reached."""


def _read_body_file(rel):
    """The exact text of a body sent by file (utf-8), retrying a transient Windows access-denied for 3 s; the file is
    deleted afterwards."""
    path = os.path.join(config.ROOT, *rel.split("/"))
    end = time.monotonic() + FILE_RETRY_S
    while True:
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                text = fh.read()
            break
        except OSError as exc:
            denied = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32)
            if not denied or time.monotonic() >= end:
                raise
            time.sleep(0.02)
    try:
        os.remove(path)
    except OSError:
        pass
    return text


class Asker:
    def __init__(self, *, reply_to=queues.GW_REPLY, sender="gateway", pool=2, queue=queues.CATALOGUE_Q):
        self.reply_to = reply_to
        self.sender = sender
        self.queue = queue
        self.TAG = "%s-%d-%s" % (sender, os.getpid(), secrets.token_hex(3))
        self._pool = Pool(pool)
        self._lock = threading.Lock()
        self._pending = {}                   # correlation_id -> [Event, Answer-or-None]
        self._reader = None
        self._stop = threading.Event()
        self._stats = {"asked": 0, "answered": 0, "timeouts": 0, "late": 0, "orphans": 0}
        self._ms = deque(maxlen=200)

    # ---- asking ----------------------------------------------------------------------------------------------------
    def ask(self, qtype, params=None, timeout_s=None):
        timeout_s = timeout_s if timeout_s is not None else TIMEOUT_S.get(qtype, DEFAULT_TIMEOUT_S)
        self._start_reader()
        t0 = time.perf_counter()
        cid = str(uuid.uuid4())
        slot = [threading.Event(), None]
        with self._lock:
            self._pending[cid] = slot            # registered BEFORE the send: a fast answer cannot be missed
            self._stats["asked"] += 1
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=timeout_s)).isoformat(timespec="milliseconds")
        env = envelope.new("query", qtype, dict(params or {}, reply_tag=self.TAG), sender=self.sender,
                           correlation_id=cid, reply_to=self.reply_to, deadline=deadline)
        try:
            with self._pool.conn() as con:
                pgmq.send(con, self.queue, env)
        except psycopg.Error as exc:
            self._forget(cid, timeout=False)
            slog.log("query.answered", kind="query", level="error", correlation_id=cid, type=qtype, status="unavailable",
                     exc=type(exc).__name__)
            raise Unavailable("the catalogue queue could not be reached: %s" % type(exc).__name__)
        slog.log("query.sent", kind="query", correlation_id=cid, type=qtype, queue=self.queue)
        if not slot[0].wait(timeout_s):
            self._forget(cid, timeout=True)
            slog.log("query.answered", kind="query", level="warn", correlation_id=cid, type=qtype, status="timeout",
                     ms=round((time.perf_counter() - t0) * 1000))
            raise Unavailable("no answer to %s in %.0f s" % (qtype, timeout_s))
        answer = slot[1]
        total = int(round((time.perf_counter() - t0) * 1000))
        slog.log("query.answered", kind="query", correlation_id=cid, type=qtype, status="ok", ms=total)
        with self._lock:
            self._pending.pop(cid, None)
            self._stats["answered"] += 1
            self._ms.append(total)
        return answer._replace(ms=total)

    def _forget(self, cid, timeout):
        with self._lock:
            self._pending.pop(cid, None)
            if timeout:
                self._stats["timeouts"] += 1

    # ---- the reader ------------------------------------------------------------------------------------------------
    def _start_reader(self):
        with self._lock:
            if self._reader is not None:
                return
            self._reader = threading.Thread(target=self._read_loop, name="asker-%s" % self.TAG, daemon=True)
            self._reader.start()

    def _read_loop(self):
        waiter = None
        last_sweep = time.monotonic()
        cond = {"payload": {"reply_tag": self.TAG}}
        while not self._stop.is_set():
            try:
                if waiter is None:
                    waiter = pgmq.Waiter()
                    waiter.listen(self.reply_to)
                got = waiter.read_first([(self.reply_to, queues.VT_S[self.reply_to], 100, cond)], 1.0)
                if got:
                    ids = []
                    for m in got[1]:
                        ids.append(m.pgmq_id)
                        self._deliver(m)
                    with self._pool.conn() as con:
                        con.execute("SELECT pgmq.delete(%s, %s::bigint[])", (self.reply_to, ids))
                if time.monotonic() - last_sweep >= SWEEP_EVERY_S:
                    last_sweep = time.monotonic()
                    self._sweep()
            except Exception as exc:
                print("asker %s: %s: %s" % (self.TAG, type(exc).__name__, str(exc)[:160]), flush=True)
                try:
                    if waiter is not None:
                        waiter.close()
                except Exception:
                    pass
                waiter = None
                time.sleep(0.5)
        if waiter is not None:
            waiter.close()

    def _deliver(self, m):
        try:
            env = envelope.validate(m.message)
        except envelope.EnvelopeError:
            return
        p = env["payload"]
        with self._lock:
            slot = self._pending.get(env["correlation_id"])
            if slot is None:
                self._stats["late"] += 1
        if slot is None:
            if p.get("body_ref"):
                try:
                    os.remove(os.path.join(config.ROOT, *p["body_ref"].split("/")))
                except OSError:
                    pass
            return
        text = p.get("body")
        if p.get("body_ref"):
            text = _read_body_file(p["body_ref"])
        slot[1] = Answer(int(p.get("code") or 0), p.get("source"), text, int(p.get("handler_ms") or 0), 0)
        slot[0].set()

    def _sweep(self):
        # the queue name comes from `queues`, never from input
        assert self.reply_to in queues.REPLY_TO_ALLOWED
        with self._pool.conn() as con:
            cur = con.execute("DELETE FROM pgmq.q_%s WHERE enqueued_at < clock_timestamp() - interval '%d seconds'"
                              % (self.reply_to, ORPHAN_AGE_S))
            n = cur.rowcount
        if n:
            with self._lock:
                self._stats["orphans"] += n

    # ---- reporting -------------------------------------------------------------------------------------------------
    def stats(self):
        with self._lock:
            ms = sorted(self._ms)
            return dict(self._stats, p50_ms=ms[len(ms) // 2] if ms else None, tag=self.TAG)

    def close(self):
        self._stop.set()
        self._pool.close()


_default = None
_default_lock = threading.Lock()


def default():
    """The process-wide Asker of the gateway, created on first use."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Asker()
        return _default
