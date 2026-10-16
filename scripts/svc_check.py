"""Checks for the job queues and the database. Run with the product venv's python (~/venvs/buildcore).

  pg-snapshot [--compare] [--db NAME]...
                           row counts of every table in this copy's database (read only); --compare reads the saved snapshot again
                           and prints the differences (0 expected). --db NAME (repeatable) snapshots those databases instead
                           of this copy's; the snapshot also keeps the Id and StartedAt of the Postgres container, and
                           --compare says whether they are unchanged; --file F keeps the snapshot elsewhere than out/pg_snapshot.json
  pgmq-install             install pgmq (vendored SQL, no extension) into the database in effect;
                           a second run does nothing; refuses the databases named in BC_PROTECTED_DBS
  pgmq-status              pgmq present in this copy's database (protected databases are never opened)
  queue-ping               one round trip (sent 1, read 1, archived 1) and a burst of 100 with p50/p95

more subcommands: scripts/svc_checks/*.py
"""
import argparse
import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from framework.catalog.pg import db  # noqa: E402
from psycopg import sql  # noqa: E402

PGMQ_SQL = os.path.join(ROOT, "vendor", "pgmq", "pgmq-v1.13.0.sql")
PGMQ_VERSION = "1.13.0"
SNAP = os.path.join(ROOT, "out", "pg_snapshot.json")
from framework.common.config import PROTECTED_DBS as PROTECTED  # noqa: E402  (one list for every guard)
TEST_QUEUE = "svc_check_ping"


def _counts(dbname):
    """{schema.table: rows} for every user table. Read only (SELECT count(*))."""
    con = db.connect(dbname=dbname, autocommit=True)
    try:
        with con.cursor() as cur:
            cur.execute("SELECT table_schema, table_name FROM information_schema.tables "
                        "WHERE table_type='BASE TABLE' AND table_schema NOT IN ('pg_catalog','information_schema') "
                        "ORDER BY 1,2")
            out = {}
            for s, t in cur.fetchall():
                cur.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(s), sql.Identifier(t)))
                out[f"{s}.{t}"] = cur.fetchone()[0]
            return out
    finally:
        con.close()


def _pg_container():
    """Id and StartedAt of the Postgres container (a run must not restart the shared server). {} when Docker does not answer."""
    import subprocess
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{.Id}} {{.State.StartedAt}}", os.environ.get("PG_CONTAINER") or "buildcore-pg"], capture_output=True, text=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return {}
    parts = r.stdout.split()
    return {"id": parts[0][:12], "started_at": parts[1]} if r.returncode == 0 and len(parts) == 2 else {}


def cmd_snapshot(a):
    names = tuple(a.db) if a.db else (db.dbname(),)
    snap = os.path.abspath(a.file) if a.file else SNAP
    now = {d: _counts(d) for d in names}
    pgc = _pg_container()
    if not a.compare:
        os.makedirs(os.path.dirname(snap), exist_ok=True)
        with open(snap, "w") as fh:
            json.dump(dict(now, _container=pgc), fh, indent=1, sort_keys=True)
        for d in names:
            print(f"{d}: {len(now[d])} tables, {sum(now[d].values())} rows")
        print(f"Postgres container: Id {pgc.get('id', '?')}, StartedAt {pgc.get('started_at', '?')}")
        print("saved", os.path.relpath(snap, ROOT))
        return 0
    with open(snap) as fh:
        before = json.load(fh)
    diffs = 0
    for d in names:
        if d not in before:
            print(f"{d}: not in the saved snapshot")
            diffs += 1
            continue
        keys = set(before[d]) | set(now[d])
        dd = [(k, before[d].get(k), now[d].get(k)) for k in sorted(keys) if before[d].get(k) != now[d].get(k)]
        diffs += len(dd)
        print(f"{d}: {len(now[d])} tables, {sum(now[d].values())} rows, {len(dd)} tables with a different row count")
        for k, b, n in dd:
            print(f"  {k}: {b} -> {n}")
    old = before.get("_container") or {}
    if "_container" not in before:              # an older snapshot without container facts: not a difference
        print(f"Postgres container: Id {pgc.get('id', '?')}, StartedAt {pgc.get('started_at', '?')} -- the saved snapshot predates this check (not compared)")
    elif old or pgc:
        same = old == pgc
        print(f"Postgres container: Id {pgc.get('id', '?')}, StartedAt {pgc.get('started_at', '?')} -- {'unchanged' if same else 'CHANGED (was %s, %s)' % (old.get('id', '?'), old.get('started_at', '?'))}")
        diffs += 0 if same else 1
    print(f"total differences: {diffs}")
    return 1 if diffs else 0


