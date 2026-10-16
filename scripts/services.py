"""One command for every service: the orchestrator, which starts the other three (gateway, catalogue, geometry).

    python scripts/services.py check            say what start WOULD do and what is needed from you; change nothing
    python scripts/services.py start            start the orchestrator (hidden, no windows); it starts the gateway (the
                                                studio) and the catalogue and geometry services; waits until all four
                                                answer, then opens the Services page (--no-browser to skip)
    python scripts/services.py status           the four services: port, process, state, last heartbeat, restarts
    python scripts/services.py restart          restart all four, but only when no service has a running job
    python scripts/services.py restart <name>   restart one service once it has no running job (--force: do not wait)
    python scripts/services.py restart <name> [--env K=V]... [--env-clear]   check hook: restart with test settings
    python scripts/services.py stop             stop all four (refuses while a job runs; add --force); then checks that
                                                no process of this copy is left

Admin token: the orchestrator's control routes (restart, stop, scale, the check cap) answer only a caller that
sends the admin token. The host set keeps it in .containers/_host.admin (made by `start` when missing); this script and the
check scripts read it from there. A web page can never use these routes.

Ports: base port + 0, 1, 2, 4 = gateway, orchestrator, catalogue, geometry. The base port is the environment variable
SVC_BASE_PORT, else the line SVC_BASE_PORT=... in the gitignored file .env.services in the project root, else 8800.
Logs: work/logs/services/<name>.log. The services run with the product venv's python (override with MECH_PYTHON).
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))
from framework.common import admin, config, hostos  # noqa: E402

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SETUP = "pip install -r requirements.txt (in the product venv)" if os.name == "nt" else "bash scripts/setup_mac.sh install"
GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
LOG_DIR = os.path.join(ROOT, "work", "logs", "services")
OK, WARN, FAIL = "OK  ", "WARN", "FAIL"
START_LIMIT_S = 60


def _reexec_in_venv():
    """The commands need psutil and psycopg, which live in the product venv: re-run there when started from another
    Python (the venv's own check still runs when the venv is missing)."""
    try:
        import psutil  # noqa: F401
        import psycopg  # noqa: F401
        return None
    except ImportError:
        pass
    if os.environ.get("BC_SERVICES_REEXEC") or not os.path.exists(config.VENV_PY):
        return None
    env = dict(os.environ, BC_SERVICES_REEXEC="1")
    return subprocess.run([config.VENV_PY, os.path.abspath(__file__)] + sys.argv[1:], env=env).returncode


def say(tag, msg, fix=None):
    print("  [%s] %s" % (tag, msg))
    if fix:
        print("         -> %s" % fix)


def base_and_source():
    if os.environ.get("SVC_BASE_PORT"):
        return config.base_port(), "SVC_BASE_PORT"
    if config._env_file().get("SVC_BASE_PORT"):
        return config.base_port(), ".env.services"
    return config.base_port(), "default"


BASE, BASE_SRC = base_and_source()
PORTS = config.ports(BASE)
ORCH = "http://127.0.0.1:%d" % PORTS["orchestrator"]
GATEWAY = "http://127.0.0.1:%d" % PORTS["gateway"]


def get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def orch_post(path, body, timeout=10):
    """A POST to one of the orchestrator's control routes carries the admin token. On a host set the token file is made when
    missing (the orchestrator reads it at every request, so one made after it started counts at once)."""
    if admin.mode(ORCH) == "host":
        admin.make(admin.host_path(ROOT))
    return post(ORCH + path, body, timeout, admin.headers(ORCH, ROOT))


def post(url, body, timeout=10, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"error": str(e)}


def listening():
    """{port: pid} of every 127.0.0.1 port of this set that something listens on."""
    if os.name != "nt":                      # macOS / Linux (lsof / psutil); Windows below
        return hostos.listening(PORTS.values())
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, creationflags=NOWIN).stdout
    held = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING":
            addr = parts[1]
            for p in PORTS.values():
                if addr.endswith(":%d" % p) and (addr.startswith("127.0.0.1") or addr.startswith("0.0.0.0")):
                    held[p] = parts[4]
    return held


