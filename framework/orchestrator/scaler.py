"""The scaler: extra copies (replicas) of the catalogue and geometry services
when work waits, and fewer when they are idle (min 1).

Three parts:
- `Rule` / `rules()`: the thresholds per service (overrides `SCALE_<SERVICE>_<FIELD>`, `SCALE=off` for no automatic
  decisions);
- `budget(...)`: PURE. How many replicas this computer can carry, from its cores, free memory and free Postgres
  connections; no I/O, no environment, so fixed cases can test it;
- `Scaler`: a thread of the orchestrator that, once a second, reads the waiting count and the age of the oldest waiting
  message of each service's queues, decides (up, down, capped), asks the supervisor to start or retire a replica, and
  writes every decision to `orch.scale_events` with a product sentence.

The supervisor owns the processes (framework/orchestrator/supervisor.py); this module only decides and records.
"""
import os
import queue as stdqueue
import threading
import time
from dataclasses import asdict, dataclass

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from framework.common import config, pg, policy


@dataclass(frozen=True)
class Rule:
    min: int
    max: int
    up_waiting: int
    up_age_s: float
    down_waiting: int
    idle_s: float
    cooldown_s: float
    down_cooldown_s: float


DEFAULTS = {"catalogue": Rule(1, 4, 6, 2.0, 0, 20, 5, 2),
            "geometry": Rule(1, 4, 2, 5.0, 0, 60, 15, 5)}
WATCH = {"catalogue": ("catalogue_cmd", "catalogue_q"), "geometry": ("geometry_cmd",)}
RESERVE_CORES, MEM_SHARE, PG_HEADROOM = 2, 0.5, 10
TIE = ("geometry", "catalogue")           # who gives a replica back first on a tie
EXTRA_CONN = {"catalogue": 5, "geometry": 0}   # the catalogue's query answerer adds 1 + its threads (measured
                                                             # at most 8 per catalogue copy)
TICK_S = 1.0
LIVE = ("starting", "running", "busy", "draining")
FIELDS = ("min", "max", "up_waiting", "up_age_s", "down_waiting", "idle_s", "cooldown_s", "down_cooldown_s")
INT_FIELDS = ("min", "max", "up_waiting", "down_waiting")
WAIT_FOR_SET_S = 60


def _log(msg):
    line = "%s scaler: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:                    # the log file may be a legacy code page: the arrow in the sentences
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)


def rules():
    """DEFAULTS with the overrides `SCALE_<SERVICE>_<FIELD>` (environment or `.env.services`)."""
    out = {}
    for svc, base in DEFAULTS.items():
        vals = asdict(base)
        for f in FIELDS:
            raw = config.setting("SCALE_%s_%s" % (svc.upper(), f.upper()))
            if raw is not None:
                vals[f] = int(float(raw)) if f in INT_FIELDS else float(raw)
        vals["min"] = max(1, int(vals["min"]))
        vals["max"] = max(vals["min"], int(vals["max"]))
        out[svc] = Rule(**vals)
    return out


def resources():
    """(workers, mb, conn) per service: CPU-heavy worker processes, memory in MB, and Postgres connections of one replica."""
    w_geo = int(config.setting("GEO_WORKERS", 4))
    workers = {"catalogue": 0, "geometry": w_geo}
    # catalogue 400: with its query answerer a copy measured 373-376 MB
    mb = {"catalogue": 400, "geometry": 400 + 400 * w_geo}
    # 1 queue connection + the pool (POOL + 1) + 1 one-at-a-time lock connection (+ the query answerer)
    conn = {s: 1 + (policy.POOL.get(s, 1) + 1) + 1 + EXTRA_CONN[s] for s in workers}
    return workers, mb, conn


