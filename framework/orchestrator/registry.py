"""`orch.services`: the orchestrator's registry of the seven services and their heartbeats.

The orchestrator is its only writer. Times are stored on the database's clock (now() minus the age the orchestrator
measured on its own monotonic clock), so a heartbeat age read back as now() - heartbeat_at is right even when the
database runs in a container whose clock differs from this PC's. Rows are keyed by port, so two sets of services
(two work copies, or a check set) never overwrite each other; the gateway lists the rows of its own base port.
Tenant and account columns are provisioned from birth (one default; nothing filters on them).
"""
import json
import threading
import time

from framework.common import pg as cpg

DDL = """
CREATE SCHEMA IF NOT EXISTS orch;
CREATE TABLE IF NOT EXISTS orch.services (
    port          integer PRIMARY KEY,
    name          text        NOT NULL,
    base_port     integer     NOT NULL,
    pid           integer,
    launcher_pid  integer,
    state         text        NOT NULL,
    detail        text        NOT NULL DEFAULT '',
    running_jobs  integer     NOT NULL DEFAULT 0,
    restarts      integer     NOT NULL DEFAULT 0,
    last_exit     text        NOT NULL DEFAULT '',
    started_at    timestamptz,
    heartbeat_at  timestamptz,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    tenant_id     text        NOT NULL DEFAULT 'default',
    account_id    text        NOT NULL DEFAULT 'default'
);
ALTER TABLE orch.services ADD COLUMN IF NOT EXISTS replicas jsonb NOT NULL DEFAULT '[]'::jsonb;   -- the service's replicas
ALTER TABLE orch.services ADD COLUMN IF NOT EXISTS scale jsonb NOT NULL DEFAULT '{}'::jsonb;
-- Where the service runs (the container's name; NULL = this PC) and the image it runs from (NULL = none). Nothing
-- reads them in a host set.
ALTER TABLE orch.services ADD COLUMN IF NOT EXISTS place text;
ALTER TABLE orch.services ADD COLUMN IF NOT EXISTS image text;
"""

UPSERT = """
INSERT INTO orch.services (port, name, base_port, pid, launcher_pid, state, detail, running_jobs, restarts, last_exit,
                           started_at, heartbeat_at, updated_at, replicas, scale, place, image)
VALUES (%(port)s, %(name)s, %(base_port)s, %(pid)s, %(launcher_pid)s, %(state)s, %(detail)s, %(running_jobs)s,
        %(restarts)s, %(last_exit)s,
        CASE WHEN %(started_age)s::float8 IS NULL THEN NULL ELSE now() - make_interval(secs => %(started_age)s::float8) END,
        CASE WHEN %(heartbeat_age)s::float8 IS NULL THEN NULL ELSE now() - make_interval(secs => %(heartbeat_age)s::float8) END,
        now(), %(replicas)s::jsonb, %(scale)s::jsonb, %(place)s, %(image)s)
ON CONFLICT (port) DO UPDATE SET
    name = EXCLUDED.name, base_port = EXCLUDED.base_port, pid = EXCLUDED.pid, launcher_pid = EXCLUDED.launcher_pid,
    state = EXCLUDED.state, detail = EXCLUDED.detail, running_jobs = EXCLUDED.running_jobs,
    restarts = EXCLUDED.restarts, last_exit = EXCLUDED.last_exit, started_at = EXCLUDED.started_at,
    heartbeat_at = EXCLUDED.heartbeat_at, updated_at = now(), replicas = EXCLUDED.replicas, scale = EXCLUDED.scale,
    place = EXCLUDED.place, image = EXCLUDED.image
"""


class Registry:
    def __init__(self, base_port):
        self.base = base_port
        self.con = None
        self.lock = threading.Lock()
        self.last_error_log = 0.0
        self.ok = False

    def _conn(self):
        if self.con is None or self.con.closed:
            self.con = cpg.connect(write=True)
            # first start: the registry and the bus both create schema orch; one lock, one creator at a time
            with self.con.transaction():
                self.con.execute("SELECT pg_advisory_xact_lock(hashtext('orch.ddl'))")
                self.con.execute(DDL)
        return self.con

    def _note(self, exc):
        self.ok = False
        if time.monotonic() - self.last_error_log > 60:
            self.last_error_log = time.monotonic()
            print("registry: writing orch.services failed (%s: %s); the services keep running"
                  % (type(exc).__name__, str(exc)[:200]), flush=True)
        try:
            if self.con is not None:
                self.con.close()
        except Exception:
            pass
        self.con = None

    def reset(self, ports):
        """Start of a run: forget this base port's rows (old pids, old restart counts)."""
        with self.lock:
            try:
                self._conn().execute("DELETE FROM orch.services WHERE base_port = %s OR port = ANY(%s)",
                                     (self.base, list(ports)))
                self.ok = True
            except Exception as exc:
                self._note(exc)

    def save(self, rows):
        """Upsert row dicts (keys of UPSERT; ages in seconds or None)."""
        if not rows:
            return True
        rows = [dict(r, replicas=json.dumps(r.get("replicas") or []), scale=json.dumps(r.get("scale") or {}),
                     place=r.get("place"), image=r.get("image"))
                for r in rows]
        with self.lock:
            try:
                con = self._conn()
                with con.cursor() as cur:
                    cur.executemany(UPSERT, rows)
                self.ok = True
                return True
            except Exception as exc:
                self._note(exc)
                return False
