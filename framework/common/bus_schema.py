"""Create the bus in the database in effect: schemas orch and bus, their tables, every queue
of queues.ALL, and instant wake-up (NOTIFY throttle 0) on the consumed ones (queues.CONSUMED).
Idempotent; the orchestrator runs it at start.

Refuses the databases listed in BC_PROTECTED_DBS and a database without pgmq.
"""
import os

from framework.common import pg, pgmq, queues

SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bus_schema.sql")
from framework.common.config import PROTECTED_DBS as PROTECTED  # noqa: E402  (one list for every guard)
TABLES = ("orch.jobs", "orch.job_events", "bus.processed", "bus.echo_effects", "orch.scale_events", "bus.single_runs")


def _zero_throttle(con):
    rows = con.execute("SELECT queue_name FROM pgmq.notify_insert_throttle WHERE throttle_interval_ms = 0").fetchall()
    return {r[0] for r in rows} & set(queues.CONSUMED)


def ensure(con=None):
    own = con is None
    if own:
        con = pg.connect(write=True)
    try:
        name = pg.dbname()
        if name in PROTECTED:
            raise RuntimeError("refusing database %s: it is listed in BC_PROTECTED_DBS" % name)
        if not pgmq.installed(con):
            raise RuntimeError("pgmq is not installed in %s; run `python scripts/svc_check.py pgmq-install`" % name)
        with open(SQL_FILE, encoding="utf-8") as fh:
            script = fh.read()
        with con.transaction():
            # first start: the registry and the bus both create schema orch; one lock, one creator at a time
            con.execute("SELECT pg_advisory_xact_lock(hashtext('orch.ddl'))")
            con.execute(script)
        created = 0
        before = len(_zero_throttle(con))
        for q in queues.ALL:
            if pgmq.create(con, q, notify=q in queues.CONSUMED):
                created += 1
        present = set(pgmq.list_queues(con)) & set(queues.ALL)
        return {"db": name, "queues_created": created, "queues_total": len(present),
                "notify_set": len(_zero_throttle(con)) - before, "tables": list(TABLES)}
    finally:
        if own:
            con.close()