def budget(cores, avail_mb, max_conn, reserved, in_use, rules, workers, mb, conn):
    """How many replicas of each scalable service this computer can carry. Pure.

    1. Per-service cap from the cores: a service with no worker processes `cores // 4`, others `(cores - 2) // workers`;
       never below 1, never above its rule's max.
    2. Shared budgets in this order (cores, memory, connections), each by taking one replica from the service with the
       most extra replicas (ties: geometry, catalogue) among those that use the budget and are above min.
    Returns {"max": {svc: n}, "why": {svc: text}, "inputs": {...}}."""
    m, why = {}, {}
    for s, r in rules.items():
        cap = cores // 4 if workers[s] == 0 else (cores - RESERVE_CORES) // workers[s]
        cap = max(1, cap)
        m[s] = max(r.min, min(r.max, cap))
        why[s] = ["set %d" % r.max] if m[s] == r.max else ["cores %d allow %d" % (cores, cap)]

    def shrink(total, limit, uses, label):
        while total() > limit:
            cands = [s for s in m if m[s] > rules[s].min and uses(s)]
            if not cands:
                return
            s = max(cands, key=lambda x: (m[x] - 1, -TIE.index(x)))
            m[s] -= 1
            why[s].append(label)

    shrink(lambda: sum(m[s] * workers[s] for s in m), cores - RESERVE_CORES, lambda s: workers[s] > 0, "cores budget")
    shrink(lambda: sum((m[s] - 1) * mb[s] for s in m), avail_mb * MEM_SHARE, lambda s: mb[s] > 0, "memory budget")
    pg_free = max_conn - reserved - in_use
    shrink(lambda: sum((m[s] - 1) * conn[s] for s in m), pg_free - PG_HEADROOM, lambda s: conn[s] > 0,
           "connections budget")
    inputs = {"cores": cores, "free_mb": avail_mb, "max_connections": max_conn, "reserved": reserved, "in_use": in_use,
              "pg_free": pg_free, "workers": dict(workers), "mb": dict(mb), "conn": dict(conn)}
    return {"max": dict(m), "why": {s: "; ".join(why[s]) for s in m}, "inputs": inputs}


