"""Every part's record and its derived facts.

    python -m framework.catalog.pg.partcards --status
    python -m framework.catalog.pg.partcards --extents          # exact extents + solids of parts missing them (geometry jobs:
                                                                #   the services must run)
    python -m framework.catalog.pg.partcards --refresh          # records + derived facts of every part (SQL only, seconds)
    python -m framework.catalog.pg.partcards --all              # the two steps above, in order

Steps (each idempotent, each only touches what is missing or stale):
1. extents: parts with no current `part.tight_bbox` fact, or whose geom_hash moved since it was measured, get one
   `geometry.tight_extent` job per 400 parts (the kernel: exact extent and each solid's volume and area);
   framework/catalog/pg/partrecord.write_extents stores them (and fills `part_solid`).
2. refresh: partrecord.build for every part; its derived facts are written (`source = 'derived'`); `part_card` gets the
   record and its sha256. A part whose record did not move but whose derived facts were closed by another writer gets
   them written again.
   Steps 1 and 2 write under the mirror's advisory lock (`buildcore_sync`), never inside or beside a sync's transaction.

The catalogue service runs `catch_up()` (both steps, only for stale parts) after its start-up work and then every
PARTCARDS_EVERY_S (600) seconds, in its own daemon thread (first replica only; PARTCARDS_AUTO=0 turns it off).
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "partcards.sql")
LOG = os.path.join(ROOT, "work", "logs", "partcards.jsonl")
EXTENT_CHUNK = int(os.environ.get("PARTCARDS_EXTENT_CHUNK", "100"))   # 400 large parts took 720 s of the 900 s budget
from framework.common.config import PROTECTED_DBS as PROTECTED  # noqa: E402  (one shared list)
SOURCE_DERIVED = "derived"     # pg/load.DERIVED_SOURCE (the part record's own facts)

# ---------------------------------------------------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------------------------------------------------

def connect():
    from framework.catalog.pg import db
    name = db.dbname()
    if name in PROTECTED:
        raise RuntimeError("refusing: %s is a live demo's database" % name)
    return db.connect(autocommit=True)


_ENSURED = set()


def ensure(con):
    """The tables and the dictionary rows (seed). Idempotent, and done once per
    process and database (the service's pass calls it every 10 minutes)."""
    from framework.catalog.pg import db, seed
    key = db.dbname()
    if key in _ENSURED:
        return
    with open(SQL, encoding="utf-8") as fh:
        con.execute(fh.read())
    seed.seed(con)
    _ENSURED.add(key)


def _log(event, **kw):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(event=event, at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), **kw)) + "\n")
    except OSError:
        pass


class _SyncLock:
    """The mirror's advisory lock (pg/sync.py, `buildcore_sync`) around this module's two writing steps, so they never run
    inside or beside a catalogue sync's transaction (one transaction per sync; readers keep answering meanwhile).
    Waits up to `wait_s`; not held -> Busy (the step is left for the next pass)."""

    def __init__(self, con, wait_s=120):
        self.con, self.wait_s = con, wait_s

    def __enter__(self):
        from framework.catalog.pg import signatures
        if not signatures._lock(self.con, wait_s=self.wait_s):
            raise Busy("a catalogue sync is running")
        return self

    def __exit__(self, *exc):
        self.con.execute("SELECT pg_advisory_unlock(hashtext('buildcore_sync'))")
        return False


class Busy(RuntimeError):
    pass


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------------------------------
# 1. extents
# ---------------------------------------------------------------------------------------------------------------------

def extent_targets(con, part_ids=None):
    """[(part_id, sources, geom_hash)] for parts with no current exact extent, or one measured for another geom_hash; a part
    whose measuring failed for its current geom_hash (`extent_failed_hash`) waits until its geometry changes."""
    from framework.catalog.pg import signatures
    rows = con.execute(
        "SELECT pr.part_id, pr.geom_hash FROM part_revision pr LEFT JOIN part_card pc ON pc.part_id = pr.part_id "
        "WHERE (%s::text[] IS NULL OR pr.part_id = ANY(%s)) AND (NOT EXISTS (SELECT 1 FROM fact f WHERE f.entity_kind = 'part' "
        "AND f.entity_id = pr.part_id AND f.property_id = 'part.tight_bbox' AND f.valid_to IS NULL) "
        "OR pc.extent_geom_hash IS DISTINCT FROM pr.geom_hash) "
        "AND pc.extent_failed_hash IS DISTINCT FROM pr.geom_hash ORDER BY pr.part_id",
        (part_ids, part_ids)).fetchall()
    out = []
    for pid, gh in rows:
        src = [[p, o] for p, o in signatures.shape_sources(con, pid)][:3]
        out.append((pid, src, gh))
    return out