def _has_pgmq(dbname):
    con = db.connect(dbname=dbname, autocommit=True)
    try:
        with con.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_namespace WHERE nspname='pgmq'")
            return cur.fetchone() is not None
    finally:
        con.close()


def cmd_install(a):
    name = db.dbname()
    if name in PROTECTED:
        print(f"refusing: {name} is a protected database (BC_PROTECTED_DBS)")
        return 2
    if _has_pgmq(None):
        print(f"pgmq already present in {name}: nothing to do")
        return 0
    with open(PGMQ_SQL, encoding="utf-8") as fh:
        script = fh.read()
    con = db.connect(autocommit=False)
    try:
        with con.cursor() as cur:
            cur.execute(script)
            cur.execute("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname='pgmq'")
            n = cur.fetchone()[0]
        con.commit()
    finally:
        con.close()
    print(f"installed pgmq {PGMQ_VERSION} (SQL only) in {name}: {n} functions")
    return 0


def cmd_status(a):
    ok = True
    for d, want in ((db.dbname(), True),):
        has = _has_pgmq(d)
        good = has == want
        ok &= good
        print(f"{d}: pgmq {'present' if has else 'absent'} ({'ok' if good else 'UNEXPECTED'})")
    return 0 if ok else 1


def cmd_ping(a):
    from framework.common import pg, pgmq
    if db.dbname() in PROTECTED:
        print("refusing: a protected database (BC_PROTECTED_DBS)")
        return 2
    con = pg.connect(write=True)
    try:
        pgmq.create(con, TEST_QUEUE, notify=False)
        t0 = time.perf_counter()
        mid = pgmq.send(con, TEST_QUEUE, {"ping": 1})
        got = pgmq.read(con, TEST_QUEUE, 30, 1)
        arch = pgmq.archive(con, TEST_QUEUE, got[0].pgmq_id) if got else False
        ms = (time.perf_counter() - t0) * 1000
        print(f"sent 1, read {len(got)}, archived {1 if arch else 0}  (round trip {ms:.1f} ms, msg {mid})")
        lat = []
        for i in range(a.burst):
            t = time.perf_counter()
            pgmq.send(con, TEST_QUEUE, {"n": i})
            m = pgmq.read(con, TEST_QUEUE, 30, 1)
            pgmq.archive(con, TEST_QUEUE, m[0].pgmq_id)
            lat.append((time.perf_counter() - t) * 1000)
        lat.sort()
        p50 = statistics.median(lat)
        p95 = lat[max(int(len(lat) * 0.95) - 1, 0)]
        print(f"burst {a.burst}: p50 {p50:.1f} ms, p95 {p95:.1f} ms, max {lat[-1]:.1f} ms")
        rest = pgmq.read(con, TEST_QUEUE, 1, 1)
        print(f"left in queue: {len(rest)}")
        pgmq.drop(con, TEST_QUEUE)
    finally:
        con.close()
    ok = len(got) == 1 and arch and not rest
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def _plugins():
    """The subcommand modules in scripts/svc_checks/*.py (names not starting with _), sorted."""
    import glob
    import importlib.util
    for path in sorted(glob.glob(os.path.join(ROOT, "scripts", "svc_checks", "*.py"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location("svc_checks." + name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("pg-snapshot")
    s.add_argument("--compare", action="store_true")
    s.add_argument("--db", action="append", help="a database to snapshot (repeatable; default this copy's database)")
    s.add_argument("--file", help="where the snapshot is kept (default out/pg_snapshot.json)")
    s.set_defaults(f=cmd_snapshot)
    sub.add_parser("pgmq-install").set_defaults(f=cmd_install)
    sub.add_parser("pgmq-status").set_defaults(f=cmd_status)
    s = sub.add_parser("queue-ping")
    s.add_argument("--burst", type=int, default=100)
    s.set_defaults(f=cmd_ping)
    for m in _plugins():
        if hasattr(m, "register"):
            m.register(sub)
        else:                                            # a module without register is skipped, and said so (stand-alone scripts are named _x.py)
            print("svc_check: scripts/svc_checks/%s.py has no register(sub); skipped" % m.__name__.split(".")[-1], file=sys.stderr)
    a = p.parse_args()
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
