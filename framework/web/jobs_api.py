"""The job API of the gateway: the test job through the whole path.

    POST /api/jobs/echo      {"text": str <= 200, "sleep_s": 0..120}  ->  {"job_id", "correlation_id"}
    GET  /api/jobs/{job_id}                                           ->  {"job": {...with result}, "events": [...]}
    POST /api/jobs/{job_id}/stop                                      ->  {"ok": true, "state": str}   (idempotent)

The gateway only sends a message to the orchestrator's queue and reads the job tables; it never writes them. Answers are
in product words: no queue names, tool names or developer text.
"""
import uuid
from datetime import datetime

import psycopg
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from framework.common import jobs, pg
from framework.common.pgpool import Pool

router = APIRouter()
POOL = Pool(2, connect=lambda: pg.connect(write=True))

BAD_INPUT = "The test job takes a text of at most 200 characters and a pause of 0 to 120 seconds."
UNAVAILABLE = "The job service is not available right now."


def _json(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return value


@router.post("/api/jobs/echo")
def post_echo(body: dict = Body(default=None)):
    body = body if isinstance(body, dict) else {}
    text = body.get("text", "hello")
    sleep_s = body.get("sleep_s", 0)
    ok = (isinstance(text, str) and len(text) <= 200 and isinstance(sleep_s, (int, float))
          and not isinstance(sleep_s, bool) and 0 <= sleep_s <= 120)
    if not ok:
        return JSONResponse({"error": BAD_INPUT}, status_code=400)
    job_id, corr = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        with POOL.conn() as con:
            jobs.submit(con, "catalogue.echo", {"text": text, "sleep_s": sleep_s}, sender="gateway", job_id=job_id,
                        correlation_id=corr)
    except psycopg.Error:
        return JSONResponse({"error": UNAVAILABLE}, status_code=503)
    return {"job_id": job_id, "correlation_id": corr}


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    try:
        uuid.UUID(job_id)
    except ValueError:
        return JSONResponse({"error": "This job is not known."}, status_code=404)
    try:
        with POOL.conn() as con:
            job = jobs.get(con, job_id, with_result=True)
            evs = jobs.events(con, job_id) if job else []
    except psycopg.Error:
        return JSONResponse({"error": UNAVAILABLE}, status_code=503)
    if job is None:
        return JSONResponse({"error": "This job is not known."}, status_code=404)
    job.pop("result_ref", None)           # a file name is not shown; the result itself is in "result"
    return {"job": _json(job), "events": _json(evs)}


@router.post("/api/jobs/{job_id}/stop")
def post_stop(job_id: str):
    """Ask the orchestrator to stop a job. A finished job is left alone: the answer is its final state."""
    try:
        uuid.UUID(job_id)
    except ValueError:
        return JSONResponse({"error": "This job is not known."}, status_code=404)
    try:
        with POOL.conn() as con:
            job = jobs.get(con, job_id)
            if job is None:
                return JSONResponse({"error": "This job is not known."}, status_code=404)
            if job["state"] not in jobs.FINAL:
                jobs.stop(con, job_id, sender="gateway")
                job = jobs.get(con, job_id) or job
    except psycopg.Error:
        return JSONResponse({"error": UNAVAILABLE}, status_code=503)
    return {"ok": True, "state": job["state"]}
