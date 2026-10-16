"""A tiny connection pool (psycopg_pool is not in the venv). Connections are made when first needed, never more
than `size` are open at once, and a broken connection is thrown away and replaced on the next checkout.

Idle trimming: a connection that sits idle longer than `max_idle` seconds is closed, oldest first, but
`min_idle` idle ones are kept (default 0: a quiet service holds only its own waiter connection). Without it a pool keeps
every connection it has ever opened, and several running sets can use up Postgres's connection limit while idle. Trimming runs on checkout, on give-back and from one daemon timer
thread per pool (wakes every `tick` seconds); it never touches a checked-out connection. Reuse stays LIFO, so a hot
connection is reused and the cold ones age out. `max_idle` defaults to 60 s; env BC_POOL_MAX_IDLE_S overrides it, 0 turns
trimming off (the old behaviour)."""
import os
import queue
import threading
import time
from contextlib import contextmanager

import psycopg

from framework.common import pg

DEFAULT_MAX_IDLE_S = 60.0
DEFAULT_TICK_S = 15.0


def _env_max_idle():
    try:
        return float(os.environ.get("BC_POOL_MAX_IDLE_S", DEFAULT_MAX_IDLE_S))
    except ValueError:
        return DEFAULT_MAX_IDLE_S


class Pool:
    def __init__(self, size, connect=pg.connect, max_idle=None, min_idle=0, tick=DEFAULT_TICK_S):
        self.size = size
        self._connect = connect
        self.max_idle = _env_max_idle() if max_idle is None else float(max_idle)
        self.min_idle = min_idle
        self._tick = tick
        self._idle = []                      # [(connection, time it was given back)], oldest first, LIFO reuse
        self._cond = threading.Condition()
        self._open = 0
        self._stop = threading.Event()
        self._timer = None

    # trimming ------------------------------------------------------------------------------------------------------
    def _expired_locked(self, now):
        """Take the connections idle longer than max_idle (oldest first, keeping min_idle) out of the idle list.
        The caller holds the lock and closes the returned connections after releasing it."""
        if self.max_idle <= 0:
            return []
        out = []
        while len(self._idle) > self.min_idle and now - self._idle[0][1] > self.max_idle:
            out.append(self._idle.pop(0)[0])
            self._open -= 1
        if out:
            self._cond.notify_all()
        return out

    @staticmethod
    def _close_trimmed(cons):
        for con in cons:
            try:
                con.close()
            except Exception:
                pass

    def trim(self):
        """Close the connections that have been idle too long. Returns how many were closed."""
        with self._cond:
            gone = self._expired_locked(time.monotonic())
        self._close_trimmed(gone)
        return len(gone)

    def _run_timer(self):
        while not self._stop.wait(self._tick):
            self.trim()

    def _start_timer(self):
        # called with the lock held; one daemon thread per pool, only when trimming is on
        if self._timer is None and self.max_idle > 0:
            self._timer = threading.Thread(target=self._run_timer, name="pgpool-trim", daemon=True)
            self._timer.start()

    # checkout / give-back ------------------------------------------------------------------------------------------
    def _checkout(self, timeout):
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                gone = self._expired_locked(time.monotonic())
                if gone:
                    self._cond.release()
                    try:
                        self._close_trimmed(gone)
                    finally:
                        self._cond.acquire()
                if self._idle:
                    return self._idle.pop()[0]
                if self._open < self.size:
                    self._open += 1
                    self._start_timer()
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    raise queue.Empty
                self._cond.wait(left)
        try:
            return self._connect()
        except Exception:
            with self._cond:
                self._open -= 1
                self._cond.notify()
            raise

    def _discard(self, con):
        try:
            con.close()
        except Exception:
            pass
        with self._cond:
            self._open -= 1
            self._cond.notify()

    @contextmanager
    def conn(self, timeout=30.0):
        con = self._checkout(timeout)
        if con.closed or con.broken:
            self._discard(con)
            con = self._checkout(timeout)
        try:
            yield con
        except psycopg.OperationalError:
            self._discard(con)
            raise
        except BaseException:
            self._give_back(con)
            raise
        else:
            self._give_back(con)

    def _give_back(self, con):
        if con.closed or con.broken:
            self._discard(con)
            return
        try:
            if con.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                con.rollback()
        except Exception:
            self._discard(con)
            return
        with self._cond:
            now = time.monotonic()
            self._idle.append((con, now))
            gone = self._expired_locked(now)
            self._cond.notify()
        self._close_trimmed(gone)

    def close(self):
        self._stop.set()
        with self._cond:
            cons = [c for c, _ in self._idle]
            self._idle.clear()
            self._open -= len(cons)
            self._cond.notify_all()
        self._close_trimmed(cons)
