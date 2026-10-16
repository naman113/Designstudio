"""The studio's drawing routes on the gateway.

    POST /api/drawings/{pid}/make     start the drawings of a design (or answer the one already running)  ->  {job_id, state}
    POST /api/drawings/{pid}/stop     stop the design's open drawings job (a queued one ends at once; the last published
                                       set stays)  ->  {ok, state}
    GET  /api/drawings/{pid}/status   {state none|making|current|stale|failed, stale, stale_reason, made_at, revision, progress,
                                       job_id, failed_parts [{ref, why}], sheets, sizes, user_text, set, previous}

The page's list of sheets (GET /api/drawings/{pid}) is the catalogue's `catalogue.q.drawings`, in app.py. The set's facts
(made_at, revision, sizes) come from `catalogue.q.drawings_state`; the running job is read from orch.jobs here, server side,
and only the fields above leave (no payload, no result, no developer text). One open
`drawings.make` per design is reused.
"""
import json
import re
import threading
import time
from datetime import datetime, timezone

import psycopg
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from starlette.concurrency import run_in_threadpool

from framework.common import jobs
from framework.web import app as studio
from framework.web import job_client

router = APIRouter()
_RECENT = {}                                      # pid -> (job_id, monotonic): a second press before the job row exists
_RECENT_LOCK = threading.Lock()
RECENT_S = 5.0
OPEN = ("queued", "dispatched", "running", "stopping")
FAILED_TEXT = "The drawings could not be made."
UNAVAILABLE = "The drawings are not available right now."
STEPS_OF = 5                                      # bom, overview, publish_overview, parts, publish_parts (the assembly is made inside the overview step)
PART_STEP = re.compile(r"^part_(\d+)_of_(\d+)$")


def _known(pid):
    """None when the catalogue does not answer, False when the design is unknown, True otherwise."""
    if not studio.PID_RX.match(pid):
        return False
    a = studio._ask("catalogue.q.head", {"product_id": pid})
    if a is None:
        return None
    head = json.loads(a.text) if a.code == 200 else {}
    return bool(a.code == 200 and head.get("in_catalogue"))