def orchestrator():
    """The orchestrator's /health when it is THIS copy's, else None."""
    h = get(ORCH + "/health")
    if h and h.get("service") == "orchestrator" and os.path.normcase(h.get("root", "")) == os.path.normcase(ROOT):
        return h
    return None


def services():
    body = get(ORCH + "/api/services")
    return body["services"] if body else None


def copy_processes():
    """Processes of THIS copy and base port: they carry BC_SERVICES_ROOT / BC_SERVICES_BASE in their environment
    (set by start, inherited by everything the services start). Never this command or its parents."""
    import psutil
    me = {os.getpid()}
    try:
        p = psutil.Process()
        me.update(x.pid for x in p.parents())
    except Exception:
        pass
    root = os.path.normcase(ROOT)
    found = []
    for p in psutil.process_iter(["pid", "name"]):
        if p.pid in me or p.pid == 0:
            continue
        try:
            env = p.environ()
        except Exception:
            continue
        if os.path.normcase(env.get("BC_SERVICES_ROOT", "")) == root and env.get("BC_SERVICES_BASE") == str(BASE):
            found.append((p.pid, p.info.get("name")))
    return found


def print_table(rows):
    print("  %-13s %-6s %-8s %-9s %-14s %-9s %-20s %s" % ("service", "port", "process", "state", "last heartbeat",
                                                        "restarts", "runs on", "running jobs / note"))
    for r in rows:
        hb = r.get("heartbeat_age_s")
        note = (str(r.get("running_jobs") or 0) + ("  " + r["detail"] if r.get("detail") else ""))
        sc = r.get("scale") or {}
        if sc.get("scalable"):
            note += "  copies %s (up to %s)" % (sc.get("replicas", 1), sc.get("max", "?"))
        where = r.get("where") or "this PC"
        if r.get("image"):
            where += " (%s)" % r["image"]
        print("  %-13s %-6s %-8s %-9s %-14s %-9s %-20s %s" % (
            r["name"], r["port"], r.get("pid") or "-", r["state"], "-" if hb is None else "%.1f s ago" % hb,
            r.get("restarts", 0), where, note))


# ---- check ---------------------------------------------------------------------------------------------------------
def checks():
    """Returns the list of blocking problems. Prints one line per check. Writes nothing."""
    bad = []
    print("\nWhat the services need:")
    print("  ports: base %d (%s): %s" % (BASE, BASE_SRC, ", ".join("%s %d" % (n, PORTS[n]) for n in config.ORDER)))
    forbidden = config.bad_ports(BASE)
    if forbidden:
        say(FAIL, "ports %s are listed in BC_RESERVED_PORTS" % forbidden,
            "pick another base port in .env.services (SVC_BASE_PORT=...)")
        bad.append("ports")
    if os.path.exists(config.VENV_PY):
        say(OK, "product environment found (%s)" % config.VENV_PY)
        r = subprocess.run([config.VENV_PY, "-c", "import fastapi, uvicorn, psycopg, psutil, build123d"],
                           capture_output=True, text=True, timeout=120, creationflags=NOWIN)
        if r.returncode == 0:
            say(OK, "web server, Postgres driver, process tools and the in-process kernel installed")
        else:
            say(FAIL, "a module is missing in the product environment: %s" % (r.stderr.strip().splitlines() or ["?"])[-1],
                SETUP)
            bad.append("modules")
    else:
        say(FAIL, "product environment not found at %s" % config.VENV_PY,
            SETUP + "   (or set MECH_PYTHON)")
        bad.append("venv")
    try:
        from framework.common import pg as cpg
        name = cpg.dbname()
        if name in config.PROTECTED_DBS:
            say(FAIL, "this copy points at a protected database %r (BC_PROTECTED_DBS)" % name,
                "name this copy's own database in .env.buildcore (BC_PG_DSN=...)")
            bad.append("db")
        else:
            con = cpg.connect()
            try:
                n = con.execute("SELECT count(*) FROM product").fetchone()[0]
                reg = con.execute("SELECT to_regclass('orch.services')").fetchone()[0]
            finally:
                con.close()
            say(OK, "Postgres database %s: %d products; service registry %s" % (
                name, n, "present" if reg else "not created yet (start creates orch.services)"))
    except Exception as e:
        say(FAIL, "Postgres not reachable (%s)" % type(e).__name__,
            "start Docker Desktop and the Postgres container (scripts/setup_postgres.md)")
        bad.append("postgres")
    return bad


