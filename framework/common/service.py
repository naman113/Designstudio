"""The shared service base: `/health`, the heartbeat to the orchestrator, running-job bookkeeping.

Every service process uses it:

    Service("catalogue", handlers={}).run()          a worker: stdlib HTTP server on its port (framework/common/config)
    Reporter("gateway", port, running_jobs=fn)       the gateway (FastAPI) reuses only the health + heartbeat part

`/health` answers status, pid, port, version, uptime, running jobs, queue depths
and the framework packages this process has loaded (an architecture check reads them).

The heartbeat is an HTTP POST to the orchestrator every HEARTBEAT_S seconds with the same facts; the orchestrator
records it in `orch.services`. A service with a running job is never restarted by the orchestrator, so a service
reports its running jobs truthfully: wrap the work in `with service.job("kind"):`.

Check hooks (127.0.0.1 only, used by the service checks):
    POST /check/hold?seconds=N          a no-op job that runs N seconds (the service "has a running job")
    POST /check/hang?seconds=N[&hold=1] the service stops answering /health and stops its heartbeat for N seconds,
                                        optionally while holding a job
    POST /control/drain                 take no new jobs (the orchestrator sends it before a requested restart)
    POST /control/drain?retire=1        an extra replica stops reading, finishes its job and exits 0 (replica 1: 409)
    POST /control/exit[?force=1]        take no new jobs, finish the running one(s), exit 0 (Docker's restart policy or the host
                                        agent starts it again); force=1 exits at once. The orchestrator sends it for a requested restart
                                        of a service it does not own (BC_SUPERVISE=containers)

Replicas: env BC_REPLICA is this process's number (default 1) and SVC_PORT=0 asks for a port the
system chooses (extra replicas); the bound port is what /health and the heartbeat report.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from framework.common import config, control, slog

MAX_HOLD_S = 600
MAX_HANG_S = 300
_VERSION = None


def version():
    """The work copy's commit (short sha), or "unknown". The orchestrator passes it to its children (SVC_VERSION)."""
    global _VERSION
    if _VERSION is None:
        _VERSION = os.environ.get("SVC_VERSION") or ""
        if not _VERSION:
            try:
                _VERSION = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=config.ROOT, capture_output=True,
                                          text=True, timeout=10,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
            except Exception:
                _VERSION = ""
        _VERSION = _VERSION or "unknown"
    return _VERSION


def _create_time():
    try:
        import psutil
        return psutil.Process().create_time()
    except Exception:
        return None


def _end_orphan(name):
    """macOS / Linux: there is no kill-on-close job object (framework/common/winjob.py is Windows only). When the
    orchestrator that started this service has gone (this process was handed to init), end this service's own tree and
    exit, as the job object does on Windows. Checked once per heartbeat."""
    print("%s: the orchestrator is gone; this service ends too" % name, flush=True)
    try:
        import psutil
        for p in psutil.Process().children(recursive=True):
            try:
                p.kill()
            except Exception:
                pass
    except Exception:
        pass
    os._exit(0)


def framework_packages():
    return sorted({m.split(".")[1] for m in list(sys.modules) if m.startswith("framework.") and m.count(".") >= 1})


