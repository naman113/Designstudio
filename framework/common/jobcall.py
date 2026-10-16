"""A command asks another service for a job and waits for its answer.

    from framework.common import jobcall
    rec = jobcall.call("geometry.measure_file", {"path": "work/sessions/<sid>/out/part.step"})

`call` sends the job to the orchestrator (a message on `orch_in`), waits until it reaches a final state and returns the
job's result, unpacked: the JSON text the service made, or the file it wrote under work/results/ (framework/common/
results.py). A result never travels as nested jsonb numbers (jsonb turns -0.0 into 0): `result_file` answers, a file
written with json.dump, are read back with `read_result_file`, so every number keeps its bytes.

The parent and the correlation default to the caller's environment (BC_PARENT_JOB_ID, BC_CORRELATION_ID), so the job shows
under its parent on the Activity page.

Only framework.common is imported (a command never imports the other services). It never prints. psycopg is
imported inside the functions, so a bare system Python can import this module and gets a plain `Unavailable` instead.
"""
import json
import os
import time

from framework.common import config

UNAVAILABLE = "The services are not available right now."


class JobError(Exception):
    pass


class JobFailed(JobError):
    """The job reached a final state other than `succeeded`."""

    def __init__(self, code, user_text, state="failed"):
        super().__init__("%s: %s (%s)" % (code, user_text, state))
        self.code, self.user_text, self.state = code, user_text, state


class Cancelled(JobError):
    """The caller's `cancelled` event was set while it waited (a Stop of the job that asked)."""


class Unavailable(JobError):
    """Postgres or the orchestrator's queue could not be reached, or the job did not finish in time."""

    def __init__(self, user_text=UNAVAILABLE):
        super().__init__(user_text)
        self.user_text = user_text


def submit_and_wait(type, payload, *, sender_name, parent_job_id=None, correlation_id=None, timeout_s=900,
                    on_submit=None, cancelled=None, deadline=None):
    """-> (job_id, the final job row, its unpacked result). Raises JobFailed, Unavailable. `on_submit(job_id)` is called
    right after the job is sent (a caller that follows the job's events starts here). `deadline` (ISO time):
    the job's own deadline; a service answers an expired command `stopped` without running it."""
    try:
        import psycopg
        from framework.common import jobs, pg, results
    except ImportError as exc:
        raise Unavailable() from exc
    con = None
    try:
        con = pg.connect(write=True)
        job_id = jobs.submit(
            con, type, payload, sender=sender_name,
            correlation_id=correlation_id or os.environ.get("BC_CORRELATION_ID") or None,
            parent_job_id=parent_job_id or os.environ.get("BC_PARENT_JOB_ID") or None, deadline=deadline)
        if on_submit is not None:
            on_submit(job_id)
        if cancelled is None:
            row = jobs.wait(con, job_id, timeout_s=timeout_s)
        else:                                     # 106-02: wait in one-second slices so a Stop is seen at once
            end = time.monotonic() + timeout_s
            while True:
                if cancelled.is_set():
                    raise Cancelled()
                left = end - time.monotonic()
                try:
                    row = jobs.wait(con, job_id, timeout_s=max(0.05, min(1.0, left)))
                    break
                except TimeoutError:
                    if left <= 1.0:
                        raise
        if row["state"] != "succeeded":
            raise JobFailed(row.get("error_code") or row["state"],
                            row.get("user_text") or "This could not be done right now.", row["state"])
        return job_id, row, results.unpack(jobs.get(con, job_id, with_result=True)["result"])
    except (TimeoutError, psycopg.Error, OSError, RuntimeError, KeyError, TypeError, ValueError) as exc:
        raise Unavailable() from exc
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def call(type, payload, *, sender="designer", parent_job_id=None, correlation_id=None, timeout_s=900, cancelled=None):
    """Submit `type` with `payload`, wait, return the unpacked result (a dict). BC_SENDER in the environment wins over
    `sender` (a person's command line passes "cli")."""
    return submit_and_wait(type, payload, sender_name=os.environ.get("BC_SENDER") or sender, parent_job_id=parent_job_id,
                           correlation_id=correlation_id, timeout_s=timeout_s, cancelled=cancelled)[2]


def submit(type, payload, *, sender="designer", parent_job_id=None, correlation_id=None):
    """Send a job and do not wait (a caller that follows the job's rows itself starts here). -> the job id."""
    try:
        import psycopg
        from framework.common import jobs, pg
    except ImportError as exc:
        raise Unavailable() from exc
    con = None
    try:
        con = pg.connect(write=True)
        return jobs.submit(con, type, payload, sender=os.environ.get("BC_SENDER") or sender,
                           correlation_id=correlation_id or os.environ.get("BC_CORRELATION_ID") or None,
                           parent_job_id=parent_job_id or os.environ.get("BC_PARENT_JOB_ID") or None)
    except (psycopg.Error, OSError, RuntimeError, ValueError) as exc:
        raise Unavailable() from exc
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def stop(job_id, *, sender="designer"):
    """Ask the orchestrator to stop a job this command sent (a pair check still running when the command has to
    answer, so it does not hold a geometry worker after its caller has gone). Idempotent (jobs.stop)."""
    try:
        import psycopg
        from framework.common import jobs, pg
    except ImportError as exc:
        raise Unavailable() from exc
    con = None
    try:
        con = pg.connect(write=True)
        jobs.stop(con, job_id, sender=os.environ.get("BC_SENDER") or sender)
    except (psycopg.Error, OSError, RuntimeError, ValueError) as exc:
        raise Unavailable() from exc
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def read_result_file(res):
    """The file a service wrote with json.dump (`{"result_file": "work/results/<job>.<op>.json", ...}`), read back with
    json.load: floats keep their bytes, -0.0 stays -0.0. The path must be a file directly under work/results/."""
    ref = res.get("result_file") if isinstance(res, dict) else None
    if not isinstance(ref, str):
        raise Unavailable("The answer could not be read.")
    folder = os.path.normpath(os.path.join(config.ROOT, "work", "results"))
    path = os.path.normpath(os.path.join(config.ROOT, ref))
    if os.path.dirname(path) != folder or not os.path.isfile(path):
        raise Unavailable("The answer could not be read.")
    with open(path, encoding="utf-8", newline="") as fh:
        return json.load(fh)
