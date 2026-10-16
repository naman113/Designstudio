"""The client side of the catalogue commands.

With BC_SERVICES=1 in the environment, find.py and product.py list|show do not read the catalogue
themselves: they submit a job (catalogue.find / .propose / .list / .show) through the orchestrator, wait for it and
print what the catalogue service's run of the same command printed. Without the variable nothing changes.

Only the standard library is imported at module level, so the system Python (no psycopg) can import this module;
psycopg, framework.common.pg and framework.common.jobs are imported inside the functions that need them.

There is deliberately NO direct fallback: when the service cannot answer, the command prints one plain sentence and
exits 3 (a command must never silently open the catalogue itself). `watch_direct` logs any direct catalogue open that a
command makes while BC_SERVICES=1 (product.py load stays direct, and shows up as such).

Name confusion guard: BC_SERVICE (singular, set by the supervisor for each service) is never read here.
"""
import json
import os
import subprocess
import sys
import time

from framework.common import config

SWITCH = "BC_SERVICES"
ROLE = "BC_CLI_ROLE"                  # "service" inside the catalogue service's child: never a client (recursion guard)
REEXEC = "BC_CLIENT_REEXEC"           # the venv hand-over happens once
LOG = os.path.join(config.ROOT, "work", "logs", "catalogue_cli.jsonl")
ENV_KEYS = ("BC_PG_OFF", "BC_PG_TRACE")
LAST = {}                              # the record of the latest run() in this process (the checks read job_id from it)
UNAVAILABLE = "The catalogue is not available right now."
NOT_FROM_HERE = "The catalogue is not available from here."


class CatalogueUnavailable(Exception):
    def __init__(self, user_text):
        super().__init__(user_text)
        self.user_text = user_text


def wanted():
    """True when this command should ask the catalogue service instead of reading the catalogue."""
    return os.environ.get(SWITCH) == "1" and os.environ.get(ROLE) != "service"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _log(record):
    """One line per call, appended; never raises."""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def run(job_type, argv):
    """Submit `job_type` with `argv`, wait, return (stdout, stderr, exit code) exactly as the catalogue service's run of
    the command produced them. Needs psycopg. Raises CatalogueUnavailable."""
    import psycopg
    from framework.common import jobs, pg, policy
    started = time.monotonic()
    correlation_id = os.environ.get("BC_CORRELATION_ID") or None
    rec = {"at": _now(), "kind": "relay", "type": job_type, "job_id": None, "correlation_id": correlation_id,
           "pid": os.getpid(), "ppid": os.getppid(), "script": os.path.basename(sys.argv[0] or "") or None}
    con = None
    try:
        con = pg.connect(write=True)
        sender = os.environ.get("BC_SENDER") or ("designer" if correlation_id else "cli")
        env = {k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)}
        job_id = jobs.submit(con, job_type, {"argv": list(argv), "env": env}, sender=sender,
                             correlation_id=correlation_id, parent_job_id=os.environ.get("BC_PARENT_JOB_ID") or None)
        rec["job_id"] = job_id
        LAST.clear()
        LAST.update(rec)
        row = jobs.wait(con, job_id, timeout_s=policy.policy(job_type).limit_s + 30)
        if row["state"] != "succeeded":
            raise CatalogueUnavailable(row.get("user_text") or "The catalogue could not answer right now.")
        res = jobs.get(con, job_id, with_result=True)["result"]
        out, err, code = res["stdout"], res["stderr"], int(res["code"])
        rec["code"] = code
        return out, err, code
    except CatalogueUnavailable as exc:
        rec["error"] = exc.user_text
        raise
    except (TimeoutError, psycopg.Error, OSError, RuntimeError, KeyError, TypeError) as exc:
        rec["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        raise CatalogueUnavailable(UNAVAILABLE) from exc
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        rec["ms"] = round((time.monotonic() - started) * 1000)
        LAST.clear()
        LAST.update(rec)
        _log(rec)


def _raw(text, stream):
    stream.flush()
    try:
        stream.write(text)
    except UnicodeEncodeError:
        buf = getattr(stream, "buffer", None)
        if buf is None:
            raise
        buf.write(text.encode(getattr(stream, "encoding", None) or "utf-8", "replace"))
    stream.flush()


def _handover(script, argv):
    """No psycopg here (a system Python): run the same command under the venv interpreter once, its
    stdout and stderr bytes passed through unchanged (the same pass-through as query.handover)."""
    py = os.environ.get("MECH_PYTHON") or config.VENV_PY
    if os.environ.get(REEXEC) or not py or not os.path.isfile(py):
        sys.stderr.write(NOT_FROM_HERE + "\n")
        return 3
    done = subprocess.run([py, script, *argv], env=dict(os.environ, **{REEXEC: "1"}), capture_output=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for data, stream in ((done.stdout, sys.stdout), (done.stderr, sys.stderr)):
        if not data:
            continue
        raw = getattr(stream, "buffer", None)
        stream.flush()
        if raw is not None:
            raw.write(data)
            raw.flush()
        else:
            stream.write(data.decode("utf-8", "replace"))
    return done.returncode


def relay(job_type, argv, *, script):
    """The command-line entry point's whole run: ask the service and print its answer. -> the exit code."""
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return _handover(script, argv)
    try:
        out, err, code = run(job_type, argv)
    except CatalogueUnavailable as exc:
        sys.stderr.write(exc.user_text + "\n")
        return 3
    if out:
        _raw(out, sys.stdout)
    if err:
        _raw(err, sys.stderr)
    return code


_WATCHING = []


def watch_direct(script):
    """While BC_SERVICES=1, log every direct open of the catalogue database (catalog.db*) this process makes.
    Every direct catalogue path, the Postgres one included, first compares SQLite's fingerprint, so this sees them all."""
    if not wanted() or _WATCHING:
        return
    _WATCHING.append(script)

    def hook(event, args):
        try:
            if event != "sqlite3.connect" or not args:
                return
            name = os.path.basename(os.fsdecode(args[0]))
            if not (name.startswith("catalog.db") or name == os.path.basename(config.catalog_db())):
                return
            _log({"at": _now(), "kind": "direct_open", "script": script, "argv": sys.argv[1:4],
                  "correlation_id": os.environ.get("BC_CORRELATION_ID") or None, "pid": os.getpid(),
                  "ppid": os.getppid()})
        except Exception:
            pass

    sys.addaudithook(hook)