class Reporter:
    """Health facts + the heartbeat thread. `running_jobs` and `jobs` are callables (the owner's own bookkeeping)."""

    def __init__(self, name, port=None, running_jobs=None, jobs=None):
        self.name = name
        slog.setup(name)                  # this process writes its structured log lines as `name`
        self.base = config.base_port()
        self.port = port if port is not None else config.port_of(name, self.base)
        self.replica = int(os.environ.get("BC_REPLICA") or 1)
        self.started = time.time()
        self.create_time = _create_time()
        self._running_jobs = running_jobs or (lambda: 0)
        self._jobs = jobs or (lambda: [])
        self.draining = False
        self.stopping = False          # SIGTERM received (a container stop); the last heartbeat says so
        self.hang_until = 0.0          # check hook: monotonic time until which this process plays dead
        self.beats = 0
        self._stop = threading.Event()

    def running_jobs(self):
        try:
            return int(self._running_jobs())
        except Exception:
            return 0

    def health(self):
        return {"service": self.name, "status": "stopping" if self.stopping else ("draining" if self.draining else "ok"),
                "pid": os.getpid(),
                "replica": self.replica, "port": self.port, "version": version(), "started_at": self.started,
                "uptime_s": round(time.time() - self.started, 1), "running_jobs": self.running_jobs(),
                "jobs": self._jobs(), "queues": {}, "packages": framework_packages(),
                "orchestrator": config.service_url("orchestrator", self.base),
                # which set this process belongs to and where it runs (the Services page); unset = a host set, "this PC"
                "set_id": os.environ.get("BC_SET_ID") or None, "where": os.environ.get("BC_WHERE") or None,
                "image": os.environ.get("BC_IMAGE") or None}

    def hung(self):
        return time.monotonic() < self.hang_until

    def beat(self):
        """One heartbeat. Returns True when the orchestrator took it."""
        body = dict(self.health(), name=self.name, create_time=self.create_time)
        req = urllib.request.Request(config.service_url("orchestrator", self.base) + "/api/heartbeat",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                r.read()
            self.beats += 1
            return True
        except Exception:
            return False

    def start_heartbeat(self):
        parent = os.getppid()

        def loop():
            while not self._stop.is_set():
                if not self.hung():
                    self.beat()
                if os.name != "nt" and os.environ.get("BC_SERVICES_ROOT") and os.getppid() != parent:
                    _end_orphan(self.name)
                self._stop.wait(config.HEARTBEAT_S)
        t = threading.Thread(target=loop, name="heartbeat-%s" % self.name, daemon=True)
        t.start()
        return t


class Service(Reporter):
    """A worker service: stdlib HTTP server (health, control, check hooks) + heartbeat + job bookkeeping.
    `handlers` maps a command type to a function(ctx, payload) -> result, run by the queue consumer."""

    def __init__(self, name, handlers=None, queries=None):
        self._jobs_lock = threading.Lock()
        self._running = {}
        raw_port = os.environ.get("SVC_PORT", "")
        super().__init__(name, port=int(raw_port) if raw_port != "" else None,
                         running_jobs=lambda: len(self._running), jobs=self._job_list)
        self.handlers = dict(handlers or {})
        self.retiring = False
        self._retire_started = False
        self._last_end_beat = 0.0
        self.consumer = None                  # the queue consumer (framework/common/consumer.py), started by run()
        self.queries = dict(queries or {})    # page-read type -> handler; answered by the Answerer on the query queue
        self.answerer = None
        self.extra_health = None              # a service may add facts to /health (the geometry pool)

    def health(self):
        h = super().health()
        if self.consumer is not None:
            h["queues"] = self.consumer.depths()
            h["bus"] = self.consumer.stats()
        if self.answerer is not None:
            h["queues"] = dict(h["queues"], **self.answerer.depths())
            h["queries"] = self.answerer.stats()
        if self.extra_health is not None:
            h.update(self.extra_health())
        return h

    def _job_list(self):
        now = time.time()
        with self._jobs_lock:
            return [{"id": j, "kind": k, "age_s": round(now - t, 1)} for j, (k, t) in self._running.items()]

    @contextmanager
    def job(self, kind, job_id=None):
        """Bookkeeping for one running job (`job_id` given: its first 12 characters are the bookkeeping id). The
        orchestrator hears about it at once (an extra heartbeat), so a service that freezes right after starting a
        job is still known to have one."""
        jid = str(job_id)[:12] if job_id else uuid.uuid4().hex[:12]
        with self._jobs_lock:
            self._running[jid] = (kind, time.time())
        threading.Thread(target=self.beat, daemon=True).start()
        try:
            yield jid
        finally:
            with self._jobs_lock:
                self._running.pop(jid, None)
            now = time.monotonic()                 # a throttled end beat, so a burst of short jobs is not a burst
            if now - self._last_end_beat > 1.0:    # of heartbeats (the start beat above stays: it is a safety rule)
                self._last_end_beat = now
                threading.Thread(target=self.beat, daemon=True).start()

    # ---- check hooks -------------------------------------------------------------------------------------------
    def hold(self, seconds, kind="check.hold"):
        if self.draining:
            return None
        seconds = max(0.0, min(float(seconds), MAX_HOLD_S))
        started = threading.Event()
        box = {}

        def run():
            with self.job(kind) as jid:
                box["id"] = jid
                started.set()
                time.sleep(seconds)
        threading.Thread(target=run, daemon=True).start()
        started.wait(5)
        return box.get("id")

    def hang(self, seconds, hold=False):
        seconds = max(0.0, min(float(seconds), MAX_HANG_S))
        jid = self.hold(seconds, kind="check.hang") if hold else None
        if hold:
            self.beat()                       # the job is reported before the service goes quiet
        self.hang_until = time.monotonic() + seconds
        return jid

    # ---- SIGTERM ---------------------------------------------------------------------------------
    def stop_gracefully(self, grace_s=None):
        """A container stop (SIGTERM): take no new commands, send a last heartbeat with status `stopping`, let the running job(s)
        finish for up to BC_STOP_GRACE_S seconds (default: the longest command limit), then exit 0 at once when none runs."""
        if getattr(self, "_stop_started", False):
            return
        self._stop_started = True
        self.draining = True
        self.stopping = True
        if grace_s is None:
            from framework.common import policy
            try:
                grace_s = float(config.setting("BC_STOP_GRACE_S", policy.LONG_ROW_S))
            except (TypeError, ValueError):
                grace_s = float(policy.LONG_ROW_S)      # a bad value never raises in the signal handler
            if not grace_s >= 0:
                grace_s = float(policy.LONG_ROW_S)
        threading.Thread(target=self._stop_loop, args=(float(grace_s),), name="stop-%s" % self.name, daemon=True).start()

    def _beat_quietly(self):
        try:
            self.beat()
        except Exception as exc:                  # a heartbeat that fails never keeps a stopping service alive
            print("%s: heartbeat: %s: %s" % (self.name, type(exc).__name__, exc), flush=True)

    def _stop_loop(self, grace_s):
        print("%s: stop requested; finishing %d running job(s), at most %d s" % (self.name, self.running_jobs(), grace_s), flush=True)
        self._beat_quietly()
        end = time.monotonic() + grace_s
        quiet = 0
        while time.monotonic() < end:
            c = self.consumer
            if self.running_jobs() == 0 and (c is None or c.idle()):
                quiet += 1
                if quiet >= 2:
                    break
            else:
                quiet = 0
            time.sleep(0.25)
        print("%s: stopping (%d job(s) still running)" % (self.name, self.running_jobs()), flush=True)
        self._beat_quietly()
        # Consumer.close waits for running handlers; never block past the grace on one: each close runs in a
        # daemon thread, joined for 5 s, then the child processes end and the service exits 0.
        for obj in (self.consumer, getattr(self, "answerer", None)):
            if obj is None:
                continue
            def _close(o=obj):
                try:
                    o.close()
                except Exception as exc:
                    print("%s: closing: %s: %s" % (self.name, type(exc).__name__, exc), flush=True)
            t = threading.Thread(target=_close, name="close-%s" % self.name, daemon=True)
            t.start()
            t.join(5)
        from framework.common import winjob
        winjob.kill_descendants()                 # os._exit skips atexit: end this service's own child processes here
        os._exit(0)

    def _install_sigterm(self):
        """POSIX only (Windows has no SIGTERM from the orchestrator: it ends a service with taskkill, unchanged). Main thread only."""
        if os.name == "nt":
            return
        import signal
        try:
            signal.signal(signal.SIGTERM, lambda _s, _f: self.stop_gracefully())     # replaces winjob's kill-and-die handler
        except (ValueError, OSError):
            pass

    # ---- exit ------------------------------------------------------------------------------------------------------
    def exit_when_idle(self, force=False):
        """POST /control/exit: stop reading, finish the running job(s), exit 0. The caller (the orchestrator) has already seen no
        running job; a job that slipped in is still finished. `force` exits at once. Any replica, the first included."""
        self.draining = True
        if not getattr(self, "_exit_started", False):
            self._exit_started = True
            threading.Thread(target=self._exit_loop, args=(bool(force),), name="exit-%s" % self.name, daemon=True).start()
        return True

    def _exit_loop(self, force):
        quiet = 0
        time.sleep(0.2)                                 # let the HTTP answer go out
        while not force:
            c = self.consumer
            if self.running_jobs() == 0 and (c is None or c.idle()):
                quiet += 1
            else:
                quiet = 0
            if quiet >= 2:
                break
            time.sleep(0.5)
        print("%s: exit requested; %s; exiting" % (self.name, "forced" if force else "no running job"), flush=True)
        for obj in (self.consumer, getattr(self, "answerer", None)):
            if obj is None:
                continue
            def _close(o=obj):
                try:
                    o.close()
                except Exception as exc:
                    print("%s: closing: %s: %s" % (self.name, type(exc).__name__, exc), flush=True)
            t = threading.Thread(target=_close, name="close-%s" % self.name, daemon=True)
            t.start()
            t.join(5)
        from framework.common import winjob
        winjob.kill_descendants()
        os._exit(0)

    # ---- retire ---------------------------------------------------------------------------------
    def retire(self):
        """Stop reading, finish the current job, exit 0. Replica 1 is never retired (returns False)."""
        if self.replica == 1:
            return False
        self.draining = True
        self.retiring = True
        if not self._retire_started:
            self._retire_started = True
            threading.Thread(target=self._retire_loop, name="retire-%s" % self.name, daemon=True).start()
        return True

    def _retire_loop(self):
        quiet = 0
        while True:
            time.sleep(0.5)
            c = self.consumer
            if self.running_jobs() == 0 and (c is None or c.idle()):
                quiet += 1
            else:
                quiet = 0
            if quiet >= 2:
                break
        print("%s replica %d: drained, no running job; exiting" % (self.name, self.replica), flush=True)
        try:
            if self.consumer is not None:
                self.consumer.close(timeout_s=5)
            answerer = getattr(self, "answerer", None)           # the query answerer, when this service has one
            if answerer is not None:
                answerer.close()
        except Exception as exc:
            print("%s replica %d: closing: %s: %s" % (self.name, self.replica, type(exc).__name__, exc), flush=True)
        os._exit(0)

    # ---- HTTP ----------------------------------------------------------------------------------------------------
    def _handler(self):
        svc = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):      # one line per non-health request; health polls stay quiet
                if not self.path.startswith("/health"):
                    sys.stdout.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
                    sys.stdout.flush()

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                path = urllib.parse.urlparse(self.path).path
                if path == "/health":
                    while svc.hung():                 # playing dead: answer only once the hang is over
                        time.sleep(0.5)
                    return self._send(200, svc.health())
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                u = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                if u.path == "/control/drain":
                    if q.get("retire") in ("1", "true", "yes"):
                        if not svc.retire():
                            return self._send(409, {"error": "the first replica is never retired"})
                        return self._send(200, {"ok": True, "retiring": True, "running_jobs": svc.running_jobs()})
                    svc.draining = True
                    slog.log("svc.drain", kind="svc")
                    return self._send(200, {"ok": True, "running_jobs": svc.running_jobs()})
                if u.path == "/control/exit":
                    code, err = control.check(self.headers)          # 404 unless a containers set; token needed
                    if code != 200:
                        return self._send(code, {"error": err})
                    svc.exit_when_idle(force=q.get("force") in ("1", "true", "yes"))
                    return self._send(200, {"ok": True, "exiting": True, "running_jobs": svc.running_jobs()})
                if u.path == "/check/hold":
                    jid = svc.hold(q.get("seconds", "30"))
                    if jid is None:
                        return self._send(409, {"error": "draining"})
                    return self._send(200, {"ok": True, "job": jid, "running_jobs": svc.running_jobs()})
                if u.path == "/check/hang":
                    jid = svc.hang(q.get("seconds", "45"), hold=q.get("hold") in ("1", "true", "yes"))
                    return self._send(200, {"ok": True, "job": jid, "hang_s": q.get("seconds", "45")})
                return self._send(404, {"error": "not found"})

        return H

    def run(self):
        server = ThreadingHTTPServer((config.bind_host(), self.port), self._handler())
        server.daemon_threads = True
        self.port = server.server_address[1]              # the port the system chose when SVC_PORT=0
        self._install_sigterm()
        print("%s service: pid %d on http://%s:%d (version %s, replica %d, handlers: %s, queries: %d)"
              % (self.name, os.getpid(), config.bind_host(), self.port, version(), self.replica,
                 ", ".join(sorted(self.handlers)) or "none yet", len(self.queries)), flush=True)
        slog.log("svc.start", kind="svc", port=self.port, version=version())
        self.start_heartbeat()
        if os.environ.get("BC_BUS") != "0":              # BC_BUS=0: a set started without the bus
            from framework.common import policy
            from framework.common.consumer import Consumer
            self.consumer = Consumer(self, self.handlers, policy.POOL.get(self.name, 1))
            self.consumer.start()
            if self.queries:
                from framework.common.answerer import Answerer
                self.answerer = Answerer(self, self.queries)
                self.answerer.start()
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            if self.answerer is not None:
                self.answerer.close()
            if self.consumer is not None:
                self.consumer.close()
            server.server_close()
