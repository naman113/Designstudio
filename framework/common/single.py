"""One at a time across every replica: a named Postgres advisory lock on its own connection.

    with single.hold("catalog.db", ctx=ctx, service="catalogue", replica=2, envelope=e):
        ...   # nothing else holding "catalog.db" runs anywhere until this block ends

The lock is pg_try_advisory_lock(675, hashtext('single:<resource>')) (class 675 = these locks; the dispatcher's
one-argument lock lives in another key space). It is held by a session of its own, so a replica that dies frees it as
soon as Postgres drops that connection. Each finished hold writes one bus.single_runs row (database clock), which is how
the checks prove that two holds of one resource never overlap.

Inside one process the resource's own `threading.Lock` is taken first, so at most one thread per process waits on the
database and a replica holds at most one lock connection per resource.
"""
import os
import threading
import time
from contextlib import contextmanager

from framework.common import pg

LOCK_CLASS = 675
POLL_S = 0.2

_LOCAL = {}
_LOCAL_GUARD = threading.Lock()


def _local(resource):
    with _LOCAL_GUARD:
        lock = _LOCAL.get(resource)
        if lock is None:
            lock = _LOCAL[resource] = threading.Lock()
        return lock


def _log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


@contextmanager
def hold(resource, *, ctx=None, service="cli", replica=0, envelope=None, poll_s=POLL_S):
    """Hold `single:<resource>` for the block. `ctx.check()` is called between tries, so a Stop or a time-out raises
    (consumer.Stopped) while waiting. Always writes one bus.single_runs row on a normal end or an error inside the block."""
    t0 = time.monotonic()
    key = "single:" + resource
    local = _local(resource)
    while not local.acquire(timeout=poll_s):
        if ctx is not None:
            ctx.check()
    con = None
    try:
        con = pg.connect(write=True, app="buildcore single %s %s r%s pid %d" % (resource, service, replica, os.getpid()))
        while True:
            got, at = con.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s)), clock_timestamp()",
                                  (LOCK_CLASS, key)).fetchone()
            if got:
                break
            if ctx is not None:
                ctx.check()
            time.sleep(poll_s)
        acquired_at = at
        waited_ms = round((time.monotonic() - t0) * 1000)
        try:
            yield
        finally:
            try:
                released_at = con.execute("SELECT clock_timestamp()").fetchone()[0]
                con.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", (LOCK_CLASS, key))
                e = envelope or {}
                con.execute(
                    "INSERT INTO bus.single_runs (resource, service, replica, pid, job_id, msg_id, type, waited_ms, "
                    "acquired_at, released_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (resource, service, int(replica), os.getpid(), e.get("job_id"), e.get("msg_id"), e.get("type"),
                     waited_ms, acquired_at, released_at))
            except Exception as exc:                       # the record is evidence, never a reason to fail the job
                _log("single %s: could not record the hold: %s: %s" % (resource, type(exc).__name__, exc))
    finally:
        try:
            if con is not None:
                con.close()
        finally:
            local.release()
