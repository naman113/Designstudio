"""The studio's client for jobs it waits on.

    from framework.web import job_client
    data = job_client.run("geometry.mesh", {"product_id": pid}, timeout_s=700)

`run` sends the job to the orchestrator (a message on `orch_in`), waits until it reaches a final state and
returns its result, unpacked (framework/common/results.py: no nested jsonb numbers). One shared listener thread holds a
single LISTEN `job_events` connection for every waiting request, so a hundred waiting requests cost one connection; each
waiter also re-reads its job once a second, so a missed NOTIFY costs at most a second.

Imports only framework.common (the gateway never imports engines, catalogue or orchestrator code for these jobs).
The engine job types are `dual.mesh` / `dual.step_out`; the callers pass the type, so only the call sites' constants
name them.

Copy-safe: the listener and the pool belong to this process; no state is shared with other processes but the
database, and a job's result is read from its row or its result file, never from memory.
"""
import json
import threading
import time
import uuid

import psycopg

from framework.common import jobs, pg, results
from framework.common.pgpool import Pool

_POOL = Pool(2, connect=lambda: pg.connect(write=True))


class JobError(Exception):
    pass


class JobFailed(JobError):
    """The job reached a final state other than `succeeded`."""

    def __init__(self, code, user_text, state):
        super().__init__("%s: %s (%s)" % (code, user_text, state))
        self.code, self.user_text, self.state = code, user_text, state


class JobUnavailable(JobError):
    """The job service (Postgres, the orchestrator's queue) could not be reached."""


class _Listener:
    def __init__(self):
        self._lock = threading.Lock()
        self._waiters = {}                     # job_id -> threading.Event
        self.ready = threading.Event()
        threading.Thread(target=self._loop, name="job-client-listener", daemon=True).start()

    def register(self, job_id):
        ev = threading.Event()
        with self._lock:
            self._waiters[job_id] = ev
        return ev

    def forget(self, job_id):
        with self._lock:
            self._waiters.pop(job_id, None)

    def _wake(self, job_id):
        with self._lock:
            ev = self._waiters.get(job_id)
        if ev is not None:
            ev.set()

    def _loop(self):
        con = None
        while True:
            try:
                if con is None:
                    con = pg.connect()
                    con.execute("LISTEN job_events")
                    self.ready.set()
                for n in con.notifies(timeout=1.0, stop_after=50):
                    try:
                        body = json.loads(n.payload)
                    except ValueError:
                        continue
                    if body.get("st") in jobs.FINAL and body.get("j"):
                        self._wake(body["j"])
            except psycopg.OperationalError:
                self.ready.clear()
                try:
                    if con is not None:
                        con.close()
                except Exception:
                    pass
                con = None
                time.sleep(1.0)
            except Exception:
                time.sleep(1.0)


_LISTENER = None
_LISTENER_LOCK = threading.Lock()


def _listener():
    global _LISTENER
    with _LISTENER_LOCK:
        if _LISTENER is None:
            _LISTENER = _Listener()
        ln = _LISTENER
    ln.ready.wait(5.0)
    return ln


def conn():
    """A pooled connection for reading job rows (the studio follows an upload's steps); use as `with conn() as con`."""
    return _POOL.conn()


def submit(type, payload=None):
    """Send a job and do not wait. -> job_id."""
    job_id = str(uuid.uuid4())
    try:
        with _POOL.conn() as con:
            jobs.submit(con, type, payload or {}, sender="gateway", job_id=job_id)
    except (psycopg.Error, RuntimeError) as exc:
        raise JobUnavailable(exc.__class__.__name__)
    return job_id


def run(type, payload, *, timeout_s):
    """Send a job, wait for its end, return its result. Raises JobFailed (a final state other than succeeded),
    JobUnavailable (the database does not answer) or TimeoutError."""
    ln = _listener()
    job_id = str(uuid.uuid4())
    ev = ln.register(job_id)               # before the submit, so an instant answer is not missed
    try:
        try:
            with _POOL.conn() as con:
                jobs.submit(con, type, payload or {}, sender="gateway", job_id=job_id)
        except (psycopg.Error, RuntimeError) as exc:
            raise JobUnavailable(exc.__class__.__name__)
        end = time.monotonic() + timeout_s
        while True:
            ev.wait(min(1.0, max(0.0, end - time.monotonic())))
            ev.clear()
            try:
                with _POOL.conn() as con:
                    job = jobs.get(con, job_id, with_result=True)
            except (psycopg.Error, RuntimeError) as exc:
                raise JobUnavailable(exc.__class__.__name__)
            if job is not None and job["state"] in jobs.FINAL:
                break
            if time.monotonic() >= end:
                raise TimeoutError("job %s (%s) did not finish within %s s" % (job_id, type, timeout_s))
        if job["state"] == "succeeded":
            return results.unpack(job.get("result"))
        raise JobFailed(job.get("error_code"), job.get("user_text"), job["state"])
    finally:
        ln.forget(job_id)