def _mark_extent_failed(con, pairs):
    """Parts whose exact extent could not be measured for their current geometry: not asked again until it changes."""
    for pid, gh in pairs:
        con.execute("INSERT INTO part_card (part_id, facts_json, facts_hash, extent_failed_hash, status) "
                    "VALUES (%s, '{}', '', %s, 'pending') ON CONFLICT (part_id) DO UPDATE SET "
                    "extent_failed_hash = EXCLUDED.extent_failed_hash", (pid, gh))


def extents(con, part_ids=None, timeout_s=1800, say=print):
    """Step 1 through geometry.tight_extent jobs. -> counts. A part the kernel cannot measure (an error of its own, or no
    shape in its sources) is marked against its geom_hash and left; a job that fails does not stop the later chunks (the
    services being unavailable does)."""
    from framework.catalog.pg import partrecord
    from framework.common import jobcall
    say = say or (lambda *_: None)
    t0 = time.perf_counter()
    targets = extent_targets(con, part_ids)
    reachable = [t for t in targets if t[1]]
    out = {"stale": len(targets), "no_shape": len(targets) - len(reachable), "measured": 0, "written": 0, "solids": 0,
           "jobs": 0, "failed_parts": 0, "failed_jobs": 0, "moved": 0}
    for i in range(0, len(reachable), EXTENT_CHUNK):
        chunk = reachable[i:i + EXTENT_CHUNK]
        want = {p: gh for p, _s, gh in chunk}
        try:
            res = jobcall.call("geometry.tight_extent", {"targets": [[p, s] for p, s, _gh in chunk], "solids": True},
                               sender="catalogue", timeout_s=timeout_s)
            data = (jobcall.read_result_file(res) or {}).get("parts") or {}
        except jobcall.Unavailable:
            raise
        except Exception as exc:                      # this chunk's job failed: the next chunks still run
            out["failed_jobs"] += 1
            _log("extents_job_failed", error="%s: %s" % (type(exc).__name__, str(exc)[:200]), parts=len(chunk))
            continue
        out["jobs"] += 1
        got = {p: r for p, r in data.items() if r and not r.get("error") and r.get("extent")}
        failed = [(p, want[p]) for p in want if p not in got]
        out["measured"] += len(got)
        with _SyncLock(con), con.transaction():
            hashes = dict(con.execute("SELECT part_id, geom_hash FROM part_revision WHERE part_id = ANY(%s)",
                                      (list(want),)).fetchall())
            keep = {p: r for p, r in got.items() if hashes.get(p) == want[p]}     # a part whose geometry moved meanwhile
            out["moved"] += len(got) - len(keep)                                 #   is measured again next pass
            w = partrecord.write_extents(con, keep)
            for p in keep:
                con.execute("INSERT INTO part_card (part_id, facts_json, facts_hash, extent_geom_hash, status) "
                            "VALUES (%s, '{}', '', %s, 'pending') ON CONFLICT (part_id) DO UPDATE SET "
                            "extent_geom_hash = EXCLUDED.extent_geom_hash", (p, want[p]))
            bad = [(p, gh) for p, gh in failed if hashes.get(p) == gh]
            _mark_extent_failed(con, bad)
            out["failed_parts"] += len(bad)
        out["written"] += w["extents"]
        out["solids"] += w["solids"]
        say("extents: %d of %d measured (%d s)" % (min(i + EXTENT_CHUNK, len(reachable)), len(reachable),
                                                  time.perf_counter() - t0))
    out["s"] = round(time.perf_counter() - t0, 1)
    _log("extents", **out)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# 2. refresh
# ---------------------------------------------------------------------------------------------------------------------

def _derived_present(con, part_ids):
    """{part_id: {property_id}} of the parts' current derived facts."""
    out = {}
    for pid, prop in con.execute("SELECT entity_id, property_id FROM fact WHERE entity_kind = 'part' AND source = %s AND "
                                 "valid_to IS NULL AND entity_id = ANY(%s)", (SOURCE_DERIVED, list(part_ids))).fetchall():
        out.setdefault(pid, set()).add(prop)
    return out


