"""The orchestrator as supervisor: starts, watches and restarts the three other services.

    python -m framework.orchestrator          (started by python scripts/services.py start; never by hand)

How it runs them:
- Each service is `C:\\venvs\\buildcore\\Scripts\\python.exe -m <module>` with the project root as working folder, a
  hidden console (CREATE_NO_WINDOW; never detached, or every program a service
  starts would open a visible window) and its own log in work/logs/services/<name>.log.
- The orchestrator first puts itself in a kill-on-close Windows job object (framework/common/winjob.py).
  Every process it starts, and everything those start, joins that job, so when the orchestrator ends by any means
  (stop, taskkill /F, a crash) Windows ends all of them: no orphans.
- Each service answers GET /health on its port and POSTs a heartbeat here every 5 s (running jobs included).
  The orchestrator writes the registry to `orch.services` (registry.py); the gateway's Services page reads it.

When it restarts a service:
- The process exited (crash, taskkill /F): it is started again after a backoff of 3, 5, 10, 20, 30 s for the 1st..5th
  exit within 60 s; the 5th exit within 60 s ends the retries (state "failed", see its log).
- No heartbeat for 30 s and its last report had no running job: it is ended and started again.
- No heartbeat for 30 s but its last report had a running job: it is NOT restarted (state "busy"); a long job may hold
  the process (a kernel call can hold Python's lock for minutes). It returns to "running" with its next heartbeat.
- A requested restart (`services.py restart <name>`) waits until the service reports no running job (the service
  is told to drain first); `--force` skips the wait.

Containers (`BC_SUPERVISE=containers`; off unless chosen): the services run in Docker (compose, restart policy
`unless-stopped`). The supervisor then
starts nothing. Every service is "external": its state comes from its heartbeats, which count only when they carry this set's id
(`BC_SET_ID`). A heartbeat from a new process (pid or start time changed) means the service restarted: its commands are released at
once (`service_died`, last=True). No heartbeat for 30 s and no running job = "down", and the host agent is asked to restart that
container (never when its last report said `stopping`: a deliberate `docker compose stop` stays stopped); with a running job =
"busy", left alone. A requested restart drains, then `POST /control/exit` (Docker or the agent starts it again). The scaler is off:
each service runs as one copy.

Replicas: the catalogue and geometry services may run as several processes. Replica
1 is the one the supervisor starts itself (fixed port, restarted after a crash). Replicas 2..n are started and retired by the scaler
(framework/orchestrator/scaler.py) through `add_replica` / `retire_replica`: they get `BC_REPLICA=<n>` and
`SVC_PORT=0` (a port the system chooses, learned from their first heartbeat), are never restarted when they stop
(a replica that exits without being retired is "lost": the scaler adds a new one if work still waits), and a death
releases only that process's commands (`DISPATCHER.service_died(name, pid, last)`).
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from framework.common import admin, config, control, policy, slog
from framework.common.service import version
from framework.common.winjob import tie_children_to_me
from framework.orchestrator.dispatcher import Dispatcher
from framework.orchestrator.registry import Registry

MODULES = {
    "gateway": "framework.web.gateway",
    "catalogue": "framework.catalog.service",
    "geometry": "framework.kernel.service",
}
RETENTION = None           # the daily archive/log pruning thread (framework/orchestrator/retention.py)
DISPATCHER = None          # the job loop (framework/orchestrator/dispatcher.py), started by main() before the services
SCALER = None              # the scaler (framework/orchestrator/scaler.py), started by main() once the services start
LIVE_STATES = ("starting", "running", "busy", "draining", "retiring")
BACKOFF_S = (3, 5, 10, 20, 30)
CRASH_WINDOW_S = 60
MAX_CRASHES = 5
START_TIMEOUT_S = 90
LOG_DIR = os.path.join(config.ROOT, "work", "logs", "services")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
AGENT_RETRY_S = 120          # a container the agent was asked to restart is not asked again for this long
EXTERNAL_START_S = 180       # a service that never reported after the orchestrator started is "down" after this long
# The admin token's file path stays with the orchestrator; no service it starts needs it.
STRIP_ENV = ("PYTHONHOME", "UV_INTERNAL__PYTHONHOME", "BC_ADMIN_TOKEN_FILE")
# A check may restart a service with test settings. Only these names are accepted, never a credential, and only on
# 127.0.0.1 like every orchestrator route.
ENV_OVERRIDE_RX = re.compile(r"^CAD_RUNNER_TEST_[A-Z0-9_]*$")
# Only the exact test settings listed here pass; a new check that needs another setting adds its name here.
ENV_OVERRIDE_NAMES = frozenset((
    "CAD_RUNNER_TEST_MODE", "CAD_RUNNER_TEST_CAPTURE_FAIL"))             # the build runner's and the catalogue's test faults
# The control routes (/api/restart incl. env, /api/stop, /api/scale, /check/cap) refuse any request a web page could send:
# a browser always adds Origin (on a POST) or Sec-Fetch-Site; local scripts and services.py send neither.
# They also need the admin token (framework/common/admin.py), in every mode.
BROWSER_HEADERS = ("Origin", "Sec-Fetch-Site")
CONTROL_ROUTES = ("/api/restart", "/api/stop", "/api/scale", "/check/cap")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print("%s orchestrator: %s" % (_now(), msg), flush=True)


def _why_code(why):
    """The reason of a (re)start as a code for the structured log."""
    why = why or ""
    if why == "start":
        return "start"
    if "(requested)" in why:
        return "requested"
    if why.startswith("replica"):
        return "scale"
    return "crash" if why.startswith("restart") else "start"


def kill_tree(pid, create_time=None):
    """End `pid` and everything under it. With `create_time`, only when it is still that same process.
    Returns how many processes were ended."""
    if not pid:
        return 0
    try:
        import psutil
        p = psutil.Process(pid)
        if create_time and abs(p.create_time() - create_time) > 1.0:
            return 0
        procs = p.children(recursive=True) + [p]
        for x in procs:
            try:
                x.kill()
            except Exception:
                pass
        psutil.wait_procs(procs, timeout=5)
        return len(procs)
    except Exception:
        return 0


def http_json(url, body=None, timeout=3, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET", headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class Child:
    def __init__(self, name, port, replica=1):
        self.name = name
        self.port = port              # None for an extra replica until its first heartbeat names the port it got
        self.replica = replica
        self.key = name if replica == 1 else "%s#%d" % (name, replica)
        self.idle_s = None            # the replica's own "no command for" seconds (its last heartbeat)
        self.retiring = False
        self.retire_try = 0.0
        self.proc = None
        self.log = None
        self.pid = None               # the service's own process (reported by its heartbeat; answers /health)
        self.create_time = None
        self.state = "stopped"
        self.detail = ""
        self.started_mono = None
        self.hb_mono = None
        self.running_jobs = 0
        self.jobs = []
        self.restarts = 0
        self.crashes = deque()
        self.restart_at = None
        self.restart_requested = False
        self.env_override = {}        # test settings for the next start(s), set through /api/restart (check hook)
        self.expect_exit = None       # "requested" | "hang" | "start" | "stop" | "retire": why the orchestrator ended it
        self.last_exit = ""
        self.dirty = True
        # containers mode: the service is not ours to start
        self.external = False
        self.ident = None             # (pid, create_time) of the process that last reported
        self.place = None             # the container's name (None = this PC)
        self.image = None
        self.stopping = False         # its last report said `stopping` (a deliberate stop)
        self.released = False         # its commands were released for the process in `ident`
        self.exit_sent = False
        self.agent_asked = -1e9

    @property
    def launcher_pid(self):
        return self.proc.pid if self.proc is not None else None

    def row(self, base, mono_now):
        return {"port": self.port, "name": self.name, "base_port": base, "pid": self.pid,
                "launcher_pid": self.launcher_pid, "state": self.state, "detail": self.detail,
                "running_jobs": self.running_jobs, "restarts": self.restarts, "last_exit": self.last_exit,
                "place": self.place, "image": self.image,
                "started_age": None if self.started_mono is None else max(0.0, mono_now - self.started_mono),
                "heartbeat_age": None if self.hb_mono is None else max(0.0, mono_now - self.hb_mono)}

    def public(self, mono_now):
        r = self.row(None, mono_now)
        hb, st = r.pop("heartbeat_age"), r.pop("started_age")
        r.pop("base_port")
        r["where"] = r.pop("place")
        r["heartbeat_age_s"] = None if hb is None else round(hb, 1)
        r["uptime_s"] = None if st is None else round(st, 1)
        r["jobs"] = self.jobs
        return r

    def view(self, mono_now):
        """One replica's view (an entry of the registry row's `replicas`)."""
        hb = None if self.hb_mono is None else round(max(0.0, mono_now - self.hb_mono), 1)
        st = None if self.started_mono is None else round(max(0.0, mono_now - self.started_mono), 1)
        return {"replica": self.replica, "pid": self.pid, "port": self.port, "state": self.state,
                "running_jobs": self.running_jobs, "jobs": self.jobs, "idle_s": self.idle_s,
                "heartbeat_age_s": hb, "uptime_s": st}


class Supervisor:
    def __init__(self, base):
        self.base = base
        self.ports = config.ports(base)
        self.children = {n: Child(n, self.ports[n]) for n in MODULES}      # keyed by Child.key: "catalogue", "catalogue#2"
        self.lock = threading.RLock()
        self.registry = Registry(base)
        self.version = version()
        self.started_mono = time.monotonic()
        self.started_wall = time.time()
        self.stopping = False
        self.job_object = False
        self._own_dirty_at = 0.0
        self.containers = os.environ.get("BC_SUPERVISE") == "containers"       # containers mode
        self.set_id = os.environ.get("BC_SET_ID") or ""

    # ---- processes ---------------------------------------------------------------------------------------------
    def env_for(self, c):
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        env.update(SVC_BASE_PORT=str(self.base), BC_SERVICE=c.name, BC_SERVICES_ROOT=config.ROOT,
                   BC_SERVICES_BASE=str(self.base), SVC_VERSION=self.version, PYTHONUNBUFFERED="1")
        env["BC_REPLICA"] = str(c.replica)
        if c.name == "gateway":
            env["PORT"] = str(c.port)
        env.update(c.env_override)             # after the filter; the names were checked when they were set
        if c.replica > 1:
            env["SVC_PORT"] = "0"               # the system chooses; the replica reports it in its heartbeat
        return env

    def set_env(self, name, env, clear):
        """Check hook: replace (or clear) a service's test settings. Returns (code, body); on 400 nothing changed."""
        if self.containers:
            return 400, {"error": "a container's settings are fixed by the compose file"}
        with self.lock:
            c = self.children.get(name)
            if c is None:
                return 404, {"error": "no service %r" % name}
            if env is not None and not isinstance(env, dict):
                return 400, {"error": "env must be an object of names and text values"}
            bad = [k for k, v in (env or {}).items()
                   if not (isinstance(k, str) and ENV_OVERRIDE_RX.match(k) and k in ENV_OVERRIDE_NAMES and isinstance(v, str))]
            if bad:
                return 400, {"error": "these settings are not accepted: %s" % ", ".join(sorted(map(str, bad)))}
            if clear:
                c.env_override = {}
            if env:
                c.env_override.update(env)
            return 200, {"env_keys": sorted(c.env_override)}

    def start_child(self, c, why="start"):
        os.makedirs(LOG_DIR, exist_ok=True)
        if c.log is not None:
            try:
                c.log.close()
            except Exception:
                pass
        logname = "%s.log" % c.name if c.replica == 1 else "%s-r%d.log" % (c.name, c.replica)
        where = "port %d" % c.port if c.port is not None else "port chosen by the system"
        c.log = open(os.path.join(LOG_DIR, logname), "a", encoding="utf-8", errors="replace")
        c.log.write("\n===== %s %s (%s) =====\n" % (_now(), why, where))
        c.log.flush()
        c.proc = subprocess.Popen([config.VENV_PY, "-m", MODULES[c.name]], cwd=config.ROOT, env=self.env_for(c),
                                  stdin=subprocess.DEVNULL, stdout=c.log, stderr=subprocess.STDOUT, creationflags=NOWIN)
        c.pid = None
        c.create_time = None
        c.hb_mono = None
        c.running_jobs = 0
        c.jobs = []
        c.started_mono = time.monotonic()
        c.state = "starting"
        c.detail = "starting" if why == "start" else why
        c.expect_exit = None
        c.restart_at = None
        c.dirty = True
        c.idle_s = None
        log("%s: %s, launcher pid %d, %s" % (c.key, why, c.proc.pid, where))
        slog.log("svc.child_start", kind="svc", target=c.name, port=c.port, child_pid=c.proc.pid, reason=_why_code(why))

    def end_child(self, c, reason):
        """End a child on purpose (its tree, then the launcher). The monitor sees the exit and acts on `reason`."""
        c.expect_exit = reason
        n = kill_tree(c.pid, c.create_time) if c.pid else 0
        n += kill_tree(c.launcher_pid)
        log("%s: ended (%s), %d process(es)" % (c.key, reason, n))

    def fresh_jobs(self, c):
        """The service's running jobs as it says right now (its /health), or the last heartbeat's when it does not
        answer within 2 s."""
        if c.port is None:                  # an extra replica that has not reported yet
            return c.running_jobs
        try:
            h = http_json(config.service_url(c.name, port=c.port) + "/health", timeout=2)
            return int(h.get("running_jobs") or 0)
        except Exception:
            return c.running_jobs

    # ---- heartbeat -----------------------------------------------------------------------------------------------
    def heartbeat(self, body):
        if self.containers:
            return self._heartbeat_external(body)
        name = body.get("name")
        rep_no = int(body.get("replica") or 1)
        key = name if rep_no == 1 else "%s#%d" % (name, rep_no)
        with self.lock:
            c = self.children.get(key)
            port = int(body.get("port") or 0)
            if c is None or c.proc is None or port == 0:
                return False
            if c.port is None:                                  # an extra replica: it learns its port, we learn it here
                c.port = port
            elif port != c.port:
                return False
            pid = int(body.get("pid") or 0)
            if not self._is_ours(c, pid):
                return False
            c.pid = pid
            c.create_time = body.get("create_time")
            c.hb_mono = time.monotonic()
            c.running_jobs = int(body.get("running_jobs") or 0)
            c.jobs = body.get("jobs") or []
            c.idle_s = (body.get("bus") or {}).get("idle_s")
            if c.state in ("starting", "busy"):
                c.state = "running"
                c.detail = ""
            if c.restart_requested:
                c.state = "draining"
                c.detail = "restart requested; waiting for %d running job(s)" % c.running_jobs
            if c.retiring:
                c.state = "retiring"
            self._dirty_first(c.name)
            c.dirty = True
            return True

    def _heartbeat_external(self, body):
        """A heartbeat in containers mode. Counts when it carries this set's id and the service's own port."""
        name = body.get("name")
        if not self.set_id or body.get("set_id") != self.set_id or int(body.get("replica") or 1) != 1:
            return False
        with self.lock:
            c = self.children.get(name)
            if c is None or int(body.get("port") or 0) != c.port:
                return False
            pid = int(body.get("pid") or 0)
            ct = body.get("create_time")
            ident = (pid, round(float(ct), 2) if ct else None)
            now = time.monotonic()
            if c.ident is not None and ident != c.ident:             # a new process: the service restarted
                if not c.released and DISPATCHER is not None and name != "gateway":
                    # every container process is pid 7 and the new process may have read commands already: release
                    # only the `running` ones the OLD process started (before the new one existed); never by name or pid.
                    st = body.get("started_at")
                    if st:
                        DISPATCHER.service_died(name, c.ident[0], last=True, before=float(st) - 2.0)
                c.restarts += 1
                c.last_exit = "%s: restarted" % _now()
                c.exit_sent = False
                c.crashes.clear()
                slog.log("svc.child_restart", kind="svc", target=name,
                         reason="requested" if c.expect_exit == "requested" else "crash", ms=0)
                c.expect_exit = None
            if c.ident != ident:
                c.released = False
                started = body.get("started_at")
                c.started_mono = now - max(0.0, time.time() - float(started)) if started else now
            c.ident = ident
            c.pid, c.create_time = pid, ct
            c.hb_mono = now
            c.running_jobs = int(body.get("running_jobs") or 0)
            c.jobs = body.get("jobs") or []
            c.idle_s = (body.get("bus") or {}).get("idle_s")
            c.place, c.image = body.get("where"), body.get("image")
            c.stopping = body.get("status") == "stopping"
            if c.stopping:
                c.state, c.detail = "stopping", "stopping"
            elif c.restart_requested or c.exit_sent or body.get("status") == "draining":
                c.state = "draining"
                c.detail = ("restart requested; waiting for %d running job(s)" % c.running_jobs) if c.restart_requested else "restarting"
            else:
                c.state, c.detail = "running", ""
            c.dirty = True
            self._dirty_first(name)
            return True

    def _dirty_first(self, name):
        first = self.children.get(name)
        if first is not None:
            first.dirty = True

    def _is_ours(self, c, pid):
        """A heartbeat counts only from the process this orchestrator started (the launcher or its child)."""
        if pid == c.launcher_pid:
            return True
        try:
            import psutil
            return psutil.Process(pid).ppid() == c.launcher_pid
        except Exception:
            return False

    # ---- the watch loop ------------------------------------------------------------------------------------------
    def tick(self):
        now = time.monotonic()
        with self.lock:
            if self.stopping:
                return
            if self.containers:
                self._tick_external(now)
                self._flush(now)
                return
            for c in list(self.children.values()):
                if c.state in ("stopped", "failed"):
                    continue
                if c.retiring and c.proc is not None and c.proc.poll() is None and c.state == "retiring" \
                        and now - c.retire_try > 5.0:
                    c.retire_try = now                          # the drain request is repeated until the replica exits
                    self._ask_retire(c)
                # 1. exited?
                if c.proc is not None and c.state != "down" and c.proc.poll() is not None:
                    self._on_exit(c, now)
                    continue
                # 2. due for a (re)start?
                if c.state == "down" and c.restart_at is not None and now >= c.restart_at:
                    c.restarts += 1
                    self.start_child(c, why="restart %d" % c.restarts)
                    continue
                # 3. a requested restart whose jobs are done
                if c.restart_requested and c.hb_mono is not None and c.running_jobs == 0:
                    if self.fresh_jobs(c) == 0:
                        c.restart_requested = False
                        self.end_child(c, "requested")
                    continue
                # 4. not starting within the limit
                if c.state == "starting" and c.hb_mono is None and now - c.started_mono > START_TIMEOUT_S:
                    c.last_exit = "%s: did not start within %d s" % (_now(), START_TIMEOUT_S)
                    self.end_child(c, "hang")
                    continue
                # 5. heartbeat too old
                if c.hb_mono is not None and now - c.hb_mono > config.DOWN_AFTER_S:
                    age = int(now - c.hb_mono)
                    if c.running_jobs > 0:
                        if c.state != "busy":
                            log("%s: no heartbeat for %d s but it reported %d running job(s): not restarted"
                                % (c.key, age, c.running_jobs))
                        c.state = "busy"
                        c.detail = "not answering for %d s while running %d job(s); not restarted" % (age, c.running_jobs)
                        c.dirty = True
                    elif c.state != "busy" and self.fresh_jobs(c) == 0:
                        c.last_exit = "%s: no heartbeat for %d s" % (_now(), age)
                        self.end_child(c, "hang")
            self._flush(now)

    # ---- containers mode ---------------------------------------------------------------------------------------------
    def _agent(self, path, body):
        """The host agent is not part of the replica: always (None, {error}), the shape of an agent that cannot be reached.
        A container that does not answer is restarted by Docker (`restart: unless-stopped`)."""
        return None, {"error": "no host agent in this set"}

    def _ask_agent_restart(self, c, now):
        """Ask the host agent to restart a container that does not answer (a no-op in the replica: see _agent)."""
        if now - c.agent_asked < AGENT_RETRY_S:
            return
        c.agent_asked = now
        threading.Thread(target=self._agent_restart_now, args=(c, "/api/container-restart"), daemon=True).start()

    def _agent_restart_now(self, c, route):
        code, out = self._agent(route, {"service": c.name})
        log("%s: not answering; the host agent was asked to restart it: %s" % (c.name, code if code else out.get("error")))
        slog.log("svc.child_restart", kind="svc", level="warn", target=c.name, reason="hang", ms=0)

    def _ask_exit(self, c, force=False):
        """POST /control/exit to a service: it finishes its job(s) and exits 0; Docker or the agent starts it again."""
        try:
            http_json(config.service_url(c.name, port=c.port) + "/control/exit" + ("?force=1" if force else ""), body={}, timeout=3,
                      headers={"X-Control-Token": control.token()})          # the control token
        except Exception:
            return False
        c.exit_sent = True
        c.expect_exit = "requested"
        c.state, c.detail = "draining", "restarting"
        c.dirty = True
        log("%s: asked to exit and start again" % c.key)
        return True

    def _tick_external(self, now):
        for c in list(self.children.values()):
            if c.hb_mono is None:
                if now - self.started_mono > EXTERNAL_START_S and c.state in ("starting", "down"):
                    c.state, c.detail = "down", "no report since the orchestrator started"
                    c.dirty = True
                    self._ask_agent_restart(c, now)
                continue
            age = now - c.hb_mono
            if c.restart_requested and c.running_jobs == 0 and not c.exit_sent and age < config.DOWN_AFTER_S:
                if self.fresh_jobs(c) == 0:
                    c.restart_requested = False
                    if not self._ask_exit(c):
                        c.restart_requested = True       # an upload started in between, or the call failed: ask again
                continue
            if age <= config.DOWN_AFTER_S:
                continue
            # no heartbeat for DOWN_AFTER_S
            if c.stopping or c.exit_sent:
                # it said it was stopping (or was told to exit): it is gone. Release its commands once.
                if not c.released and DISPATCHER is not None and c.name != "gateway":
                    c.released = True
                    DISPATCHER.service_died(c.name, c.pid, last=True)
                if c.state != "down":
                    c.state = "down"
                    c.detail = "stopped" if c.stopping else "not back yet"
                    c.running_jobs, c.jobs = 0, []
                    c.dirty = True
                if c.exit_sent and age > config.DOWN_AFTER_S * 2:
                    self._ask_agent_restart(c, now)      # told to exit but Docker / the agent did not bring it back
                continue
            if c.running_jobs > 0:
                if c.state != "busy":
                    log("%s: no heartbeat for %d s but it reported %d running job(s): not restarted" % (c.key, int(age), c.running_jobs))
                c.state = "busy"
                c.detail = "not answering for %d s while running %d job(s); not restarted" % (age, c.running_jobs)
                c.dirty = True
            elif self.fresh_jobs(c) == 0:
                if c.state != "down":
                    c.last_exit = "%s: no heartbeat for %d s" % (_now(), int(age))
                    log("%s: no heartbeat for %d s and no running job: down" % (c.key, int(age)))
                c.state = "down"
                c.detail = "not answering for %d s; asking for a restart" % age
                c.dirty = True
                self._ask_agent_restart(c, now)

    def _on_exit(self, c, now):
        others = [o for o in self._replicas(c.name)
                  if o is not c and o.state in LIVE_STATES and o.proc is not None and o.proc.poll() is None]
        if DISPATCHER is not None and c.name != "gateway":      # release its commands at once
            DISPATCHER.service_died(c.name, c.pid, last=not others)
        code = c.proc.returncode
        why = c.expect_exit
        slog.log("svc.child_exit", kind="svc", level="info" if why else "warn", target=c.name,
                 child_pid=c.pid or c.launcher_pid, exit_code=code, reason=why or "crash")
        # a launcher whose service process was ended leaves nothing behind; a service whose launcher was ended does
        if c.pid:
            kill_tree(c.pid, c.create_time)
        if c.replica > 1:                                        # an extra replica is never started again
            jobs_lost = c.running_jobs
            del self.children[c.key]
            self._dirty_first(c.name)
            if why in ("retire", "stop"):
                log("%s: retired (exit code %s)" % (c.key, code))
            else:
                before = len(others) + 1
                log("%s: stopped unexpectedly (exit code %s); not started again" % (c.key, code))
                if SCALER is not None and why != "stop":
                    SCALER.note(c.name, "lost", before, before - 1, c.replica, c.pid,
                                "%s: copy %d stopped unexpectedly; its %d job(s) go to another copy (%d → %d copies)."
                                % (config.LABELS[c.name], c.replica, jobs_lost, before, before - 1),
                                {"exit_code": code, "running_jobs": jobs_lost})
            try:
                if c.log is not None:
                    c.log.close()
            except Exception:
                pass
            return
        if why == "requested":
            c.last_exit = "%s: restarted on request" % _now()
            c.restarts += 1
            slog.log("svc.child_restart", kind="svc", target=c.name, reason="requested", ms=0)
            self.start_child(c, why="restart %d (requested)" % c.restarts)
            return
        if why != "hang":
            c.last_exit = "%s: stopped unexpectedly (exit code %s)" % (_now(), code)
        c.crashes.append(now)
        while c.crashes and now - c.crashes[0] > CRASH_WINDOW_S:
            c.crashes.popleft()
        n = len(c.crashes)
        if n >= MAX_CRASHES:
            c.state = "failed"
            c.detail = "stopped %d times within %d s; not started again" % (n, CRASH_WINDOW_S)
            c.restart_at = None
            log("%s: %s" % (c.name, c.detail))
            slog.log("svc.child_failed", kind="svc", level="error", target=c.name, n=n)
        else:
            delay = BACKOFF_S[min(n - 1, len(BACKOFF_S) - 1)]
            c.state = "down"
            c.detail = "starting again in %d s" % delay
            c.restart_at = now + delay
            log("%s: %s; %s" % (c.name, c.last_exit, c.detail))
            slog.log("svc.child_restart", kind="svc", level="warn", target=c.name, reason=why or "crash", ms=delay * 1000)
        c.running_jobs = 0
        c.jobs = []
        c.dirty = True

    # ---- replicas ----------------------------------------------------------------------------------------------------
    def _replicas(self, name):
        return sorted((c for c in self.children.values() if c.name == name), key=lambda c: c.replica)

    def replica_states(self, name):
        """What the scaler needs about each replica of `name`."""
        now = time.monotonic()
        with self.lock:
            out = []
            for c in self._replicas(name):
                v = c.view(now)
                out.append({"replica": c.replica, "state": c.state, "pid": c.pid, "port": c.port,
                            "running_jobs": c.running_jobs, "idle_s": c.idle_s,
                            "heartbeat_age_s": v["heartbeat_age_s"] or 0.0})
            return out

    def all_running(self):
        with self.lock:
            return all(self.children[n].state == "running" for n in MODULES if n in self.children)

    def add_replica(self, name, why):
        """Start the next extra replica of a scalable service. Returns its number, or None."""
        if name not in config.SCALABLE or self.containers:
            return None                       # in containers each service runs as one copy
        with self.lock:
            if self.stopping:
                return None
            n = 2
            while "%s#%d" % (name, n) in self.children:
                n += 1
            c = Child(name, None, n)
            self.children[c.key] = c
            self.start_child(c, why="replica %d (%s)" % (n, why))
            self._dirty_first(name)
            return n

    def _ask_retire(self, c):
        if c.port is None:
            return False
        try:
            http_json(config.service_url(c.name, port=c.port) + "/control/drain?retire=1", body={}, timeout=2)
            return True
        except Exception:
            return False

    def retire_replica(self, name, n, why):
        """Tell replica `n` to finish its job and exit. Never replica 1. True when it was asked."""
        if n <= 1:
            return False
        with self.lock:
            c = self.children.get("%s#%d" % (name, n))
            if c is None or c.proc is None or c.retiring:
                return False
            c.retiring = True
            c.expect_exit = "retire"
            c.state = "retiring"
            c.detail = why
            c.retire_try = time.monotonic()
            c.dirty = True
            self._dirty_first(name)
            ok = self._ask_retire(c)
            if not ok and not self.fresh_jobs(c):
                self.end_child(c, "retire")              # it never reported a port, or does not answer, and has no job
            log("%s: retire requested (%s)" % (c.key, why))
            return True

    def _first_rows(self, now):
        """Rows of the replica-1 children (and so of every service) that changed, with their replicas and scale."""
        rows = []
        for c in self.children.values():
            if c.replica != 1:
                continue
            if c.dirty:
                rows.append(self._row(c, now))
        return rows

    def _scale_of(self, name):
        reps = self._replicas(name)
        if name not in config.SCALABLE or self.containers:
            return {"scalable": False, "replicas": 1}
        view = dict((SCALER.view.get(name) if SCALER is not None else None) or {"scalable": True})
        view["scalable"] = True
        view["replicas"] = sum(1 for c in reps if c.state in LIVE_STATES and c.state != "retiring")
        return view

    def _row(self, c, now):
        """The registry row of a service (replica 1's row): its own facts, the sum of running jobs over every replica,
        the list of replicas and the scale info."""
        row = c.row(self.base, now)
        reps = self._replicas(c.name)
        row["running_jobs"] = sum(x.running_jobs for x in reps)
        row["replicas"] = [x.view(now) for x in reps]
        row["scale"] = self._scale_of(c.name)
        return row

    def _flush(self, now):
        rows = self._first_rows(now)
        if now - self._own_dirty_at >= config.HEARTBEAT_S or rows:
            rows.append(self.own_row(now))
            self._own_dirty_at = now
        if self.registry.save(rows):
            for c in self.children.values():
                c.dirty = False

    def own_row(self, now):
        return {"port": self.ports["orchestrator"], "name": "orchestrator", "base_port": self.base, "pid": os.getpid(),
                "launcher_pid": os.getppid(), "state": "stopping" if self.stopping else "running",
                "detail": "" if (self.job_object or self.containers) else "the services are not tied to the orchestrator's life",
                "running_jobs": 0, "restarts": 0, "last_exit": "",
                "place": os.environ.get("BC_WHERE") or None, "image": os.environ.get("BC_IMAGE") or None,
                "started_age": now - self.started_mono, "heartbeat_age": 0.0, "replicas": [], "scale": {}}

    # ---- commands --------------------------------------------------------------------------------------------------
    def services(self):
        now = time.monotonic()
        with self.lock:
            own = self.own_row(now)
            own = {k: v for k, v in own.items() if k not in ("base_port", "started_age", "heartbeat_age", "replicas", "scale")}
            own["where"] = own.pop("place")
            own.update(heartbeat_age_s=0.0, uptime_s=round(now - self.started_mono, 1), jobs=[])
            own.update(replicas=[], scale={"scalable": False, "replicas": 1})
            rows = [own]
            for c in self.children.values():
                if c.replica != 1:
                    continue
                r = c.public(now)
                reps = self._replicas(c.name)
                r["running_jobs"] = sum(x.running_jobs for x in reps)
                r["replicas"] = [x.view(now) for x in reps]
                r["scale"] = self._scale_of(c.name)
                rows.append(r)
        order = {n: i for i, n in enumerate(config.ORDER)}
        return sorted(rows, key=lambda r: order[r["name"]])

    def running(self):
        with self.lock:
            out = {}
            for c in self.children.values():
                if c.running_jobs:
                    out[c.name] = out.get(c.name, 0) + c.running_jobs
            return out

    def restart(self, name, force=False):
        if self.containers:
            return self._restart_external(name, force)
        with self.lock:
            c = self.children.get(name)
            if c is None:
                return 404, {"error": "no service %r" % name}
            for x in self._replicas(name):                          # the extras stop first
                if x.replica > 1:
                    self.retire_replica(name, x.replica, "restart requested")
            if c.state in ("failed", "stopped", "down"):
                c.crashes.clear()
                c.restarts += 1
                self.start_child(c, why="restart %d (requested)" % c.restarts)
                return 200, {"state": "restarting", "running_jobs": 0}
            jobs = self.fresh_jobs(c)
            if jobs and not force:
                c.restart_requested = True
                c.state = "draining"
                c.detail = "restart requested; waiting for %d running job(s)" % jobs
                c.dirty = True
                try:
                    http_json(config.service_url(c.name, port=c.port) + "/control/drain", body={}, timeout=3)
                except Exception:
                    pass             # the gateway has no drain; its requests are simply waited for
                return 200, {"state": "waiting", "running_jobs": jobs}
            c.restart_requested = False
            self.end_child(c, "requested")
            return 200, {"state": "restarting", "running_jobs": jobs}

    def _restart_external(self, name, force):
        """Drain, then /control/exit; Docker (or the host agent) starts the service again."""
        with self.lock:
            c = self.children.get(name)
            if c is None:
                return 404, {"error": "no service %r" % name}
            if c.state in ("down", "failed") or c.hb_mono is None:
                c.agent_asked = -1e9
                self._ask_agent_restart(c, time.monotonic())
                return 200, {"state": "restarting", "running_jobs": 0}
            jobs = self.fresh_jobs(c)
            if jobs and not force:
                c.restart_requested = True
                c.state = "draining"
                c.detail = "restart requested; waiting for %d running job(s)" % jobs
                c.dirty = True
                try:
                    http_json(config.service_url(c.name, port=c.port) + "/control/drain", body={}, timeout=3)
                except Exception:
                    pass
                return 200, {"state": "waiting", "running_jobs": jobs}
            c.restart_requested = False
            if not self._ask_exit(c, force=bool(jobs)):
                c.agent_asked = -1e9
                self._ask_agent_restart(c, time.monotonic())
            return 200, {"state": "restarting", "running_jobs": jobs}

    def stop(self, force=False):
        if self.containers:
            return 409, {"error": "In containers the set is stopped with: python scripts/services.py down --containers"}
        with self.lock:
            busy = {}
            for c in list(self.children.values()):
                if c.state not in ("stopped", "failed"):
                    n = self.fresh_jobs(c)
                    if n:
                        busy[c.name] = busy.get(c.name, 0) + n
            if busy and not force:
                return 409, {"error": "running jobs", "running": busy}
            self.stopping = True
        threading.Thread(target=self._stop_all, daemon=True).start()
        return 200, {"ok": True, "stopping": [c.name for c in self.children.values() if c.replica == 1]}

    def _stop_all(self):
        time.sleep(0.3)                     # let the HTTP answer go out
        now = time.monotonic()
        with self.lock:
            for c in list(self.children.values()):
                c.expect_exit = "stop"
                kill_tree(c.pid, c.create_time)
                kill_tree(c.launcher_pid)
                c.state, c.detail, c.running_jobs, c.jobs = "stopped", "", 0, []
                c.dirty = True
            rows = [self._row(c, now) for c in self.children.values() if c.replica == 1]
            own = self.own_row(now)
            own["state"] = "stopped"
            self.registry.save(rows + [own])
        log("stopped every service; exiting (the job object ends anything left)")
        os._exit(0)

    def start_external(self):
        """Nothing is started; every service is expected to report (compose and the host agent start them)."""
        log("pid %d, base port %d, containers mode (set %s), version %s" % (os.getpid(), self.base, self.set_id or "?", self.version))
        if not self.set_id:
            log("BC_SET_ID is not set: no heartbeat will be accepted")
        self.registry.reset(self.ports.values())
        with self.lock:
            now = time.monotonic()
            for c in self.children.values():
                c.external = True
                c.state, c.detail = "starting", "waiting for its first report"
                c.started_mono = now
            self._flush(now)

    def start_all(self):
        self.job_object = tie_children_to_me()
        log("pid %d, base port %d, job object %s, version %s" % (os.getpid(), self.base, self.job_object, self.version))
        self.registry.reset(self.ports.values())
        with self.lock:
            for c in self.children.values():
                self.start_child(c)
            self._flush(time.monotonic())

    def watch_forever(self):
        while True:
            try:
                self.tick()
            except Exception as exc:          # the supervisor never dies of its own bookkeeping
                log("watch loop error: %s: %s" % (type(exc).__name__, exc))
            time.sleep(0.5)