def live_inputs(con):
    """(cores, free memory MB, max_connections, reserved, in use) of this computer and the database right now."""
    import psutil
    cores = os.cpu_count() or 1
    avail = int(psutil.virtual_memory().available // 2 ** 20)
    max_conn = int(con.execute("SHOW max_connections").fetchone()[0])
    reserved = int(con.execute("SHOW superuser_reserved_connections").fetchone()[0])
    in_use = int(con.execute("SELECT count(*) FROM pg_stat_activity").fetchone()[0])
    return cores, avail, max_conn, reserved, in_use


def visible(con, queues):
    """(waiting, oldest_s): the messages that can be read now (`vt <= now`) on `queues`, and the age of the oldest."""
    waiting, oldest = 0, 0.0
    for q in queues:
        n, age = con.execute(
            sql.SQL("SELECT count(*), coalesce(extract(epoch FROM clock_timestamp() - min(enqueued_at)), 0) "
                    "FROM pgmq.{} WHERE vt <= clock_timestamp()").format(sql.Identifier("q_" + q))).fetchone()
        waiting += int(n)
        oldest = max(oldest, float(age))
    return waiting, oldest


class Scaler(threading.Thread):
    def __init__(self, sup):
        super().__init__(name="scaler", daemon=True)
        self.sup = sup
        self.rules = rules()
        self.workers, self.mb, self.conn = resources()
        self.state = "waiting"                      # waiting | running | off
        self.budget = None
        self.pinned = {}                            # service -> max set by hand
        self.view = {}                              # service -> ScaleInfo
        self.con = None
        self._events = stdqueue.Queue()
        self._last_up = {s: 0.0 for s in config.SCALABLE}
        self._last_down = {s: 0.0 for s in config.SCALABLE}
        self._capped = {s: False for s in config.SCALABLE}
        self._want_budget = threading.Event()
        self._budgeted = False
        self._born = time.monotonic()
        self._lock = threading.Lock()

    # ---- the numbers ---------------------------------------------------------------------------------------------------
    def _off(self):
        # in containers (BC_SUPERVISE=containers) each service runs as one copy: the scaler decides nothing
        return bool(getattr(self.sup, "containers", False)) or str(config.setting("SCALE", "")).lower() == "off"

    def _live_count(self, name):
        return sum(1 for r in self.sup.replica_states(name) if r["state"] in LIVE)

    def _rebudget(self):
        cores, avail, max_conn, reserved, in_use = live_inputs(self.con)
        # the connections of the extra replicas that already run are in `in_use`: take them out, the budget counts them
        mine = sum(max(0, self._live_count(s) - 1) * self.conn[s] for s in config.SCALABLE)
        b = budget(cores, avail, max_conn, reserved, max(0, in_use - mine), self.rules, self.workers, self.mb, self.conn)
        with self._lock:
            self.budget = b
        i = b["inputs"]
        _log("budget: %d cores, %d MB free, %d database connections free (%d of %d in use): %s"
             % (i["cores"], i["free_mb"], i["pg_free"], i["in_use"], i["max_connections"],
                ", ".join("%s up to %d (%s)" % (s, b["max"][s], b["why"][s]) for s in config.SCALABLE)))
        self._budgeted = True

    def effective_max(self, s):
        top = self.budget["max"][s] if self.budget else 1
        pin = self.pinned.get(s)
        return min(top, pin) if pin is not None else top

    # ---- events ----------------------------------------------------------------------------------------------------------
    def note(self, service, action, before, after, replica=None, pid=None, reason="", meta=None):
        """Record one decision (thread-safe: the supervisor calls it for `lost`); the scaler thread writes it."""
        self._events.put((service, action, int(before), int(after), replica, pid, reason, meta or {}))

    def _write_events(self):
        while True:
            try:
                service, action, before, after, replica, pid, reason, meta = self._events.get_nowait()
            except stdqueue.Empty:
                return
            _log("scale %s %s %d->%d: %s" % (service, action, before, after, reason))
            try:
                self._con().execute(
                    "INSERT INTO orch.scale_events (base_port, service, action, copies_before, copies_after, replica, pid, "
                    "reason, meta) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (self.sup.base, service, action, before, after, replica, pid, reason, Jsonb(meta)))
            except psycopg.OperationalError:
                self._close()
                self._events.put((service, action, before, after, replica, pid, reason, meta))
                return
            except Exception as exc:
                _log("could not record a decision: %s: %s" % (type(exc).__name__, exc))

    # ---- connection ----------------------------------------------------------------------------------------------------
    def _con(self):
        if self.con is None or self.con.closed:
            self.con = pg.connect(write=True, app="buildcore orchestrator scaler pid %d" % os.getpid())
        return self.con

    def _close(self):
        try:
            if self.con is not None:
                self.con.close()
        except Exception:
            pass
        self.con = None

    # ---- the loop --------------------------------------------------------------------------------------------------------
    def run(self):
        while True:
            try:
                self._con()
                self._step()
            except psycopg.OperationalError as exc:
                _log("lost the database connection (%s); reconnecting" % type(exc).__name__)
                self._close()
                time.sleep(1)
            except Exception as exc:                  # the scaler never dies of one bad tick
                _log("error: %s: %s" % (type(exc).__name__, exc))
                time.sleep(1)
            time.sleep(TICK_S)

    def _step(self):
        if not self._budgeted:
            if self.sup.all_running() or time.monotonic() - self._born > WAIT_FOR_SET_S:
                self._rebudget()
            else:
                return
        if self._want_budget.is_set():
            self._want_budget.clear()
            self._rebudget()
        self.state = "off" if self._off() else "running"
        self._tick()
        self._write_events()

    def _tick(self):
        now = time.monotonic()
        auto = not self._off()
        for s in config.SCALABLE:
            rule = self.rules[s]
            waiting, oldest = visible(self._con(), WATCH[s])
            reps = self.sup.replica_states(s)
            live = [r for r in reps if r["state"] in LIVE]
            n_live = len(live)
            emax = self.effective_max(s)
            label = config.LABELS[s]

            # 3. too many (a max lowered by hand): the highest extras stop now, each after its current job
            if n_live > emax:
                for r in sorted((r for r in live if r["replica"] > 1), key=lambda r: -r["replica"])[:n_live - emax]:
                    if self.sup.retire_replica(s, r["replica"], "at most %d copies, set by hand" % emax):
                        self.note(s, "down", n_live, n_live - 1, r["replica"], r["pid"],
                                  "%s: copy %d stops: at most %d copies, set by hand (%d → %d copies)."
                                  % (label, r["replica"], emax, n_live, n_live - 1),
                                  {"waiting": waiting, "by_hand": True})
                        n_live -= 1
                        self._last_down[s] = now

            starting = any(r["state"] == "starting" for r in reps)
            trigger = waiting >= rule.up_waiting or (waiting >= 1 and oldest >= rule.up_age_s)
            if auto:
                # 4. up
                if trigger and n_live < emax and not starting and now - self._last_up[s] >= rule.cooldown_s:
                    n = self.sup.add_replica(s, "scale up")
                    if n is not None:
                        self.note(s, "up", n_live, n_live + 1, n, None,
                                  "%s: %d → %d copies: %d waiting, the oldest for %.1f s."
                                  % (label, n_live, n_live + 1, waiting, oldest),
                                  {"waiting": waiting, "oldest_s": round(oldest, 2), "max": emax,
                                   "cooldown_s": rule.cooldown_s})
                        self._last_up[s] = now
                        n_live += 1
                elif trigger and n_live >= emax:
                    if not self._capped[s]:
                        self._capped[s] = True
                        why = self.budget["why"][s] if self.budget else ""
                        if self.pinned.get(s) is not None and self.pinned[s] <= self.budget["max"][s]:
                            why = "set by hand"
                        self.note(s, "capped", n_live, n_live, None, None,
                                  "%s: %d copies is the most allowed right now (%s)." % (label, emax, why),
                                  {"waiting": waiting, "oldest_s": round(oldest, 2), "max": emax})
                if not trigger:
                    self._capped[s] = False
                # 5. down
                if (waiting <= rule.down_waiting and now - self._last_up[s] >= rule.cooldown_s
                        and now - self._last_down[s] >= rule.down_cooldown_s and n_live > rule.min):
                    idle = [r for r in live if r["replica"] > 1 and r["state"] == "running" and not r["running_jobs"]
                            and (r["idle_s"] or 0) + (r["heartbeat_age_s"] or 0) >= rule.idle_s]
                    if idle:
                        r = max(idle, key=lambda x: x["replica"])
                        idle_s = (r["idle_s"] or 0) + (r["heartbeat_age_s"] or 0)
                        if self.sup.retire_replica(s, r["replica"], "idle for %.0f s" % idle_s):
                            self.note(s, "down", n_live, n_live - 1, r["replica"], r["pid"],
                                      "%s: copy %d stops after %.0f s without work (%d → %d copies)."
                                      % (label, r["replica"], idle_s, n_live, n_live - 1),
                                      {"idle_s": round(idle_s, 1), "waiting": waiting})
                            n_live -= 1
                            self._last_down[s] = now

            # 6. what the Services page shows
            b = self.budget["inputs"] if self.budget else {}
            self.view[s] = {"scalable": True, "replicas": n_live, "min": rule.min, "max": emax,
                            "max_why": self.budget["why"][s] if self.budget else "",
                            "pinned_max": self.pinned.get(s), "waiting": waiting, "oldest_s": round(oldest, 1),
                            "cores": b.get("cores"), "free_mb": b.get("free_mb"), "pg_free": b.get("pg_free")}

    # ---- what /api/scale answers -----------------------------------------------------------------------------------------
    def health(self):
        out = {"state": self.state, "budget": self.budget, "rules": {s: asdict(r) for s, r in self.rules.items()},
               "services": {s: dict(v) for s, v in self.view.items()}}
        if getattr(self.sup, "containers", False):
            out["note"] = "Each service runs as one copy in containers."
        return out

    def request(self, body):
        """POST /api/scale -> (http code, body). Bodies: {service, add: 1} {service, max: n} {service, reset: true}
        {rebudget: true}."""
        if getattr(self.sup, "containers", False):
            return 409, {"error": "Each service runs as one copy in containers."}
        if body.get("rebudget"):
            self._want_budget.set()
            return 200, {"ok": True, "note": "the budget is computed again within a second"}
        s = body.get("service")
        if s not in config.LABELS:
            return 400, {"error": "Choose one of: %s." % ", ".join(config.ORDER)}
        label = config.LABELS[s]
        if s not in config.SCALABLE:
            return 400, {"error": "The %s runs as one copy." % label}
        n_live = self._live_count(s)
        if body.get("add"):
            emax = self.effective_max(s)
            if n_live >= emax:
                return 409, {"error": "%s already has the most copies allowed (%d)." % (label, emax)}
            n = self.sup.add_replica(s, "by hand")
            if n is None:
                return 409, {"error": "%s cannot start another copy right now." % label}
            self.note(s, "requested", n_live, n_live + 1, n, None,
                      "%s: a copy was added by hand (%d → %d copies)." % (label, n_live, n_live + 1), {"by_hand": True})
            self._last_up[s] = time.monotonic()
            return 200, {"ok": True, "replica": n, "copies": n_live + 1}
        if "max" in body:
            top = self.budget["max"][s] if self.budget else 1
            try:
                n = int(body["max"])
            except (TypeError, ValueError):
                n = 0
            if not 1 <= n <= top:
                return 400, {"error": "Choose between 1 and %d copies." % top}
            self.pinned[s] = n
            self.note(s, "requested", n_live, n_live, None, None, "%s: at most %d copies, set by hand." % (label, n),
                      {"by_hand": True, "max": n})
            return 200, {"ok": True, "max": n}
        if body.get("reset"):
            self.pinned.pop(s, None)
            return 200, {"ok": True, "max": self.effective_max(s)}
        return 400, {"error": "Say what to do: add, max or reset."}
