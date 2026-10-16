"""The catalogue service's command handlers for the CLI's catalogue commands.

    catalogue.find     -> framework/catalog/find.py
    catalogue.list     -> framework/catalog/product.py list
    catalogue.show     -> framework/catalog/product.py show <id>

Each handler runs the UNCHANGED command as a child of the catalogue service and returns what the child printed, as
text (never nested jsonb, so a number is never rewritten on the way). The asking client
(`framework/common/cliclient.py`) writes that text through its own stdout, so a run through the service prints exactly
what a direct run prints. The child gets an argument list and no shell. It runs with BC_CLI_ROLE=service and without
BC_SERVICES, so it can never become a client itself (recursion guard).
"""
import os
import re
import subprocess
import threading
import time

from framework.common import config, policy
from framework.common.consumer import ServiceError, Stopped

CATALOG = os.path.dirname(os.path.abspath(__file__))
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SCRIPTS = {"catalogue.find": "find.py",
           "catalogue.list": "product.py", "catalogue.show": "product.py"}
ID_RX = re.compile(r"^[A-Za-z0-9_.+-]{1,120}$")
ENV_PASS = ("BC_PG_OFF", "BC_PG_TRACE")        # the only environment a request may carry
ENV_DROP = ("BC_SERVICES", "BC_CORRELATION_ID", "BC_PARENT_JOB_ID", "BC_SENDER", "BC_PG_REEXEC", "BC_CLIENT_REEXEC")
MAX_ARGS, MAX_ARG_CHARS = 40, 4000


def _bad():
    return ServiceError("bad_request", "This catalogue request could not be read.")


def _argv(job_type, payload):
    argv = (payload or {}).get("argv")
    if (not isinstance(argv, list) or len(argv) > MAX_ARGS
            or not all(isinstance(x, str) and len(x) <= MAX_ARG_CHARS for x in argv)):
        raise _bad()
    if job_type == "catalogue.list" and argv != ["list"]:
        raise _bad()
    if job_type == "catalogue.show" and not (len(argv) == 2 and argv[0] == "show" and ID_RX.match(argv[1])):
        raise _bad()
    return argv


def _kill_tree(proc):
    """Kill the child and its children: only the PID this handler started (never by image name)."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=CREATE_NO_WINDOW, timeout=15)
        else:                                # macOS / Linux: the psutil tree of this pid
            from framework.common.proc import kill_tree
            kill_tree(proc.pid)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _pump(stream, sink):
    try:
        sink.append(stream.read())
    except Exception:
        sink.append("")


def _run(ctx, payload, job_type=None):
    job_type = job_type or ctx.envelope["type"]
    argv = _argv(job_type, payload)
    script = SCRIPTS[job_type]
    env = dict(os.environ)
    for k in ENV_DROP:
        env.pop(k, None)
    env["BC_CLI_ROLE"] = "service"
    env["PYTHONIOENCODING"] = "utf-8"
    sent = (payload or {}).get("env") or {}
    for k in ENV_PASS:
        if isinstance(sent.get(k), str):
            env[k] = sent[k]
    ctx.progress(job_type.split(".", 1)[1])
    limit_s = policy.policy(job_type).limit_s
    proc = subprocess.Popen([config.VENV_PY, os.path.join(CATALOG, script), *argv], cwd=config.ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", creationflags=CREATE_NO_WINDOW)
    ctx.on_stop(lambda: _kill_tree(proc))
    out, err = [], []
    threads = [threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True),
               threading.Thread(target=_pump, args=(proc.stderr, err), daemon=True)]
    for t in threads:
        t.start()
    started = time.monotonic()
    try:
        while proc.poll() is None:
            ctx.check()
            if time.monotonic() - started > limit_s - 5:
                _kill_tree(proc)
                raise ServiceError("catalogue_timeout", "The catalogue took too long to answer.", retryable=False)
            time.sleep(0.1)
    except Stopped:
        _kill_tree(proc)
        raise
    for t in threads:
        t.join(10)
    return {"stdout": out[0] if out else "", "stderr": err[0] if err else "", "code": int(proc.returncode)}


HANDLERS = {t: _run for t in SCRIPTS}
