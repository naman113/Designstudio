"""Follow an `upload.build` / `designer.register` job and say what it is doing in plain words (one follower for every
caller).

`follow(job_id, stems)` is a generator of ("status", label, state) lines and one last ("done", dict). It polls the job's rows
every 0.25 s (framework.common.jobs on a connection from `conn`: the studio passes its pool, a command the
default, one connection it holds for the whole follow; nothing from the catalogue or the engines):

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
import time

import psycopg

from framework.common import jobs, pg, policy, results

POLL_S = 0.25
LIMIT_S = policy.wait_s("upload.build")      # the upload.build row + 30 s
FILE_PREFIX = "file_"


class _Held:
    """The default connection of `follow`: one connection opened on first use and kept for the whole follow (a fresh one
    per poll would cost a connect every 0.25 s); dropped after a database error so the next poll opens a new one. Use as
    `with held() as con`."""

    def __init__(self):
        self.con = None

    def __call__(self):
        return self

    def __enter__(self):
        if self.con is None or self.con.closed:
            self.con = pg.connect()
        return self.con

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None and issubclass(exc_type, psycopg.Error):
            try:
                self.con.close()
            except Exception:
                pass
            self.con = None
        return False

    def close(self):
        try:
            if self.con is not None:
                self.con.close()
        except Exception:
            pass
        self.con = None


def _unpack(row):
    try:
        return results.unpack(row.get("result")) if row else None
    except Exception:
        return None


def _children(con, parent):
    return con.execute("SELECT job_id, step, state, error_code, user_text FROM orch.jobs WHERE parent_job_id = %s "
                       "ORDER BY created_at", (parent,)).fetchall()


def _result(con, job_id):
    row = jobs.get(con, str(job_id), with_result=True)
    return _unpack(row)


def follow(job_id, stems, limit_s=LIMIT_S, conn=None):
    conn = conn or _Held()
    n = len(stems)
    seen, finished = set(), set()        # child step names dispatched / finished (each once)
    read_seq = {}                        # read child's last event seq
    reading = set()                      # file numbers already announced
    mesh_step = None                     # "mesh" (both, freecad) or "kmesh" (kernel mode): the 3D view that is waited for
    end = time.monotonic() + limit_s
    lines = []

    def say(label, state):
        lines.append(("status", label, state))

    def announce_reading(i):
        if 1 <= i <= n and i not in reading:
            reading.add(i)
            say("Reading %s (%d of %d)" % (stems[i - 1], i, n), "run")

    errors = 0
    while True:
        try:
            with conn() as con:
                for jid, step, state, _code, _text in _children(con, job_id):
                    if step not in seen:
                        seen.add(step)
                        if step == "kread" and "read" not in seen:          # kernel mode: no per-file progress from the kernel
                            for i in range(1, n + 1):
                                announce_reading(i)
                        elif step == "load":
                            say("Measuring parts and finding fits", "run")
                        elif step in ("mesh", "kmesh") and mesh_step is None:
                            mesh_step = step
                            say("Preparing the 3D view", "run")
                    if step == "read" and step not in finished:               # per-file progress of the first engine's read
                        for ev in jobs.events(con, str(jid), read_seq.get(step, 0)):
                            read_seq[step] = ev["seq"]
                            if ev["type"] == "job.progressed" and str(ev["step"] or "").startswith(FILE_PREFIX):
                                try:
                                    announce_reading(int(ev["step"][len(FILE_PREFIX):]))
                                except ValueError:
                                    pass
                    if state in jobs.FINAL and step not in finished:
                        finished.add(step)
                        if step == "add" and state == "succeeded":
                            for label, st in (_result(con, jid) or {}).get("messages") or []:
                                say(label, st)
                        elif step == "load" and state == "succeeded":
                            r = _result(con, jid) or {}
                            say("Measured %d parts, %d fits and contacts" % (r.get("parts", 0), r.get("interfaces", 0)), "done")
                        elif step == mesh_step:
                            say("3D view ready", "done")
                parent = jobs.get(con, str(job_id), with_result=True)
                trailing = con.execute("SELECT trailing_steps FROM orch.jobs WHERE job_id = %s", (job_id,)).fetchone()
            errors = 0
        except psycopg.Error:
            errors += 1
            if errors > 40:
                yield ("done", {"ok": False, "error": "The design could not be added right now."})
                return
            time.sleep(1.0)
            continue
        for ln in lines:
            yield ln
        lines.clear()
        if parent is not None and parent["state"] in jobs.FINAL:
            if parent["state"] != "succeeded":
                yield ("done", {"ok": False, "error": parent.get("user_text") or "The design could not be added right now."})
                return
            if mesh_step in finished or (mesh_step is None and trailing is not None and not trailing[0]):
                if mesh_step is None:
                    yield ("status", "3D view ready", "done")
                r = _unpack(parent) or {}
                yield ("done", {"ok": True, "product_id": r.get("product_id"), "name": r.get("name"), "parts": r.get("parts"),
                                "interfaces": r.get("interfaces"), "added": r.get("added"),
                                "not_catalogued": len(r.get("missing") or [])})
                return
        if time.monotonic() > end:
            yield ("done", {"ok": False, "error": "The design could not be added right now."})
            return
        time.sleep(POLL_S)
