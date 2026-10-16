"""The pgmq client for the services.

Stateless calls on the caller's connection: the connection is held by the caller (a connect costs about 30 ms), and no
function here commits or rolls back (the caller owns the transaction, so an effect, a reply and an ack can commit
together). `Waiter` is the one queue connection of a process: it LISTENs on the queues' INSERT
channels and reads in priority order.

Visibility timeout (`vt`, whole seconds): a message read but not archived or deleted becomes visible again after
`vt` and is redelivered; `read_ct` counts the deliveries.

pgmq pitfalls handled here: always the 4-argument `pgmq.send(q, msg, headers, delay)` (the 3-argument form is
ambiguous), explicit casts (`::bigint`, `::integer`, `::bigint[]`), and a quoted identifier for LISTEN.
"""
import json
import time
from dataclasses import dataclass

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from framework.common import pg

MAX_MESSAGE_BYTES = 256 * 1024          # bigger data goes by reference (a path or a row id)


@dataclass
class Message:
    pgmq_id: int
    read_ct: int
    enqueued_at: object
    vt: object
    message: dict
    headers: object = None


def channel(queue):
    """The NOTIFY channel pgmq uses for inserts into `queue` (the payload is empty: always read after waking)."""
    return "pgmq.q_%s.INSERT" % queue


def installed(con):
    """True when the pgmq schema is present in the database of `con`."""
    return con.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'pgmq'").fetchone() is not None


def list_queues(con):
    return [r[0] for r in con.execute("SELECT queue_name FROM pgmq.list_queues() ORDER BY 1").fetchall()]


def create(con, queue, notify=True):
    """Create the queue if missing (True when newly created). With `notify`, inserts NOTIFY at once (throttle 0): pgmq's
    default 250 ms throttle would put a 250 ms floor on every hop."""
    new = con.execute("SELECT 1 FROM pgmq.meta WHERE queue_name = %s", (queue,)).fetchone() is None
    if new:
        con.execute("SELECT pgmq.create(%s)", (queue,))
    if notify:
        row = con.execute("SELECT throttle_interval_ms FROM pgmq.notify_insert_throttle WHERE queue_name = %s",
                          (queue,)).fetchone()
        if row is None or row[0] != 0:
            con.execute("SELECT pgmq.enable_notify_insert(%s, 0)", (queue,))
    return new


def drop(con, queue):
    con.execute("SELECT pgmq.drop_queue(%s)", (queue,))


def send(con, queue, message, delay_s=0):
    """Enqueue one JSON-able message (at most 256 KB serialized); returns its pgmq_id."""
    if len(json.dumps(message)) > MAX_MESSAGE_BYTES:
        raise ValueError("message too large (over %d bytes): send a reference" % MAX_MESSAGE_BYTES)
    return con.execute("SELECT pgmq.send(%s, %s, NULL, %s::integer)", (queue, Jsonb(message), int(delay_s))).fetchone()[0]


def read(con, queue, vt_s, qty=1, conditional=None):
    """Take up to `qty` visible messages, hiding them for `vt_s` seconds. `conditional` is a JSON filter on the message
    body (`{"job_id": "..."}`). Returns [Message]."""
    if conditional:
        rows = con.execute("SELECT msg_id, read_ct, enqueued_at, vt, message, headers "
                           "FROM pgmq.read(%s, %s::integer, %s::integer, %s::jsonb)",
                           (queue, int(vt_s), int(qty), Jsonb(conditional))).fetchall()
    else:
        rows = con.execute("SELECT msg_id, read_ct, enqueued_at, vt, message, headers "
                           "FROM pgmq.read(%s, %s::integer, %s::integer)", (queue, int(vt_s), int(qty))).fetchall()
    # pgmq.read is an UPDATE ... RETURNING over a joined subquery, so the rows can come back in any order even though the
    # subquery picks the oldest first. Sort by pgmq_id: a progress message must be applied before the reply sent after it.
    return sorted((Message(*r) for r in rows), key=lambda m: m.pgmq_id)


def archive(con, queue, pgmq_id):
    """Move a handled message to the queue's archive. False when it no longer exists (someone withdrew it)."""
    return bool(con.execute("SELECT pgmq.archive(%s, %s::bigint)", (queue, int(pgmq_id))).fetchone()[0])


def delete(con, queue, pgmq_id):
    """Remove a message without keeping it (the orchestrator's withdraw). False when it does not exist."""
    return bool(con.execute("SELECT pgmq.delete(%s, %s::bigint)", (queue, int(pgmq_id))).fetchone()[0])


