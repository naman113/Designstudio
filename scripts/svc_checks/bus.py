"""svc_check subcommands for the job bus. Loaded by scripts/svc_check.py (discovery).

  bus-init                         create every queue, the tables and instant wake-up (idempotent)
  bus-status                       the bus as it stands: queues, throttle, tenant columns, dispatcher lock, connections
  sql "<one SELECT>"               read-only look at the database (anything but one SELECT is refused)
  consumer-ping [--service S]      a direct command to <S>_cmd (no orchestrator): started, step, reply, ms
  send-echo / events / echo-load   the test job through the whole path

Every subcommand ends with PASS or FAIL and refuses the databases named in BC_PROTECTED_DBS.
"""
import os
import statistics
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.common import bus_schema, config, envelope, pg, pgmq, queues  # noqa: E402

from framework.common.config import PROTECTED_DBS as PROTECTED  # noqa: E402  (one list for every guard)


def _refuse():
    name = pg.dbname()
    if name in PROTECTED:
        print("refusing: %s is a protected database (BC_PROTECTED_DBS)" % name)
        return True
    return False


def _verdict(ok):
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


# ---- bus-init ---------------------------------------------------------------------------------------------------
def cmd_bus_init(a):
    if _refuse():
        return 2
    r = bus_schema.ensure()
    print("bus-init %s: queues created %d, total %d, notify set %d, tables %s"
          % (r["db"], r["queues_created"], r["queues_total"], r["notify_set"], " ".join(r["tables"])))
    return _verdict(r["queues_total"] == len(queues.ALL))


# ---- bus-status -------------------------------------------------------------------------------------------------
def _mark(ok):
    return "PASS" if ok else "FAIL"


def cmd_bus_status(a):
    if _refuse():
        return 2
    con = pg.connect()
    ok = True
    try:
        present = set(pgmq.list_queues(con)) & set(queues.ALL)
        good = len(present) == len(queues.ALL)
        ok &= good
        print("%s queues present: %d of %d" % (_mark(good), len(present), len(queues.ALL)))
        rows = con.execute("SELECT queue_name FROM pgmq.notify_insert_throttle WHERE throttle_interval_ms = 0").fetchall()
        zero = {r[0] for r in rows} & set(queues.CONSUMED)
        good = len(zero) == len(queues.CONSUMED)
        ok &= good
        print("%s consumed queues with throttle 0: %d of %d" % (_mark(good), len(zero), len(queues.CONSUMED)))
        from framework.common.bus_schema import TABLES
        have = 0
        for t in TABLES:
            s, n = t.split(".")
            cols = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                              "WHERE table_schema = %s AND table_name = %s", (s, n)).fetchall()}
            have += 1 if {"tenant_id", "account_id"} <= cols else 0
        good = have == len(TABLES)
        ok &= good
        print("%s tables with tenant_id and account_id: %d of %d" % (_mark(good), have, len(TABLES)))
        row = con.execute("SELECT l.pid, coalesce(a.application_name, '') FROM pg_locks l "
                          "LEFT JOIN pg_stat_activity a ON a.pid = l.pid "
                          "WHERE l.locktype = 'advisory' AND l.granted "
                          "AND l.objid = (hashtext('orch.dispatcher')::bigint & 4294967295) "
                          "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())").fetchone()
        print("PASS dispatcher lock holder: %s" % ("database backend %s (%s)" % row if row else "none"))
        n = con.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()").fetchone()[0]
        print("PASS connections in this database: %d" % n)
    finally:
        con.close()
    return _verdict(ok)


# ---- sql --------------------------------------------------------------------------------------------------------
def cmd_sql(a):
    if _refuse():
        return 2
    text = a.statement.strip()
    low = text.lower()
    if ";" in text or not (low.startswith("select") or low.startswith("with")):
        print("refused: one statement starting with select or with, and no ';'")
        return _verdict(False)
    con = pg.connect()
    try:
        con.execute("BEGIN READ ONLY")
        try:
            cur = con.execute(text)
            for row in cur.fetchall():
                print("\t".join("" if v is None else str(v) for v in row))
        finally:
            con.execute("ROLLBACK")
    finally:
        con.close()
    return _verdict(True)