def _made(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _open_job(cur, pid):
    """The design's open drawings job; the running one first (a queued follow-up must not hide the progress of the
    job that is drawing), else the oldest queued one."""
    return cur.execute("SELECT job_id, state, step, created_at FROM orch.jobs WHERE type = 'drawings.make' "
                       "AND payload->>'product_id' = %s AND state = ANY(%s) ORDER BY (state = 'queued'), created_at LIMIT 1",
                       (pid, list(OPEN))).fetchone()


@router.post("/api/drawings/{pid}/make")
def make(pid: str):
    ok = _known(pid)
    if ok is None:
        return studio._cat_down()
    if not ok:
        return JSONResponse({"error": "no such product"}, status_code=404)
    try:
        with job_client.conn() as con:
            cur = con.cursor(row_factory=dict_row)
            row = _open_job(cur, pid)
        if row is not None:
            return {"job_id": str(row["job_id"]), "state": "making"}
        with _RECENT_LOCK:                        # the row is made by the orchestrator a moment after the submit
            hit = _RECENT.get(pid)
            if hit and time.monotonic() - hit[1] < RECENT_S:
                return {"job_id": hit[0], "state": "making"}
            job_id = job_client.submit("drawings.make", {"product_id": pid, "reason": "button"})
            _RECENT[pid] = (job_id, time.monotonic())
        return {"job_id": job_id, "state": "making"}
    except (psycopg.Error, RuntimeError, job_client.JobUnavailable):
        return JSONResponse({"error": UNAVAILABLE}, status_code=503)


@router.post("/api/drawings/{pid}/stop")
def stop(pid: str):
    """Stop on the drawing bar. Every open `drawings.make` of this design is stopped (the running one and a
    queued follow-up), and only those (the job ids are read from the database, never taken from the request)."""
    ok = _known(pid)
    if ok is None:
        return studio._cat_down()
    if not ok:
        return JSONResponse({"error": "no such product"}, status_code=404)
    try:
        with job_client.conn() as con:
            rows = con.cursor(row_factory=dict_row).execute(
                "SELECT job_id FROM orch.jobs WHERE type = 'drawings.make' AND payload->>'product_id' = %s AND state = ANY(%s) "
                "ORDER BY created_at", (pid, list(OPEN))).fetchall()
            if not rows:
                return {"ok": False, "state": "none"}
            for r in rows:
                jobs.stop(con, r["job_id"], sender="gateway")
        return {"ok": True, "state": "stopping", "job_id": str(rows[0]["job_id"]), "stopped": len(rows)}
    except (psycopg.Error, RuntimeError, job_client.JobUnavailable):
        return JSONResponse({"error": UNAVAILABLE}, status_code=503)


def _progress(cur, parent_id):
    """{step, done, of} of an open drawings.make: its open child's step and how many children have finished."""
    kid = cur.execute("SELECT job_id, step FROM orch.jobs WHERE parent_job_id = %s AND state = ANY(%s) "
                      "ORDER BY created_at DESC LIMIT 1", (parent_id, list(OPEN))).fetchone()
    done = cur.execute("SELECT count(*) AS n FROM orch.jobs WHERE parent_job_id = %s AND state = 'succeeded'",
                       (parent_id,)).fetchone()["n"]
    step = None
    if kid is not None:
        ev = cur.execute("SELECT step FROM orch.job_events WHERE job_id = %s AND type = 'job.progressed' "
                         "ORDER BY seq DESC LIMIT 1", (kid["job_id"],)).fetchone()
        step = (ev or {}).get("step") or kid["step"]
        m = PART_STEP.match(str(step or ""))
        if m:                                     # "part 3 of 8" while the parts are drawn
            return {"step": "parts", "done": max(0, int(m.group(1)) - 1), "of": int(m.group(2)), "part": True}
    return {"step": step, "done": int(done), "of": STEPS_OF}


@router.get("/api/drawings/{pid}/status")
def status(pid: str):
    ok = _known(pid)
    if ok is None:
        return studio._cat_down()
    if not ok:
        return JSONResponse({"error": "no such product"}, status_code=404)
    a = studio._ask("catalogue.q.drawings_state", {"product_id": pid})
    if a is None or a.code != 200:
        return studio._cat_down()
    body = json.loads(a.text)
    cur_set = body.get("current")
    out = {"state": "none", "stopped": False, "stale": False, "stale_reason": None, "made_at": None, "revision": None, "progress": None,
           "job_id": None, "failed_parts": [], "sheets": 0, "sizes": None, "user_text": None, "set": None,
           "previous": body.get("previous")}
    if cur_set:
        stale = bool(cur_set.get("stale"))
        out.update(state="stale" if stale else "current", stale=stale, stale_reason=cur_set.get("stale_reason"),
                   made_at=cur_set["made_at"], revision=cur_set.get("revision"), set=cur_set.get("set"),
                   failed_parts=[{"ref": f.get("ref"), "why": f.get("why")} for f in cur_set.get("failed", [])
                                 if isinstance(f, dict)],
                   sheets=cur_set.get("sheets", 0), sizes=None if stale else (cur_set.get("sizes") or None))   # never the sizes of an older design
    try:
        with job_client.conn() as con:
            cur = con.cursor(row_factory=dict_row)
            row = _open_job(cur, pid)
            if row is not None:
                out.update(state="making", job_id=str(row["job_id"]), progress=_progress(cur, row["job_id"]))   # `stale` stays: the shown set is older
            else:
                last = cur.execute("SELECT job_id, state, finished_at FROM orch.jobs WHERE type = 'drawings.make' "
                                   "AND payload->>'product_id' = %s AND state = ANY(%s) ORDER BY finished_at DESC "
                                   "LIMIT 1", (pid, list(jobs.FINAL))).fetchone()
                if last is not None and last["state"] == "stopped" and (not cur_set or last["finished_at"] > _made(cur_set["made_at"])):
                    out["stopped"] = True                  # the page says "Stopped. The last drawings are kept."
                if last is not None and last["state"] in ("failed", "timed_out") and (
                        not cur_set or last["finished_at"] > _made(cur_set["made_at"])):
                    out.update(state="failed", user_text=FAILED_TEXT, job_id=str(last["job_id"]))
    except (psycopg.Error, RuntimeError):
        return JSONResponse({"error": UNAVAILABLE}, status_code=503)
    return out