def set_vt(con, queue, pgmq_id, vt_s):
    """Set the visibility of one message or a list, `vt_s` seconds from now. Returns the rows found (0 = gone)."""
    if isinstance(pgmq_id, (list, tuple)):
        rows = con.execute("SELECT msg_id FROM pgmq.set_vt(%s, %s::bigint[], %s::integer)",
                           (queue, [int(i) for i in pgmq_id], int(vt_s))).fetchall()
    else:
        rows = con.execute("SELECT msg_id FROM pgmq.set_vt(%s, %s::bigint, %s::integer)",
                           (queue, int(pgmq_id), int(vt_s))).fetchall()
    return len(rows)


def metrics_all(con):
    """[{queue_name, queue_length, queue_visible_length, total_messages, oldest_msg_age_sec}] for every queue."""
    rows = con.execute("SELECT queue_name, queue_length, queue_visible_length, total_messages, oldest_msg_age_sec "
                       "FROM pgmq.metrics_all() ORDER BY 1").fetchall()
    keys = ("queue_name", "queue_length", "queue_visible_length", "total_messages", "oldest_msg_age_sec")
    return [dict(zip(keys, r)) for r in rows]


class Waiter:
    """The queue connection of one process: LISTEN on the INSERT channels, read in priority order, wake on NOTIFY.

    Never shared with worker threads. Re-reads at least every 1 s even without a NOTIFY: delayed sends (retries), nacks
    and visibility expiry send none, and a reconnect loses NOTIFYs. On `psycopg.OperationalError` it closes, sleeps 1 s,
    reconnects, LISTENs again and carries on.
    """

    def __init__(self, connect=pg.connect):
        self._connect = connect
        self._listening = []
        self._subs = {}                  # broadcast channel -> fn(payload)
        self.con = None
        self.reconnects = 0
        self._open()

    def _open(self):
        self.con = self._connect()
        for q in self._listening:
            self._listen_one(q)
        for ch in self._subs:
            self._listen_channel(ch)

    def _listen_one(self, queue):
        self.con.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel(queue))))

    def _listen_channel(self, name):
        self.con.execute(sql.SQL("LISTEN {}").format(sql.Identifier(name)))

    def subscribe(self, channel_name, fn):
        """LISTEN on a plain NOTIFY channel and call `fn(payload)` for every notification (from `wait` and `poll`).
        Used by the Stop broadcast `stop_<service>`: it reaches every replica."""
        self._subs[channel_name] = fn
        try:
            self._listen_channel(channel_name)
        except psycopg.OperationalError:
            self._reopen()

    def _route(self, n):
        fn = self._subs.get(n.channel)
        if fn is not None:
            try:
                fn(n.payload)
            except Exception as exc:
                print("waiter: handler of %s failed: %s: %s" % (n.channel, type(exc).__name__, exc), flush=True)

    def poll(self):
        """Handle the notifications that already arrived, without waiting."""
        try:
            for n in self.con.notifies(timeout=0):
                self._route(n)
        except psycopg.OperationalError:
            self._reopen()

    def _reopen(self):
        try:
            if self.con is not None:
                self.con.close()
        except Exception:
            pass
        self.con = None
        while self.con is None:
            time.sleep(1.0)
            try:
                self._open()
                self.reconnects += 1
            except psycopg.OperationalError:
                self.con = None

    def listen(self, *queues):
        for q in queues:
            if q not in self._listening:
                self._listening.append(q)
                try:
                    self._listen_one(q)
                except psycopg.OperationalError:
                    self._reopen()

    def wait(self, timeout_s):
        """Wait for a NOTIFY on any listened queue. True when one arrived."""
        try:
            got = False
            for n in self.con.notifies(timeout=max(0.0, float(timeout_s)), stop_after=1):
                got = True
                self._route(n)
            return got
        except psycopg.OperationalError:
            self._reopen()
            return False

    def read_first(self, queues, wait_s=1.0):
        """`queues` is [(queue, vt_s, qty), ...] in priority order. Returns (queue, [Message]) for the first queue with
        messages, or None after `wait_s` seconds (0 = look once).
        A 4th element is a pgmq conditional (message @> conditional), used by the page-read askers."""
        end = time.monotonic() + wait_s
        while True:
            try:
                for spec in queues:
                    q, vt_s, qty = spec[:3]
                    if qty <= 0:
                        continue
                    got = read(self.con, q, vt_s, qty, spec[3] if len(spec) > 3 else None)
                    if got:
                        return q, got
            except psycopg.OperationalError:
                self._reopen()
                continue
            left = end - time.monotonic()
            if left <= 0:
                return None
            self.wait(min(left, 1.0))

    def close(self):
        try:
            if self.con is not None:
                self.con.close()
        except Exception:
            pass
        self.con = None