def cmd_check():
    print("SERVICES - dry run (nothing is changed)")
    bad = checks()
    held = listening()
    orch = orchestrator()
    print("\nWhat 'start' would do:")
    if orch:
        print("  - The services are ALREADY running (orchestrator pid %s). It would leave them alone:" % orch["pid"])
        rows = services()
        if rows:
            print_table(rows)
    elif held:
        print("  - Ports of this set are held by other processes: %s." % ", ".join(
            "%d (pid %s)" % (p, pid) for p, pid in sorted(held.items())))
        print("  - It would stop and tell you; free the ports or use another base port (.env.services).")
    else:
        print("  - Start the orchestrator hidden (no windows) on %s; log work\\logs\\services\\orchestrator.log." % ORCH)
        print("  - The orchestrator ties every service to its own life (one kill-on-close job object) and starts:")
        for n in config.ORDER:
            if n != "orchestrator":
                print("      %-10s on 127.0.0.1:%d   log work\\logs\\services\\%s.log" % (n, PORTS[n], n))
        print("  - It creates the registry orch.services in the database if it is missing, then waits until all %d"
              % len(config.ORDER))
        print("    answer (limit %d s) and opens %s/services." % (START_LIMIT_S, GATEWAY))
    print("\nWhat is needed from you:")
    if bad:
        print("  - Fix the FAIL lines above first (each has its fix underneath).")
    else:
        print("  - Nothing. Run:  python scripts/services.py start")
    return 1 if bad else 0