def make_handler(sup):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if not (self.path.startswith("/api/heartbeat") or self.path.startswith("/health")
                    or self.path.startswith("/api/services")):
                log("http " + fmt % args)

        def _send(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return {}

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/health":
                disp = DISPATCHER.health() if DISPATCHER is not None else None
                return self._send(200, {"service": "orchestrator", "status": "stopping" if sup.stopping else "ok",
                                        "pid": os.getpid(), "port": sup.ports["orchestrator"], "version": sup.version,
                                        "started_at": sup.started_wall,
                                        "uptime_s": round(time.monotonic() - sup.started_mono, 1),
                                        "running_jobs": disp["jobs_open"] if disp else 0, "jobs": [],
                                        "queues": disp["queues"] if disp else {}, "dispatcher": disp,
                                        "job_object": sup.job_object,
                                        "registry_ok": sup.registry.ok, "base_port": sup.base,
                                        "root": config.ROOT,
                                        "supervise": "containers" if sup.containers else None, "set_id": sup.set_id or None,
                                        "scaling": SCALER.health() if SCALER is not None else None,
                                        "retention": RETENTION.health() if RETENTION is not None else None})
            if path == "/api/services":
                return self._send(200, {"services": sup.services(), "base_port": sup.base,
                                        "registry_ok": sup.registry.ok})
            if path == "/api/scale":
                if SCALER is None:
                    return self._send(503, {"error": "the scaler is starting"})
                return self._send(200, SCALER.health())
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path in CONTROL_ROUTES and any(self.headers.get(h) is not None for h in BROWSER_HEADERS):
                return self._send(403, {"error": "not allowed from a web page"})
            if path in CONTROL_ROUTES:
                code, err = admin.check(self.headers)       # the admin token, in every mode
                if code != 200:
                    return self._send(code, {"error": err})       # nothing read, nothing changed
            body = self._body()
            if path == "/api/heartbeat":
                return self._send(200 if sup.heartbeat(body) else 409, {"ok": True})
            if path == "/api/restart":
                if body.get("env") is not None or body.get("env_clear"):
                    code, out = sup.set_env(body.get("service"), body.get("env"), bool(body.get("env_clear")))
                    if code != 200:
                        return self._send(code, out)          # nothing changed, nothing restarted
                code, out = sup.restart(body.get("service"), force=bool(body.get("force")))
                return self._send(code, out)
            if path == "/api/stop":
                code, out = sup.stop(force=bool(body.get("force")))
                return self._send(code, out)
            if path == "/api/scale":
                if SCALER is None:
                    return self._send(503, {"error": "the scaler is starting"})
                code, out = SCALER.request(body)
                return self._send(code, out)
            if path == "/check/cap":                      # check hook (127.0.0.1 only): a concurrency cap for a check
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                service = (q.get("service") or [""])[0]
                try:
                    n = int((q.get("n") or ["-1"])[0])
                except ValueError:
                    n = -1
                if DISPATCHER is None or service not in policy.CAPS or n < 0:
                    return self._send(400, {"ok": False, "error": "service must be one of %s and n >= 0 (0 restores the policy)"
                                            % sorted(policy.CAPS)})
                return self._send(200, {"ok": True, "caps": DISPATCHER.set_cap(service, n)})
            return self._send(404, {"error": "not found"})

    return H


def main():
    base = config.base_port()
    bad = config.bad_ports(base)
    if bad:
        log("refusing base port %d: ports %s are listed in BC_RESERVED_PORTS" % (base, bad))
        return 2
    slog.setup("orchestrator")             # this process writes the structured log as the orchestrator
    sup = Supervisor(base)
    server = ThreadingHTTPServer((config.bind_host(), sup.ports["orchestrator"]), make_handler(sup))
    server.daemon_threads = True
    global DISPATCHER, SCALER, RETENTION
    DISPATCHER = Dispatcher(base)          # runs bus_schema.ensure() itself, so a missing database never stops us
    DISPATCHER.start()
    from framework.orchestrator import retention
    RETENTION = retention.start_thread()   # once a day, prune archives and logs older than 7 days (own thread, own connection)
    if os.environ.get("BC_SUPERVISE") == "containers":
        sup.start_external()               # the services run in containers (and the host agent); this only watches them
        from framework.orchestrator.scaler import Scaler
        SCALER = Scaler(sup)               # off in containers mode ("Each service runs as one copy in containers.")
        SCALER.start()
    elif os.environ.get("BC_SUPERVISE") == "0":
        # the orchestrator alone (for checks of the dispatcher): the dispatcher and the HTTP server run,
        # no service is started, watched or restarted, and no scaler runs. Nothing changes when the variable is unset.
        log("orchestrator alone (BC_SUPERVISE=0): no services started")
    else:
        sup.start_all()
        from framework.orchestrator.scaler import Scaler
        SCALER = Scaler(sup)               # starts and retires replicas by the queues' waiting counts
        SCALER.start()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    log("listening on http://%s:%d" % (config.bind_host(), sup.ports["orchestrator"]))
    sup.watch_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