# ---- consumer-ping ----------------------------------------------------------------------------------------------
def _count(con, sql_text, *params):
    return con.execute(sql_text, params).fetchone()[0]


def cmd_consumer_ping(a):
    if _refuse():
        return 2
    svc = a.service
    q = queues.cmd(svc)
    qa = "a_%s" % q
    con = pg.connect(write=True)
    try:
        job = str(uuid.uuid4())
        proc0 = _count(con, "SELECT count(*) FROM bus.processed WHERE consumer = %s", svc)
        arch0 = _count(con, "SELECT count(*) FROM pgmq." + qa)
        env = envelope.new("command", "%s.echo" % svc, {"text": "ping"}, sender="cli", job_id=job,
                           reply_to=queues.CHECK_REPLY, step="echo")
        envelope.validate(env)
        t0 = time.perf_counter()
        pgmq.send(con, q, env)
        got = []
        end = t0 + 5.0
        while time.perf_counter() < end:
            for m in pgmq.read(con, queues.CHECK_REPLY, 10, 10, conditional={"job_id": job}):
                pgmq.archive(con, queues.CHECK_REPLY, m.pgmq_id)
                got.append(((time.perf_counter() - t0) * 1000, m.message))
            if any(g[1]["kind"] == "reply" for g in got):
                break
            time.sleep(0.002)
        time.sleep(0.2)          # let the final transaction's counters settle before counting
        proc1 = _count(con, "SELECT count(*) FROM bus.processed WHERE consumer = %s", svc)
        arch1 = _count(con, "SELECT count(*) FROM pgmq." + qa)
    finally:
        con.close()
    order = []
    for ms, m in got:
        p = m["payload"]
        what = p.get("phase") or p.get("status")
        if m["kind"] == "progress" and p.get("phase") == "step":
            what = "step %s" % p.get("step")
        if m["kind"] == "reply" and p.get("status") == "error":
            what = "error %s" % p["error"]["code"]
        order.append(what)
        print("%-9s %-24s +%.1f ms" % (m["kind"], what, ms))
    print("bus.processed +%d, %s +%d" % (proc1 - proc0, "pgmq." + qa, arch1 - arch0))
    if svc == "catalogue":
        ok = order == ["started", "step echo", "ok"] and proc1 - proc0 == 1 and arch1 - arch0 == 1
    else:
        ok = order == ["started", "error unknown_type"] and proc1 - proc0 == 1 and arch1 - arch0 == 1
    return _verdict(ok)


# ---- events / send-echo -------------------------------------------------------------------------------------------
def _print_events(con, job_id):
    """The job line and its events; returns True when seq runs 1..n and every row has tenant and account 'default'."""
    from framework.common import jobs
    job = jobs.get(con, job_id)
    if job is None:
        print("job %s not found" % job_id)
        return False
    full = con.execute("SELECT error_code, tenant_id, account_id, user_text, attempt FROM orch.jobs WHERE job_id = %s",
                       (job_id,)).fetchone()
    print("job %s %s %s attempt %s error %s tenant %s account %s"
          % (job_id, job["type"], job["state"], full[4], full[0] or "-", full[1], full[2]))
    if full[3]:
        print("user_text: %s" % full[3])
    evs = jobs.events(con, job_id)
    t0 = evs[0]["created_at"] if evs else None
    ok = bool(evs)
    for n, ev in enumerate(evs, 1):
        ms = (ev["created_at"] - t0).total_seconds() * 1000
        ok &= ev["seq"] == n and ev["tenant_id"] == "default" and ev["account_id"] == "default"
        print("%d %-16s %-10s %-12s %-6s %-2s +%.1f ms  %s %s" % (ev["seq"], ev["type"], ev["state"],
              ev["service"] or "-", ev["step"] or "-", ev["read_ct"] if ev["read_ct"] is not None else "-", ms,
              ev["tenant_id"], ev["account_id"]))
    return ok


