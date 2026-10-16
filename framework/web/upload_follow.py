"""The studio follows an `upload.build` job and says what it is doing in plain words.

`follow(job_id, stems)` is a generator of ("status", label, state) lines and one last ("done", dict). It polls the job's rows
every 0.25 s (framework.common.jobs on the studio's pooled connection; nothing from the catalogue or the engines):

  the read step's `file_<i>` progress     -> "Reading <stem i> (i of n)"                      run
  the add step ends                       -> its messages, in order: "Read <stem>: n part(s)", "No solid geometry in <stem>, skipped",
                                             "Repaired the geometry of <stem>"
  the load step starts / ends             -> "Measuring parts and finding fits" run / "Measured N parts, M fits and contacts" done
  the 3D view step starts / ends          -> "Preparing the 3D view" run / "3D view ready" done (ends = succeeded or not, as before)
  last                                    -> ("done", the upload's answer {ok, product_id, name, parts, interfaces, added,
                                             not_catalogued}) or ("done", {"ok": False, "error": <the failure's sentence>})

The parent answers when the design is measured; the 3D view and the comparisons are trailing steps, so this follows the children's
rows, not the parent's events (a final parent writes no more step events). Only the first engine's 3D view is waited for ("mesh",
or "kmesh" in kernel mode); the second engine's view and the comparisons run on.

Order change against the in-process upload: the batch read is one step, so the "Reading" lines of a batch come together at its
start and the "Read" lines together after it (words and counts are the same).
"""
import psycopg

from framework.common import jobs, upload_follow as _shared
from framework.web import job_client

POLL_S = _shared.POLL_S
LIMIT_S = _shared.LIMIT_S
FILE_PREFIX = _shared.FILE_PREFIX


def follow(job_id, stems, limit_s=LIMIT_S):
    """The follower itself is framework/common/upload_follow.py (shared with other callers); the
    studio gives it its own pooled connection."""
    return _shared.follow(job_id, stems, limit_s, conn=job_client.conn)


def finished_all(job_id):
    """True when the upload's parent is final, its trailing steps are over and every child is final (what `BUSY` waits for)."""
    try:
        with job_client.conn() as con:
            row = con.execute("SELECT state, trailing_steps FROM orch.jobs WHERE job_id = %s", (job_id,)).fetchone()
            if row is None:
                return False
            kids = con.execute("SELECT state FROM orch.jobs WHERE parent_job_id = %s", (job_id,)).fetchall()
    except psycopg.Error:
        return False
    return row[0] in jobs.FINAL and not row[1] and all(k[0] in jobs.FINAL for k in kids)
