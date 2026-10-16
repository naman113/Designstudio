"""The geometry service's worker pool.

A pool of warm `spawn` worker PROCESSES, one job at a time each. Messages carry paths and small dicts only: a worker
writes its full result to a JSON file (framework/kernel/ops.py) and returns the path; shapes are never pickled. A job
that runs past its time limit, or that the user stops, has its worker killed (TerminateProcess) and a fresh warm worker
takes its place; a worker is also replaced after MAX_JOBS jobs or when its memory passes MAX_RSS_MB.

Measured (build123d 0.12.0, 20-core PC): a warm worker costs 2.7 s of import and 380 MB; four workers ran
8 jobs in 6.8 s against 26.7 s on one worker (3.9x), while four threads gave 1.2x because the kernel holds the GIL in
places. Threads also cannot be killed, so a runaway OCP call would hold a thread (and its slot) for good.

Rules this module keeps (measured pitfalls):
  1. Pool creation is inside the caller's main(): spawn re-imports the entry module, so no import-time pool.
  2. The project root is a parameter (config.ROOT of THIS copy) put first on sys.path inside the worker, which asserts
     it imported this copy's kernel and exits otherwise.
  3. A worker exits when the pipe to its parent closes (no orphans after a service crash); daemon=True besides.
No state is shared between replicas of the service: each replica has its own pool.
"""
import multiprocessing as mp
import os
import threading
import time
import traceback

from framework.common import slog

MAX_JOBS = 50            # recycle a worker after this many jobs
MAX_RSS_MB = 3000        # ...or when its memory exceeds this after a job
READY_TIMEOUT_S = 90     # a worker must report ready within this (measured: 2.7-3 s)
POLL_S = 0.25            # how often a running job looks at its stop flag and its time limit


def _worker(conn, root):
    """Runs in the child process. Warms the kernel once, then answers jobs on the pipe until the pipe closes."""
    import sys
    sys.path.insert(0, root)
    import psutil
    from framework.kernel import io, measure, mesh  # noqa: F401  (warm: 2.7 s, once)
    import framework.kernel as kernel_pkg
    if not os.path.normcase(os.path.abspath(kernel_pkg.__file__)).startswith(os.path.normcase(os.path.abspath(root))):
        print("geometry worker imported the wrong kernel: %s (expected under %s)" % (kernel_pkg.__file__, root),
              flush=True)
        os._exit(3)
    from framework.kernel import ops, shapes
    proc = psutil.Process()
    done = 0
    conn.send({"ready": True, "pid": os.getpid()})
    while True:
        try:
            job = conn.recv()
        except (EOFError, OSError):
            return                                  # the parent is gone: exit (no orphans)
        if job is None:
            return
        t0 = time.time()
        ops.PROGRESS = lambda d: conn.send({"progress": d})          # ops that report progress send it to their handler
        try:
            res = ops.run(job["op"], job["args"])
            done += 1
            rss = proc.memory_info().rss / 1e6
            reply = {"ok": True, "res": res, "sec": round(time.time() - t0, 2), "rss_mb": round(rss), "jobs": done,
                     "recycle": done >= MAX_JOBS or rss > MAX_RSS_MB}
        except BaseException as exc:                # noqa: BLE001  (a worker never dies of its own job's error)
            nodump = isinstance(exc, FileNotFoundError) and str(exc).startswith("no BREP dump")
            reply = {"ok": False, "err": repr(exc), "tb": traceback.format_exc()[-800:],
                     "kind": "nodump" if nodump else "error"}
        ops.PROGRESS = None
        shapes._MODEL_CACHE.clear()                 # bounded memory; the cache only saves reloads, never changes a number
        import gc
        gc.collect()
        try:
            conn.send(reply)
        except (OSError, ValueError):
            return


class Slot:
    """One worker process and its pipe."""

    def __init__(self, pool, n):
        self.pool, self.n = pool, n
        self.p = None
        self.parent = None
        self.pid = None
        self.state = "starting"              # starting | ready | busy
        self.jobs = 0
        self.rss_mb = 0

    def spawn(self):
        """Start a worker and wait until it is warm. Raises RuntimeError when it does not become ready."""
        self.state = "starting"
        parent, child = self.pool.ctx.Pipe()
        p = self.pool.ctx.Process(target=_worker, args=(child, self.pool.root), daemon=True,
                                  name="geometry-worker-%d" % self.n)
        p.start()
        child.close()
        ok = False
        try:
            if parent.poll(READY_TIMEOUT_S):
                ok = bool(parent.recv().get("ready"))
        except (EOFError, OSError):
            ok = False
        if not ok:
            try:
                p.kill()
                p.join(5)
            finally:
                parent.close()
            raise RuntimeError("a geometry worker did not become ready")
        self.p, self.parent, self.pid = p, parent, p.pid
        self.jobs = 0
        self.state = "ready"

    def kill(self):
        """Terminate the worker (TerminateProcess) and close the pipe."""
        p, parent = self.p, self.parent
        self.state = "starting"
        if p is not None:
            try:
                p.kill()
                p.join(5)
            except Exception:
                pass
        if parent is not None:
            try:
                parent.close()
            except Exception:
                pass

    def run(self, op, args, timeout, cancel=None, on_message=None):
        """One job on this worker. Polls the pipe in POLL_S slices: a set `cancel` event kills the worker and returns
        {"stopped": True}; the time limit kills it and returns {"timeout": True}; a broken pipe or a dead process
        returns {"lost": True}. Otherwise the worker's own reply. A message `{"progress": {...}}` from the worker is
        not the reply: `on_message` (when given) is called with its content and the wait goes on."""
        self.state = "busy"
        try:
            self.parent.send({"op": op, "args": args})
        except (OSError, ValueError):
            return {"ok": False, "lost": True}
        end = time.monotonic() + timeout
        while True:
            try:
                if self.parent.poll(POLL_S):
                    msg = self.parent.recv()
                    if isinstance(msg, dict) and "progress" in msg and "ok" not in msg:
                        if on_message is not None:
                            try:
                                on_message(msg["progress"])
                            except Exception:                    # a handler's callback never breaks the job
                                pass
                        continue
                    return msg
            except (EOFError, OSError):
                return {"ok": False, "lost": True}
            if cancel is not None and cancel.is_set():
                return {"ok": False, "stopped": True}
            if time.monotonic() >= end:
                return {"ok": False, "timeout": True}
            if not self.p.is_alive():
                return {"ok": False, "lost": True}