def cmd_events(a):
    if _refuse():
        return 2
    con = pg.connect()
    try:
        return _verdict(_print_events(con, a.job_id))
    finally:
        con.close()


def _gateway_url():
    return "http://127.0.0.1:%d" % config.port_of("gateway")


def cmd_send_echo(a):
    if _refuse():
        return 2
    from framework.common import jobs
    faults = a.effect or a.fail or a.once or a.deadline_s is not None or a.fail_until_attempt is not None
    via = "direct" if faults else a.via
    con = pg.connect(write=True)
    try:
        con.execute("LISTEN job_events")
        if via == "gateway":
            import json
            import urllib.error
            import urllib.request
            req = urllib.request.Request(_gateway_url() + "/api/jobs/echo",
                                         data=json.dumps({"text": a.text, "sleep_s": a.sleep}).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    job_id = json.load(r)["job_id"]
            except urllib.error.HTTPError as exc:
                print("the gateway refused: %s %s" % (exc.code, exc.read().decode()[:200]))
                return _verdict(False)
            except OSError as exc:
                print("the gateway is not answering: %s" % type(exc).__name__)
                return _verdict(False)
        else:
            payload = {"text": a.text, "sleep_s": a.sleep}
            if a.effect:
                payload["effect"] = True
            if a.fail:
                payload["fail"] = a.fail
            if a.fail_until_attempt is not None:
                payload["fail_until_attempt"] = a.fail_until_attempt
            deadline = None
            if a.deadline_s is not None:
                from datetime import datetime, timedelta, timezone
                deadline = (datetime.now(timezone.utc) + timedelta(seconds=a.deadline_s)).isoformat(timespec="milliseconds")
            job_id = jobs.submit(con, "catalogue.echo_once" if a.once else "catalogue.echo", payload, sender="cli",
                                 deadline=deadline)
        print("job %s" % job_id)
        if a.no_wait:
            return _verdict(True)
        try:
            jobs.wait(con, job_id, 60 + a.sleep)
        except TimeoutError as exc:
            print(exc)
            return _verdict(False)
        return _verdict(_print_events(con, job_id))
    finally:
        con.close()


# ---- echo-load -----------------------------------------------------------------------------------------------------
def _median(xs):
    return statistics.median(xs) if xs else 0.0


def _pct(xs, p):
    xs = sorted(xs)
    return xs[max(min(int(len(xs) * p + 0.9999) - 1, len(xs) - 1), 0)] if xs else 0.0


def _load_worker(count, results, idx0, start_gate):
    """One thread: its own send and listen connections; sequential jobs. results[i] = (job_id, t0, t1, db_t0, state)."""
    import json
    from framework.common import jobs
    send = pg.connect(write=True)
    listen = pg.connect()
    listen.execute("LISTEN job_events")
    start_gate.wait()
    try:
        for k in range(count):
            i = idx0 + k
            job_id = str(uuid.uuid4())
            db_t0 = send.execute("SELECT clock_timestamp()").fetchone()[0]
            t0 = time.perf_counter()
            jobs.submit(send, "catalogue.echo", {"text": "load %d" % i}, sender="cli", job_id=job_id)
            state, t1, end = None, None, t0 + 30.0
            last_check = t0
            while state is None and time.perf_counter() < end:
                for n in listen.notifies(timeout=1.0, stop_after=1):
                    try:
                        body = json.loads(n.payload)
                    except ValueError:
                        continue
                    if body.get("j") == job_id and body.get("st") in jobs.FINAL:
                        t1 = time.perf_counter()
                        state = body["st"]
                if state is None and time.perf_counter() - last_check >= 1.0:
                    last_check = time.perf_counter()
                    row = jobs.get(listen, job_id)
                    if row and row["state"] in jobs.FINAL:
                        t1, state = time.perf_counter(), row["state"]
            results[i] = (job_id, t0, t1, db_t0, state)
    finally:
        send.close()
        listen.close()


def cmd_echo_load(a):
    if _refuse():
        return 2
    import threading
    n, conc = a.n, max(1, a.concurrency)
    con = pg.connect(write=True)
    a0 = _count(con, "SELECT count(*) FROM pgmq.a_catalogue_cmd")
    results = [None] * n
    gate = threading.Event()
    per, extra = divmod(n, conc)
    threads, idx = [], 0
    for c in range(conc):
        cnt = per + (1 if c < extra else 0)
        t = threading.Thread(target=_load_worker, args=(cnt, results, idx, gate))
        idx += cnt
        threads.append(t)
        t.start()
    time.sleep(0.5)
    wall0 = time.perf_counter()
    gate.set()
    for t in threads:
        t.join()
    wall = time.perf_counter() - wall0
    time.sleep(0.3)
    done = [r for r in results if r]
    ids = [r[0] for r in done]
    lost = sum(1 for r in done if r[4] is None) + (n - len(done))
    ok_jobs = [r for r in done if r[4] == "succeeded"]
    failed = sum(1 for r in done if r[4] is not None and r[4] != "succeeded")
    rt = [(r[2] - r[1]) * 1000 for r in ok_jobs]
    dup = _count(con, "SELECT count(*) FROM (SELECT job_id FROM orch.job_events WHERE job_id = ANY(%s::uuid[]) "
                      "AND type = 'job.succeeded' GROUP BY job_id HAVING count(*) > 1) x", ids)
    dup += _count(con, "SELECT count(*) FROM (SELECT job_id FROM bus.processed WHERE consumer = 'catalogue' "
                       "AND job_id = ANY(%s::uuid[]) GROUP BY job_id HAVING count(*) > 1) x", ids)
    dup += _count(con, "SELECT count(*) FROM (SELECT message->>'job_id' FROM pgmq.a_catalogue_cmd "
                       "WHERE message->>'job_id' = ANY(%s::text[]) GROUP BY 1 HAVING count(*) > 1) x", ids)
    # per-hop medians from the event times (the database's clock) and the client's submit time
    hops = {"submit->queued": [], "queued->dispatched": [], "dispatched->started": [], "started->progressed": [],
            "progressed->succeeded": []}
    ev = {}
    for jid, typ, at in con.execute("SELECT job_id::text, type, created_at FROM orch.job_events "
                                    "WHERE job_id = ANY(%s::uuid[])", (ids,)).fetchall():
        ev.setdefault(jid, {})[typ] = at
    for r in ok_jobs:
        e = ev.get(r[0], {})
        pairs = (("submit->queued", r[3], e.get("job.queued")),
                 ("queued->dispatched", e.get("job.queued"), e.get("job.dispatched")),
                 ("dispatched->started", e.get("job.dispatched"), e.get("job.started")),
                 ("started->progressed", e.get("job.started"), e.get("job.progressed")),
                 ("progressed->succeeded", e.get("job.progressed"), e.get("job.succeeded")))
        for name, x, y in pairs:
            if x is not None and y is not None:
                hops[name].append((y - x).total_seconds() * 1000)
    waiting = _count(con, "SELECT count(*) FROM pgmq.q_catalogue_cmd")
    a1 = _count(con, "SELECT count(*) FROM pgmq.a_catalogue_cmd")
    conns = _count(con, "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
    con.close()
    med = _median(rt)
    print("echo-load %d (direct, concurrency %d): succeeded %d, failed %d, lost %d, duplicated %d"
          % (n, conc, len(ok_jobs), failed, lost, dup))
    print("round trip ms: median %.1f, p95 %.1f, max %.1f   (mark: median <= 100)   total %.2f s, %.0f jobs/s"
          % (med, _pct(rt, 0.95), max(rt) if rt else 0.0, wall, n / wall if wall else 0.0))
    print("per hop median ms: " + ", ".join("%s %.1f" % (k, _median(v)) for k, v in hops.items()))
    print("catalogue_cmd: waiting %d; archive +%d   (connections in this database: %d)" % (waiting, a1 - a0, conns))
    ok = (len(ok_jobs) == n and lost == 0 and dup == 0 and failed == 0 and waiting == 0 and a1 - a0 == n
          and med <= 100)
    return _verdict(ok)


# ---- exact-check: numbers keep their bytes through a queue hop ---------------------------------------------------
def cmd_exact_check(a):
    """-0.0 and 1e-17 must come back byte-identical: as JSON text inside a command, as JSON text in a reply, and by file
    reference when the result is big. (pgmq's jsonb would turn -0.0 into 0.0; shown for contrast.)"""
    if _refuse():
        return 2
    import json
    from psycopg.types.json import Jsonb
    from framework.catalog.svc_handlers import BIG_REPEAT, EXACT_SAMPLE
    from framework.common import jobs
    want = json.dumps(EXACT_SAMPLE)
    con = pg.connect(write=True)
    ok = True
    try:
        mangled = con.execute("SELECT %s::jsonb::text", (Jsonb(EXACT_SAMPLE),)).fetchone()[0]
        print("for contrast, the same numbers through jsonb: %s" % mangled[:90])
        print("jsonb changes them: %s" % ("yes" if mangled.replace(" ", "") != want.replace(" ", "") else "NO (unexpected)"))
        cases = (("JSON text in and out", {"numbers_json": want}, want, False),
                 ("made by the service, small", {"numbers": "sample"}, want, False),
                 ("made by the service, big (by file)", {"numbers": "big"}, json.dumps(EXACT_SAMPLE * BIG_REPEAT), True))
        for name, payload, expect, by_ref in cases:
            job_id = jobs.submit(con, "catalogue.echo", dict(payload, text="exact"), sender="cli")
            job = jobs.wait(con, job_id, 30)
            full = jobs.get(con, job_id, with_result=True)
            got = json.dumps((full["result"] or {}).get("numbers"))
            row = con.execute("SELECT result_text, result_ref FROM orch.jobs WHERE job_id = %s", (job_id,)).fetchone()
            via = "file " + row[1] if row[1] else "text in the job row"
            good = job["state"] == "succeeded" and got == expect and bool(row[1]) == by_ref
            if row[1]:
                with open(os.path.join(ROOT, row[1]), encoding="utf-8", newline="") as fh:
                    good &= expect in fh.read()
            else:
                good &= expect in (row[0] or "")
            ok &= good
            print("%s %s: %d bytes of numbers, byte-identical %s (%s)"
                  % ("PASS" if good else "FAIL", name, len(got), "yes" if got == expect else "NO", via))
    finally:
        con.close()
    return _verdict(ok)


# ---- registration -----------------------------------------------------------------------------------------------
def register(sub):
    sub.add_parser("exact-check").set_defaults(f=cmd_exact_check)
    sub.add_parser("bus-init").set_defaults(f=cmd_bus_init)
    sub.add_parser("bus-status").set_defaults(f=cmd_bus_status)
    s = sub.add_parser("sql")
    s.add_argument("statement")
    s.set_defaults(f=cmd_sql)
    s = sub.add_parser("events")
    s.add_argument("job_id")
    s.set_defaults(f=cmd_events)
    s = sub.add_parser("send-echo")
    s.add_argument("--via", choices=("gateway", "direct"), default="gateway")
    s.add_argument("--text", default="hello")
    s.add_argument("--sleep", type=float, default=0.0)
    s.add_argument("--effect", action="store_true")
    s.add_argument("--fail", choices=("error", "retryable", "raise"))
    s.add_argument("--fail-until-attempt", type=int, default=None)
    s.add_argument("--once", action="store_true")
    s.add_argument("--deadline-s", type=float, default=None)
    s.add_argument("--no-wait", action="store_true")
    s.set_defaults(f=cmd_send_echo)
    s = sub.add_parser("echo-load")
    s.add_argument("n", type=int)
    s.add_argument("--concurrency", type=int, default=1)
    s.set_defaults(f=cmd_echo_load)
    s = sub.add_parser("consumer-ping")
    s.add_argument("--service", default="catalogue", choices=list(queues.SERVICES))
    s.set_defaults(f=cmd_consumer_ping)