def refresh(con, part_ids=None, wait_s=120):
    """Step 2. -> {"parts", "changed" (record hash moved or new), "healed" (record unchanged but some of its derived facts
    were closed by another writer, e.g. a mirror write of the part: written again), "facts_written"}.
    `wait_s`: how long to wait for the mirror's lock before Busy (a product load asks with 0 and retries itself)."""
    from framework.catalog.pg import partrecord
    t0 = time.perf_counter()
    changed = facts = healed = 0
    with _SyncLock(con, wait_s=wait_s), con.transaction():        # the records are built under the lock, from the rows a sync leaves
        ids = part_ids or [r[0] for r in con.execute("SELECT part_id FROM part_revision ORDER BY part_id").fetchall()]
        recs = partrecord.build(con, ids)
        old = dict(con.execute("SELECT part_id, facts_hash FROM part_card WHERE part_id = ANY(%s)", (list(recs),)).fetchall())
        present = _derived_present(con, list(recs))
        for pid, rec in recs.items():
            text = json.dumps(rec, sort_keys=True, ensure_ascii=False)
            h = sha(text)
            if old.get(pid) == h:
                want = {k for k, v in partrecord.derived_values(rec).items() if v is not None and v != []}
                if want - present.get(pid, set()):
                    facts += partrecord.write_derived(con, pid, rec)
                    healed += 1
                continue
            facts += partrecord.write_derived(con, pid, rec)
            con.execute("INSERT INTO part_card (part_id, facts_json, facts_hash, status) VALUES (%s, %s, %s, 'pending') "
                        "ON CONFLICT (part_id) DO UPDATE SET facts_json = EXCLUDED.facts_json, facts_hash = "
                        "EXCLUDED.facts_hash, made_at = now(), status = CASE WHEN part_card.described_hash = "
                        "EXCLUDED.facts_hash THEN part_card.status ELSE 'pending' END", (pid, text, h))
            changed += 1
    out = {"parts": len(recs), "changed": changed, "healed": healed, "facts_written": facts,
           "s": round(time.perf_counter() - t0, 1)}
    _log("refresh", **out)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# status, catch-up, CLI
# ---------------------------------------------------------------------------------------------------------------------

def status(con):
    q = lambda sql: con.execute(sql).fetchone()[0]
    return {
        "parts": q("SELECT COUNT(*) FROM part_revision"),
        "records": q("SELECT COUNT(*) FROM part_card WHERE facts_hash <> ''"),
        "with_mass": q("SELECT COUNT(DISTINCT entity_id) FROM fact WHERE entity_kind = 'part' AND property_id = 'part.mass' "
                       "AND valid_to IS NULL"),
        "with_exact_size": q("SELECT COUNT(DISTINCT entity_id) FROM fact WHERE entity_kind = 'part' AND property_id = "
                             "'part.tight_bbox' AND valid_to IS NULL"),
        "solids_rows": q("SELECT COUNT(*) FROM part_solid"),
    }


def catch_up(say=None):
    """The service's pass: steps 1-2 for what is missing or stale. Never raises. -> dict."""
    out = {}
    try:
        con = connect()
        try:
            ensure(con)
            try:
                out["extents"] = extents(con, say=say or (lambda *_: None))
            except Exception as exc:                     # the geometry service may not answer yet
                out["extents"] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
            try:
                out["refresh"] = refresh(con)
            except Busy as exc:                          # a sync holds the lock: next pass
                out["refresh"] = {"busy": str(exc)}
        finally:
            con.close()
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    _log("catch_up", **{k: v for k, v in out.items()})
    return out


def _setting(name, default):
    """A switch of the service's pass: the environment, else the gitignored `.env.services` (framework.common.config.setting).
    So `PARTCARDS_AUTO=0` written in `.env.services` is honoured like the environment variable."""
    from framework.common import config
    return config.setting(name, default)


def _loop(after):
    if after is not None:
        after.join()
    every = float(_setting("PARTCARDS_EVERY_S", "600"))
    while True:
        out = catch_up()
        try:
            r = out.get("refresh") or {}
            print("catalogue part records: %s parts, %s changed" % (r.get("parts"), r.get("changed")), flush=True)
        except Exception:
            pass
        time.sleep(every)


def start_loop(after=None, replica=1):
    """The catalogue service's background pass (first replica only; PARTCARDS_AUTO=0 or BC_PG_OFF turn it off)."""
    from framework.common.config import pg_off
    if str(_setting("PARTCARDS_AUTO", "1")).strip() in ("0", "false", "no") or int(replica or 1) != 1 or pg_off():
        return None
    t = threading.Thread(target=_loop, args=(after,), name="catalogue-part-records", daemon=True)
    t.start()
    return t


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for f in ("status", "extents", "refresh", "all"):
        ap.add_argument("--" + f, action="store_true")
    ap.add_argument("--parts", nargs="*", default=None)
    a = ap.parse_args(argv)
    con = connect()
    try:
        ensure(con)
        if a.extents or a.all:
            print(json.dumps(extents(con, a.parts), indent=1))
        if a.refresh or a.all:
            print(json.dumps(refresh(con, a.parts), indent=1))
        if a.status or not any((a.extents, a.refresh, a.all)):
            print(json.dumps(status(con), indent=1))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