class Pool:
    def __init__(self, n, root):
        self.n = max(1, int(n))
        self.root = root
        self.ctx = mp.get_context("spawn")           # the only start method on Windows
        self.slots = [Slot(self, i + 1) for i in range(self.n)]
        self.free = []
        self.cv = threading.Condition()
        self.closed = False
        self.counters = {"jobs": 0, "killed": 0, "recycled": 0, "lost": 0}
        threads = [threading.Thread(target=self._bring_up, args=(s,), name="geometry-spawn-%d" % s.n, daemon=True)
                   for s in self.slots]              # in parallel: 4 in a row take 10.8 s (measured)
        for t in threads:
            t.start()

    # ---- slots -----------------------------------------------------------------------------------------------------
    def _bring_up(self, slot):
        """Spawn the slot's worker (retrying every 5 s) and put the slot on the free list."""
        while not self.closed:
            try:
                slot.spawn()
                break
            except Exception as exc:
                print("geometry pool: worker %d did not start (%s); trying again" % (slot.n, exc), flush=True)
                time.sleep(5)
        with self.cv:
            if not self.closed and slot.state == "ready":
                self.free.append(slot)
                self.cv.notify()

    def _replace(self, slot):
        """Kill the slot's worker and bring a fresh one up in the background (the slot rejoins the free list)."""
        slot.kill()
        threading.Thread(target=self._bring_up, args=(slot,), name="geometry-respawn-%d" % slot.n, daemon=True).start()

    def _take(self, cancel):
        with self.cv:
            while True:
                if self.closed:
                    return None
                if self.free:
                    return self.free.pop(0)
                if cancel is not None and cancel.is_set():
                    return None
                self.cv.wait(POLL_S)

    # ---- the one call the handlers make ---------------------------------------------------------------------------
    def submit(self, op, args, timeout, cancel=None, on_message=None):
        """Run one job on a free worker (waits for one). -> the worker's reply dict, or {"ok": False, "stopped" |
        "timeout" | "lost": True}. The worker is killed and replaced on stop, time-out and loss; after a job it is
        replaced when the reply says `recycle`."""
        slot = self._take(cancel)
        if slot is None:
            return {"ok": False, "stopped": True} if (cancel is not None and cancel.is_set()) else \
                {"ok": False, "lost": True}
        pid = slot.pid
        t0 = time.monotonic()
        r = slot.run(op, args, timeout, cancel, on_message)
        r["pid"] = pid
        # one line per worker job (the worker's own output is never copied into the structured log)
        slog.log("pool.run", proc="worker", op=op if isinstance(op, str) else None, child_pid=pid,
                 ms=round((time.monotonic() - t0) * 1000),
                 status="stopped" if r.get("stopped") else "timeout" if r.get("timeout") else "lost" if r.get("lost")
                 else "ok" if r.get("ok") else "error",
                 level="info" if r.get("ok") else "warn")
        with self.cv:
            self.counters["jobs"] += 1
            if r.get("stopped") or r.get("timeout"):
                self.counters["killed"] += 1
            elif r.get("lost"):
                self.counters["lost"] += 1
            elif r.get("recycle"):
                self.counters["recycled"] += 1
        if r.get("ok"):
            slot.jobs = r.get("jobs", slot.jobs)
            slot.rss_mb = r.get("rss_mb", slot.rss_mb)
        if r.get("stopped") or r.get("timeout") or r.get("lost") or r.get("recycle"):
            slog.log("pool.recycle", proc="worker", child_pid=pid,
                     reason="stop" if r.get("stopped") else "timeout" if r.get("timeout") else "crash" if r.get("lost")
                     else "jobs" if (r.get("jobs") or 0) >= MAX_JOBS else "rss")
            self._replace(slot)
        else:
            slot.state = "ready"
            with self.cv:
                self.free.append(slot)
                self.cv.notify()
        return r

    # ---- facts and shutdown ----------------------------------------------------------------------------------------
    def stats(self):
        with self.cv:
            ready = sum(1 for s in self.slots if s.state in ("ready", "busy"))
            busy = sum(1 for s in self.slots if s.state == "busy")
            out = {"workers": self.n, "ready": ready, "busy": busy}
            out.update(self.counters)
            out["pids"] = [s.pid for s in self.slots if s.state in ("ready", "busy") and s.pid]
            out["rss_mb"] = [s.rss_mb for s in self.slots if s.state in ("ready", "busy")]
        return out

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()
        for s in self.slots:
            s.kill()