# ---- start ---------------------------------------------------------------------------------------------------------
def launch():
    os.makedirs(LOG_DIR, exist_ok=True)
    log = open(os.path.join(LOG_DIR, "orchestrator.log"), "a", encoding="utf-8")
    log.write("\n===== %s services.py start (base port %d) =====\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), BASE))
    log.flush()
    env = {k: v for k, v in os.environ.items()
           if k not in ("BC_SERVICES_REEXEC",)}
    env.update(SVC_BASE_PORT=str(BASE), BC_SERVICES_ROOT=ROOT, BC_SERVICES_BASE=str(BASE), BC_SERVICE="orchestrator",
               PYTHONUNBUFFERED="1")
    # The orchestrator's control routes need the admin token on the host set too. The file is made once
    # (when missing) and kept: a restart keeps the token, so a script that read it before still holds the right one.
    tok = admin.host_path(ROOT)
    if admin.make(tok):
        print("  Made the admin token for the orchestrator's control routes (%s; the scripts read it)." % os.path.relpath(tok, ROOT))
    env["BC_ADMIN_TOKEN_FILE"] = tok
    # Hidden console, NOT detached: a detached process makes every console child open a window.
    # macOS / Linux: its own session (the analogue of the new process group), so closing the terminal that ran
    # `start` neither hangs it up nor reaches the services it starts.
    subprocess.Popen([config.VENV_PY, "-m", "framework.orchestrator"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                     stdout=log, stderr=subprocess.STDOUT, creationflags=NOWIN | GROUP,
                     start_new_session=os.name != "nt")


# macOS: an idle-sleeping Mac freezes a long-running job, and on wake its queue message is redelivered and the job fails
# as "interrupted". While the services run, `caffeinate` holds the Mac awake: -i idle sleep,
# -s system sleep on AC power (lid closed included); -w ties it to the orchestrator, so it ends when the services stop.
def _awake_pid(orch_pid):
    if sys.platform != "darwin" or not orch_pid:
        return None
    out = subprocess.run(["pgrep", "-f", "caffeinate -i -s -w %s$" % orch_pid], capture_output=True, text=True).stdout
    return out.split()[0] if out.split() else None


def keep_awake(orch_pid):
    """Start (once) the caffeinate that keeps this Mac awake for the orchestrator's life. Returns its pid or None."""
    if sys.platform != "darwin" or not orch_pid:
        return None
    pid = _awake_pid(orch_pid)
    if pid:
        return pid
    p = subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(orch_pid)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return str(p.pid)


def wait_up(limit=START_LIMIT_S, quiet=False):
    """Wait until every service (config.ORDER) reports running. Returns (seconds, rows) or (None, rows)."""
    t0 = time.monotonic()
    last = None
    rows = None
    while time.monotonic() - t0 < limit:
        rows = services()
        if rows:
            up = [r["name"] for r in rows if r["state"] == "running"]
            if not quiet and len(up) != last:
                print("  %5.1f s  %d of %d running" % (time.monotonic() - t0, len(up), len(config.ORDER)), flush=True)
                last = len(up)
            if len(up) == len(config.ORDER):
                return round(time.monotonic() - t0, 1), rows
        time.sleep(0.5)
    return None, rows


def cmd_start(browser=True):
    print("SERVICES - start (base port %d, %s)" % (BASE, BASE_SRC))
    bad = checks()
    if bad:
        print("\nNot started: fix the FAIL lines above, then run this again.")
        return 1
    orch = orchestrator()
    if orch:
        print("\nAlready running:")
        print_table(services() or [])
        if keep_awake(orch.get("pid")):
            print("Mac kept awake while the services run (caffeinate).")
        if browser:
            webbrowser.open(GATEWAY + "/services")
        return 0
    held = listening()
    if held:
        print("\nPorts of this set are held by other processes: %s. Not started." % ", ".join(
            "%d (pid %s)" % (p, pid) for p, pid in sorted(held.items())))
        return 1
    print("\nStarting the orchestrator ...")
    launch()
    secs, rows = wait_up()
    if secs is None:
        print("  Not all %d answered within %d s:" % (len(config.ORDER), START_LIMIT_S))
        print_table(rows or [])
        print("  Logs: work\\logs\\services\\<name>.log")
        return 1
    print("  All %d running after %.1f s:" % (len(config.ORDER), secs))
    print_table(rows)
    orch = orchestrator()
    if orch and keep_awake(orch.get("pid")):
        print("  Mac kept awake while the services run (caffeinate; ends when they stop).")
    print("\nServices page: %s/services    Studio: %s" % (GATEWAY, GATEWAY))
    if browser:
        webbrowser.open(GATEWAY + "/services")
    return 0


# ---- status --------------------------------------------------------------------------------------------------------
def cmd_status():
    orch = orchestrator()
    if not orch:
        held = listening()
        if held:
            print("The orchestrator does not answer, but ports are held: %s." % ", ".join(
                "%d (pid %s)" % (p, pid) for p, pid in sorted(held.items())))
            print("Run: python scripts/services.py stop   (it ends what is left of this copy)")
        else:
            print("Not running. Start them with: python scripts/services.py start")
        return 0
    rows = services() or []
    up = sum(1 for r in rows if r["state"] == "running")
    print("Services (base port %d): %d of %d running; orchestrator pid %s, up %.0f s; Services page %s/services"
          % (BASE, up, len(config.ORDER), orch["pid"], orch["uptime_s"], GATEWAY))
    print_table(rows)
    if sys.platform == "darwin":
        print("Mac kept awake: %s" % ("yes (caffeinate pid %s)" % _awake_pid(orch["pid"]) if _awake_pid(orch["pid"])
                                       else "NO -- run: python3 scripts/services.py start"))
    return 0


# ---- stop ----------------------------------------------------------------------------------------------------------
def kill_left():
    left = copy_processes()
    for pid, _ in left:
        if os.name != "nt":                  # macOS / Linux, the same pid tree by psutil
            hostos.kill_tree(pid)
            continue
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, creationflags=NOWIN)
    return left


def cmd_stop(force=False):
    orch = orchestrator()
    if orch:
        code, body = orch_post("/api/stop", {"force": force})
        if code in (401, 403):
            print("The orchestrator refused the stop (%s: %s). Nothing was stopped." % (code, body.get("error")))
            return 1
        if code == 409:
            print("Not stopping: running jobs %s. Wait for them, or use: python scripts/services.py stop --force"
                  % body.get("running"))
            return 1
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20 and orchestrator():
            time.sleep(0.3)
        print("Orchestrator stopped (pid %s) after %.1f s." % (orch["pid"], time.monotonic() - t0))
    else:
        print("The orchestrator is not running.")
    time.sleep(1.0)
    left = copy_processes()
    if left:
        print("Ending %d process(es) of this copy that were still there: %s" % (len(left), left))
        kill_left()
        time.sleep(1.0)
        left = copy_processes()
    held = listening()
    print("Processes of this copy left: %d; ports %d-%d held: %d" % (len(left), min(PORTS.values()), max(PORTS.values()), len(held)))
    return 0 if not left and not held else 1


# ---- restart -------------------------------------------------------------------------------------------------------
def total_running(rows):
    return {r["name"]: r["running_jobs"] for r in rows or [] if r.get("running_jobs")}


def cmd_restart(name=None, force=False, env=None, env_clear=False):
    if not orchestrator():
        print("Not running; starting.")
        return cmd_start(browser=False)
    if name:
        if name not in config.ORDER or name == "orchestrator":
            print("No service %r. Names: %s (the orchestrator restarts with: services.py restart)"
                  % (name, ", ".join(n for n in config.ORDER if n != "orchestrator")))
            return 2
        before = {r["name"]: r for r in services() or []}.get(name, {})
        req = {"service": name, "force": force}
        if env:                               # check hook: test settings for this service
            req["env"] = env
        if env_clear:
            req["env_clear"] = True
        code, body = orch_post("/api/restart", req)
        if code != 200:
            print("Restart refused: %s" % body)
            return 1
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3600:
            rows = {r["name"]: r for r in services() or []}
            r = rows.get(name, {})
            if r.get("state") == "draining":
                print("waiting: %s has %d running job(s)" % (name, r.get("running_jobs") or 0), flush=True)
                time.sleep(5)
                continue
            if r.get("state") == "running" and r.get("pid") and (r.get("pid") != before.get("pid") or r.get("restarts", 0) > before.get("restarts", 0)):
                print("%s restarted: process %s -> %s after %.1f s; restarts %d"
                      % (name, before.get("pid"), r["pid"], time.monotonic() - t0, r.get("restarts", 0)))
                return 0
            time.sleep(0.5)
        print("No answer from %s within the limit; see work\\logs\\services\\%s.log" % (name, name))
        return 1
    # the whole set: wait for every running job (every service's), then stop and start
    while True:
        busy = total_running(services())
        if not busy or force:
            break
        print("waiting: running jobs %s" % busy, flush=True)
        time.sleep(5)
    rc = cmd_stop(force=True)
    if rc:
        return rc
    return cmd_start(browser=False)


def main(argv):
    rc = _reexec_in_venv()
    if rc is not None:
        return rc
    cmd = argv[0] if argv else "status"
    env, rest, i = {}, [], 1
    while i < len(argv):                     # --env K=V (repeatable) belongs to restart; K=V is not a service name
        if argv[i] == "--env" and i + 1 < len(argv):
            k, _, v = argv[i + 1].partition("=")
            env[k] = v
            i += 2
            continue
        if not argv[i].startswith("--"):
            rest.append(argv[i])
        i += 1
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if cmd == "check":
        return cmd_check()
    if cmd == "start":
        return cmd_start("--no-browser" not in argv)
    if cmd == "status":
        return cmd_status()
    if cmd == "stop":
        return cmd_stop("--force" in argv)
    if cmd == "restart":
        if env or "--env-clear" in argv:
            keys = sorted(env)
            print("settings for %s: %s%s" % (rest[0] if rest else "?", ", ".join(keys) or "none",
                                            " (earlier ones cleared)" if "--env-clear" in argv else ""))
        return cmd_restart(rest[0] if rest else None, "--force" in argv, env or None, "--env-clear" in argv)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
