"""Postgres mirror of the SQLite catalogue: one re-runnable full resync.

    python -m framework.catalog.pg.sync --full [--sqlite PATH] [--no-verify]
    python -m framework.catalog.pg.sync --status [--sqlite PATH]
    python -m framework.catalog.pg.sync --catch-up [--sqlite PATH]
    python -m framework.catalog.pg.sync after-product <product_id> [--sqlite PATH]
    python -m framework.catalog.pg.sync after-remove  <product_id> [--sqlite PATH]
    python -m framework.catalog.pg.sync after-ingest  <doc_id> [<doc_id> ...] [--sqlite PATH]
    python -m framework.catalog.pg.sync after-write   [--sqlite PATH]

`--full` brings the database in BC_PG_DSN (or the one named in `.env.buildcore`) to
exactly the SQLite catalogue: it creates the database when missing (never drops one), applies
the schema, seeds the dictionary, replays every SQLite row through `migrate_sqlite` -> `pg/load.py`
(the ONE writer path; nothing is re-measured), prunes what SQLite no longer has (with the
fact history and signatures of those rows), backfills signatures, refreshes the read models,
verifies (counts, every numeric column, every product page) and only then stores the SQLite
fingerprint. `--status` compares counts and the stored fingerprint without writing.

Module top level is stdlib only: the write hooks import this module (fingerprint_sqlite, is_current) from
interpreters without psycopg; everything Postgres is imported inside the functions.

Writing refuses any database listed in BC_PROTECTED_DBS (db.refuse_main_db_in_worktree).
SQLite is opened read-only (URI mode=ro) everywhere here.

Incremental half: `after_product`, `after_remove`, `after_ingest` and
`after_write` run after a SQLite write. ONE choke point calls them: every writer connection
comes from framework/catalog/ingest.py connect() (a CatalogueConnection), and closing it calls
`on_write(db, label)` when it changed rows; the label the writer set picks the verb (product.load /
load_side -> after-product, product.remove -> after-remove, ingest.main -> after-ingest, anything
else -> after-write, which replays whatever differs). They mirror just the change
(SQLite rows replayed through migrate_sqlite -> pg/load.py; nothing is re-measured), prune what
SQLite no longer has, refresh the read models and compare fingerprints: equal -> stored; unequal
(writes were missed while Postgres was down, or an ingest --rebuild) -> the differences are replayed
(seconds) and, only if that is not enough, `full()` (minutes). They NEVER raise
and never delay the caller by more than a 2 s connect timeout when Postgres is down; every event is
one line in work/logs/catalogue_sync.log. Only the default catalogue is mirrored: a test or shadow
SQLite file (anything that is not DEFAULT_SQLITE) returns None at once, and `BC_PG_OFF=1` turns
the mirror off. `BC_PG_SQLITE_DEFAULT` replaces DEFAULT_SQLITE as "the default catalogue" - for
tests only (a test mirrors a tmp copy into a scratch database).

Called from an interpreter without psycopg (e.g. a system Python), the job is handed to the venv
interpreter (`MECH_PYTHON`, default ~/venvs/buildcore/bin/python, or
C:/venvs/buildcore/Scripts/python.exe on Windows) in a hidden subprocess running the CLI verb above.

Sync without gaps (so the catalogue page and the search never say "not available" while a large
design is being mirrored):
- Every sync (`_mirror` and `full`) writes in ONE transaction: the rows of what changed, prune, the
  shape-free signatures (geom_hash, moments_v1), the read models (REFRESH ... CONCURRENTLY inside
  the transaction) and the stored fingerprint, committed together. A reader sees the last good copy
  until that commit and the new one after it; a sync that fails rolls all of it back. Only then,
  in its own short writes, the shape signatures (d2_v1) of the parts just written (one geometry job
  in the services); the copy is current before they arrive.
- The rows go through pg/load.py's batched writer (one read of the current facts, one COPY), so
  the cost follows the size of the change, not two round trips per fact.
- `sync_running(con)` tells a reader that a sync holds the lock right now (pg_locks, nothing taken):
  the catalogue service keeps answering from the last good copy while it runs (svc_queries).
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

from framework.common import paths      # paths.basename splits a stored Windows path on Linux

# framework/catalog/pg/sync.py -> framework/catalog/pg -> framework/catalog -> framework -> root
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_SQLITE = os.path.abspath(os.environ.get("BC_CATALOG_DB") or os.path.join(ROOT, "framework", "catalog", "catalog.db"))   # BC_CATALOG_DB overrides the default catalogue

LOG_PATH = os.path.join(ROOT, "work", "logs", "catalogue_sync.log")
DEFAULT_MECH_PYTHON = (r"C:\venvs\buildcore\Scripts\python.exe" if os.name == "nt"     # per-OS default
                       else os.path.join(os.path.expanduser("~"), "venvs", "buildcore", "bin", "python"))

# exit code of the after-* / --catch-up verbs when the mirror did not happen and the child already
# wrote the reason into the log (so the parent that delegated does not log a second line)
EXIT_LOGGED_FAILURE = 3

FINGERPRINT_NAME = "sqlite_fingerprint"
_TS_FMT = "%Y-%m-%d %H:%M:%S"
_ENTITY_TABLES = ("document", "part_revision", "feature", "product", "occurrence", "interface")
COUNT_KEYS = ("documents", "parts", "features", "products", "occurrences", "interfaces")


# ---------------------------------------------------------------------------
# small helpers (stdlib only)
# ---------------------------------------------------------------------------

def _connect_sqlite(path):
    cx = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(path).replace("\\", "/"), uri=True)
    cx.row_factory = sqlite3.Row
    return cx


def parse_ts(text):
    """SQLite 'YYYY-MM-DD HH:MM:SS' (UTC) -> aware datetime; None when empty or unparsable."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("T", " ")).replace(
            microsecond=0, tzinfo=timezone.utc)
    except ValueError:
        return None


def _norm_ts(text):
    ts = parse_ts(text)
    return ts.strftime(_TS_FMT) if ts is not None else ("" if text is None else str(text))


def _s(value):
    return "" if value is None else str(value)


def _hash(counts6, products, documents):
    h = hashlib.sha256()
    h.update(("counts|" + "|".join(str(c) for c in counts6)).encode("utf-8"))
    for line in sorted("product|%s|%s|%s" % (_s(a), _s(b), _s(c)) for a, b, c in products):
        h.update(b"\n" + line.encode("utf-8"))
    for line in sorted("document|%s|%s|%s" % (_s(a), _s(b), _norm_ts(c)) for a, b, c in documents):
        h.update(b"\n" + line.encode("utf-8"))
    return h.hexdigest()


def sqlite_counts(path):
    """The six counts the mirror must equal, read live from SQLite."""
    sx = _connect_sqlite(path)
    try:
        q = lambda sql: sx.execute(sql).fetchone()[0]
        return {
            "documents": q("SELECT COUNT(*) FROM documents"),
            "parts": q("SELECT COUNT(*) FROM parts"),
            "features": q("SELECT COUNT(*) FROM features"),
            "products": q("SELECT COUNT(*) FROM products"),
            "occurrences": q("SELECT COALESCE(SUM(qty), 0) FROM product_parts"),
            "interfaces": q("SELECT COUNT(*) FROM interfaces"),
        }
    finally:
        sx.close()


def fingerprint_sqlite(path=DEFAULT_SQLITE):
    """sha256 over the table counts (documents, parts, features, products, product_parts,
    interfaces), the sorted (product_id, revision, created_at) rows and the sorted
    (doc_id, checksum_sha256, ingested_at) rows of the SQLite catalogue (read-only)."""
    sx = _connect_sqlite(path)
    try:
        q = lambda sql: sx.execute(sql).fetchone()[0]
        counts6 = [q("SELECT COUNT(*) FROM documents"), q("SELECT COUNT(*) FROM parts"),
                   q("SELECT COUNT(*) FROM features"), q("SELECT COUNT(*) FROM products"),
                   q("SELECT COUNT(*) FROM product_parts"), q("SELECT COUNT(*) FROM interfaces")]
        products = [tuple(r) for r in sx.execute("SELECT product_id, revision, created_at FROM products")]
        documents = [tuple(r) for r in sx.execute("SELECT doc_id, checksum_sha256, ingested_at FROM documents")]
    finally:
        sx.close()
    return _hash(counts6, products, documents)


def fingerprint_pg(con):
    """The same tuple built from Postgres (entity tables; document.ingested_at is the SQLite
    value carried through by migrate_sqlite, rendered in UTC with the SQLite text format)."""
    q = lambda sql: con.execute(sql).fetchone()[0]
    counts6 = [q("SELECT COUNT(*) FROM document"), q("SELECT COUNT(*) FROM part_revision"),
               q("SELECT COUNT(*) FROM feature"), q("SELECT COUNT(*) FROM product"),
               q("SELECT COUNT(*) FROM (SELECT DISTINCT product_id, ref_des FROM occurrence) t"),
               q("SELECT COUNT(*) FROM interface")]
    products = con.execute("SELECT product_id, revision, created_at FROM product").fetchall()
    documents = con.execute(
        "SELECT doc_id, sha256, to_char(ingested_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') "
        "FROM document").fetchall()
    return _hash(counts6, products, documents)


def pg_counts(con):
    q = lambda sql: con.execute(sql).fetchone()[0]
    return {
        "documents": q("SELECT COUNT(*) FROM document"),
        "parts": q("SELECT COUNT(*) FROM part_revision"),
        "features": q("SELECT COUNT(*) FROM feature"),
        "products": q("SELECT COUNT(*) FROM product"),
        "occurrences": q("SELECT COUNT(*) FROM occurrence"),
        "interfaces": q("SELECT COUNT(*) FROM interface"),
    }


def target_dbname():
    from framework.catalog.pg import db
    return db.dbname()


def _guard(name=None):
    """Raise when the database about to be written is listed in BC_PROTECTED_DBS."""
    from framework.catalog.pg import db
    db.refuse_main_db_in_worktree(name or db.dbname())


def _meta_exists(con):
    """True when readmodel_meta exists (False on a brand-new database, before the first full resync applies the
    schema): the probes below then answer "never synced" without a failing statement in the Postgres log."""
    return con.execute("SELECT to_regclass('readmodel_meta')").fetchone()[0] is not None


def stored_fingerprint(con):
    """-> (fingerprint, refreshed_at) or (None, None)."""
    if not _meta_exists(con):
        return None, None
    row = con.execute("SELECT ddl_hash, refreshed_at FROM readmodel_meta WHERE name=%s",
                      (FINGERPRINT_NAME,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def store_fingerprint(con, fingerprint, commit=True):
    """Store `fingerprint` as the copy's (`commit` False keeps it in the caller's transaction, so the
    fingerprint and the rows it describes become visible together). A stored fingerprint also clears the
    failed-resync mark (`last_sync_failed`)."""
    con.execute(
        """INSERT INTO readmodel_meta (name, ddl_hash, refreshed_at) VALUES (%s, %s, clock_timestamp())
           ON CONFLICT (name) DO UPDATE SET ddl_hash = EXCLUDED.ddl_hash,
               refreshed_at = EXCLUDED.refreshed_at""",
        (FINGERPRINT_NAME, fingerprint))
    con.execute("DELETE FROM readmodel_meta WHERE name=%s", (FAILED_NAME,))
    if commit and not con.autocommit:
        con.commit()


def _forget_fingerprint(con):
    """No stored fingerprint: the copy is not certified (an unverified `--full --no-verify`)."""
    con.execute("DELETE FROM readmodel_meta WHERE name=%s", (FINGERPRINT_NAME,))


# A full resync that could not bring the copy in sync (rolled back, or it raised) leaves this mark until a
# sync stores a fingerprint again. While it is there the catalogue service never answers from a lagging copy: a
# mismatch no sync repairs shows as "not available" instead of a frozen copy answering for ever.
FAILED_NAME = "sync_failed"


def _mark_failed(con, why):
    """Record that the last full resync failed (autocommit connection). Never raises."""
    try:
        con.execute(
            """INSERT INTO readmodel_meta (name, ddl_hash, refreshed_at) VALUES (%s, %s, clock_timestamp())
               ON CONFLICT (name) DO UPDATE SET ddl_hash = EXCLUDED.ddl_hash,
                   refreshed_at = EXCLUDED.refreshed_at""",
            (FAILED_NAME, _mask(why)[:300]))
    except Exception:
        pass


def last_sync_failed(con):
    """-> (why, when) when the last full resync failed and no sync has stored a fingerprint since, else None."""
    if not _meta_exists(con):
        return None
    row = con.execute("SELECT ddl_hash, refreshed_at FROM readmodel_meta WHERE name=%s", (FAILED_NAME,)).fetchone()
    return (row[0], row[1]) if row else None


_SYNC_RUNNING_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted AND objsubid = 1 "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND ((classid::bigint << 32) | objid::bigint) = hashtext('buildcore_sync')::bigint)")


def sync_running(con):
    """True when a session holds the mirror's advisory lock in this database right now (a write's mirror, a
    catch-up, a full resync or a signature fill): SQLite is ahead of the copy until it ends. Reads pg_locks;
    takes nothing. An advisory lock on one bigint key is listed as classid (high 32 bits), objid (low
    32 bits) and objsubid 1."""
    return bool(con.execute(_SYNC_RUNNING_SQL).fetchone()[0])


def is_current(sqlite_path=DEFAULT_SQLITE, con=None):
    """True when the mirror's stored fingerprint equals the SQLite catalogue's right now.
    False on any error (no psycopg, Postgres down, never synced); never raises."""
    own = None
    try:
        if con is None:
            from framework.catalog.pg import db
            own = con = db.connect(autocommit=True, connect_timeout=2)
        stored, _when = stored_fingerprint(con)
        return stored is not None and stored == fingerprint_sqlite(sqlite_path)
    except Exception:
        return False
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# prune: delete what SQLite no longer produces, with fact history
# ---------------------------------------------------------------------------

def _expected_ids(sqlite_path):
    """The ids the SQLite catalogue produces under migrate_sqlite's id rules."""
    sx = _connect_sqlite(sqlite_path)
    try:
        docs = {r[0] for r in sx.execute("SELECT doc_id FROM documents")}
        parts, features = set(), set()
        for doc_id, raw in sx.execute("SELECT doc_id, raw_json FROM parts"):
            record = json.loads(raw)
            part_id = "%s:%s" % (doc_id, record["object"])
            parts.add(part_id)
            for i in range(1, len(record.get("features") or []) + 1):
                features.add("%s/f%d" % (part_id, i))
        products, occurrences, interfaces = set(), set(), set()
        for (pid,) in sx.execute("SELECT product_id FROM products"):
            products.add(pid)
        for pid, ref, qty in sx.execute("SELECT product_id, ref_des, qty FROM product_parts"):
            for i in range(1, (qty or 1) + 1):
                occurrences.add("%s/%s#%d" % (pid, ref, i))
        counter = {}
        for (pid,) in sx.execute("SELECT product_id FROM interfaces ORDER BY product_id, interface_id"):
            counter[pid] = counter.get(pid, 0) + 1
            interfaces.add("%s/i%d" % (pid, counter[pid]))
    finally:
        sx.close()
    return {"documents": docs, "parts": parts, "features": features, "products": products,
            "occurrences": occurrences, "interfaces": interfaces}


def prune(con, sqlite_path=DEFAULT_SQLITE, commit=True):
    """Delete every document/part/feature/solid/product/occurrence/interface row whose id the
    SQLite catalogue no longer produces, their fact rows (all history, by entity kind and id),
    their signatures, and orphan part_master rows. -> counts of what was removed, and
    `products_touched`: the product ids whose rows it removed or changed.
    `commit`: False leaves the commit to the caller's transaction."""
    want = _expected_ids(sqlite_path)

    def have(sql):
        return {r[0] for r in con.execute(sql).fetchall()}

    gone = {
        "documents": have("SELECT doc_id FROM document") - want["documents"],
        "parts": have("SELECT part_id FROM part_revision") - want["parts"],
        "features": have("SELECT feature_id FROM feature") - want["features"],
        "products": have("SELECT product_id FROM product") - want["products"],
        "occurrences": have("SELECT occ_id FROM occurrence") - want["occurrences"],
        "interfaces": have("SELECT interface_id FROM interface") - want["interfaces"],
    }
    # parts of a pruned document go with it (document -> part_revision cascades)
    if gone["documents"]:
        gone["parts"] |= {r[0] for r in con.execute(
            "SELECT part_id FROM part_revision WHERE doc_id = ANY(%s)", (list(gone["documents"]),)
        ).fetchall()}
    # features of a pruned part, and the occurrences/interfaces of a pruned product, go with it
    if gone["parts"]:
        gone["features"] |= {r[0] for r in con.execute(
            "SELECT feature_id FROM feature WHERE part_id = ANY(%s)", (list(gone["parts"]),)).fetchall()}
    if gone["products"]:
        gone["occurrences"] |= {r[0] for r in con.execute(
            "SELECT occ_id FROM occurrence WHERE product_id = ANY(%s)", (list(gone["products"]),)).fetchall()}
        gone["interfaces"] |= {r[0] for r in con.execute(
            "SELECT interface_id FROM interface WHERE product_id = ANY(%s)", (list(gone["products"]),)).fetchall()}
    solids = set()
    if gone["parts"]:
        solids = {"%s#s%d" % (p, i) for p, i in con.execute(
            "SELECT part_id, solid_index FROM part_solid WHERE part_id = ANY(%s)",
            (list(gone["parts"]),)).fetchall()}

    # the designs whose rows this prune changes (their product_doc must be rebuilt, and a catch-up replays
    # those SQLite still has): the products removed, and those that lose an occurrence, an interface, or a feature of a
    # part they use. Read before anything is deleted.
    touched = set(gone["products"])
    for sql, ids in (("SELECT DISTINCT product_id FROM occurrence WHERE occ_id = ANY(%s)", gone["occurrences"]),
                     ("SELECT DISTINCT product_id FROM interface WHERE interface_id = ANY(%s)", gone["interfaces"]),
                     ("SELECT DISTINCT o.product_id FROM occurrence o JOIN feature f ON f.part_id = o.part_id "
                      "WHERE f.feature_id = ANY(%s)", gone["features"]),
                     ("SELECT DISTINCT product_id FROM occurrence WHERE part_id = ANY(%s)", gone["parts"])):
        if ids:
            touched |= {r[0] for r in con.execute(sql, (list(ids),)).fetchall()}

    removed = {k: len(v) for k, v in gone.items()}
    removed["solids"] = len(solids)
    removed["products_touched"] = sorted(touched)
    facts = 0
    for kind, ids in (("part", gone["parts"]), ("feature", gone["features"]), ("solid", solids),
                      ("product", gone["products"]), ("occurrence", gone["occurrences"]),
                      ("interface", gone["interfaces"])):
        if ids:
            facts += con.execute("DELETE FROM fact WHERE entity_kind=%s AND entity_id = ANY(%s)",
                                 (kind, list(ids))).rowcount
    removed["facts"] = facts

    if gone["interfaces"]:
        con.execute("DELETE FROM interface WHERE interface_id = ANY(%s)", (list(gone["interfaces"]),))
    if gone["occurrences"]:
        con.execute("DELETE FROM occurrence WHERE occ_id = ANY(%s)", (list(gone["occurrences"]),))
    if gone["products"]:
        con.execute("DELETE FROM product WHERE product_id = ANY(%s)", (list(gone["products"]),))
    if gone["parts"]:
        # signature, feature and part_solid cascade from part_revision; part_master cascades to it
        con.execute("DELETE FROM part_master WHERE part_no = ANY(%s)", (list(gone["parts"]),))
        con.execute("DELETE FROM part_revision WHERE part_id = ANY(%s)", (list(gone["parts"]),))
    if gone["features"]:
        con.execute("DELETE FROM feature WHERE feature_id = ANY(%s)", (list(gone["features"]),))
    if gone["documents"]:
        con.execute("DELETE FROM document WHERE doc_id = ANY(%s)", (list(gone["documents"]),))
    orphans = con.execute(
        "DELETE FROM part_master pm WHERE NOT EXISTS (SELECT 1 FROM part_revision pr WHERE pr.part_no = pm.part_no)"
    ).rowcount
    removed["part_masters"] = orphans
    if commit:
        con.commit()
    return removed


# ---------------------------------------------------------------------------
# verify: product pages
# ---------------------------------------------------------------------------

def _sqlite_page_payload(sx, product_id):
    """The two SELECTs framework/web/app.py's api_product runs, minus mesh_url."""
    prod = sx.execute("SELECT product_id, name, revision, description FROM products WHERE product_id=?",
                      (product_id,)).fetchone()
    if not prod:
        return None
    bom = [dict(zip(("ref", "qty", "label", "family", "volume_mm3", "bbox_a_mm", "bbox_b_mm",
                     "bbox_c_mm", "source"), r))
           for r in sx.execute("""SELECT pp.ref_des, pp.qty, pa.label, pa.family, pa.volume_mm3,
                pa.bbox_a_mm, pa.bbox_b_mm, pa.bbox_c_mm, d.path
                FROM product_parts pp JOIN parts pa ON pa.part_id=pp.part_id JOIN documents d ON d.doc_id=pa.doc_id
                WHERE pp.product_id=? ORDER BY pp.ref_des""", (product_id,))]
    for b in bom:
        b["source"] = paths.basename(b["source"])
    ifs = [dict(zip(("kind", "ref_a", "ref_b", "nominal_a_mm", "nominal_b_mm", "clearance_mm",
                     "fit_class", "contact_area_mm2"), r))
           for r in sx.execute("""SELECT kind, ref_a, ref_b, nominal_a_mm, nominal_b_mm, clearance_mm,
                fit_class, contact_area_mm2
                FROM interfaces WHERE product_id=? ORDER BY kind, nominal_a_mm DESC""", (product_id,))]
    return {"product_id": prod[0], "name": prod[1], "revision": prod[2], "description": prod[3],
            "bom": bom, "interfaces": ifs}


def _compare_pages(con, sqlite_path):
    from framework.catalog.pg import read_models
    sx = _connect_sqlite(sqlite_path)
    try:
        ids = [r[0] for r in sx.execute("SELECT product_id FROM products ORDER BY product_id")]
        diffs = []
        for pid in ids:
            want = json.dumps(_sqlite_page_payload(sx, pid), sort_keys=True)
            got = json.dumps(read_models.product_payload(con, pid), sort_keys=True)
            if want != got:
                diffs.append(pid)
    finally:
        sx.close()
    return len(ids), diffs


# ---------------------------------------------------------------------------
# the full resync
# ---------------------------------------------------------------------------

def _row_state(con):
    """(entity rows per table, total fact rows, closed fact rows, max fact_id)."""
    rows = {t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in _ENTITY_TABLES}
    total, closed, top = con.execute(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE valid_to IS NOT NULL), COALESCE(MAX(fact_id), 0) FROM fact"
    ).fetchone()
    return rows, total, closed, top


def _no_shapes(_targets):
    """A d2 provider that measures nothing (the shape signatures come after the commit)."""
    return {}


def _moments(con, part_ids):
    """geom_hash and moments_v1 of `part_ids` from their read-model record (no shape needed): what
    `signatures.backfill` writes for a part, without its shape signature (written in the sync's own
    transaction, so a part never appears without them). -> number of parts written."""
    from framework.catalog.pg import signatures
    n = 0
    for part_id in part_ids:
        record = signatures._part_record(con, part_id)
        if record is not None:
            signatures._write_moments(con, part_id, record)
            n += 1
    return n


def _refresh_in_txn(con, names):
    """REFRESH MATERIALIZED VIEW CONCURRENTLY each read model in the caller's transaction: readers keep the
    rows of the last commit (no exclusive lock) and see the new ones with the sync's commit."""
    for name in names:
        con.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY %s" % name)
    if names:
        con.execute("UPDATE readmodel_meta SET refreshed_at = clock_timestamp() WHERE name = ANY(%s)", (list(names),))


def full(sqlite_path=DEFAULT_SQLITE, verify=True, con=None, d2_provider=None):
    """Bring the target database to exactly the SQLite catalogue (see the module docstring).
    `con` (optional) is an open connection to a scratch database; by default the database of
    BC_PG_DSN is created when missing and connected. -> result dict.

    The rows, prune, geom_hash/moments_v1 of every part, the read models, the verification
    and the stored fingerprint are ONE transaction, committed only when the result is in sync; otherwise
    it is rolled back and the last good copy stays as it was (the result lists the differences).
    `verify=False` (`--no-verify`) commits the rows unverified and removes the stored fingerprint (the
    copy is not certified until a verified sync). After the commit, the shape signatures (d2_v1) of the
    parts that have none. `committed` and `seconds_to_commit` say when the new copy became visible."""
    from framework.catalog.pg import db, migrate_sqlite, read_models, seed, signatures

    t0 = time.perf_counter()
    own = con is None
    if own:
        name = db.dbname()
        db.refuse_main_db_in_worktree(name)
        created = db.ensure_db(name)
        con = db.connect(autocommit=True)
    else:
        name = con.info.dbname
        db.refuse_main_db_in_worktree(name)
        created = False
        if not con.autocommit:
            con.commit()
        con.autocommit = True
    result = {"database": name, "created": created, "sqlite": os.path.abspath(sqlite_path), "committed": False}
    locked = False
    try:
        # the lock first. schema.sql's ALTER TABLE ... IF NOT EXISTS takes an exclusive lock even when the
        # column exists; queued behind another sync's open transaction it would stall every reader. Under the lock no
        # sync transaction is open.
        con.execute("SELECT pg_advisory_lock(hashtext('buildcore_sync'))")
        locked = True
        db.apply_schema(con)
        seed.seed(con)
        names = read_models.ensure(con)          # DDL only when the dictionary changed; before the transaction
        con.autocommit = False

        rows0, total0, closed0, top0 = _row_state(con)
        result["migrated"] = migrate_sqlite.migrate(sqlite_path, con, commit=False)
        rows1, total1, closed1, top1 = _row_state(con)
        result["inserted"] = {t: max(0, rows1[t] - rows0[t]) for t in _ENTITY_TABLES}
        result["facts_opened"] = con.execute(
            "SELECT COUNT(*) FROM fact WHERE fact_id > %s", (top0,)).fetchone()[0]
        result["facts_closed"] = closed1 - closed0
        result["pruned"] = prune(con, sqlite_path, commit=False)
        every = [r[0] for r in con.execute("SELECT part_id FROM part_revision ORDER BY part_id").fetchall()]
        result["moments_written"] = _moments(con, every)     # every part was rewritten above
        _refresh_in_txn(con, names)
        read_models.refresh_products(con, None)              # every design's doc

        result["counts"] = {"sqlite": sqlite_counts(sqlite_path), "postgres": pg_counts(con)}
        result["mismatches"] = []
        result["payload_diffs"] = []
        result["pages"] = (0, 0)
        if verify:
            result["mismatches"] = migrate_sqlite.verify(sqlite_path, con)
            total_pages, diffs = _compare_pages(con, sqlite_path)
            result["payload_diffs"] = diffs
            result["pages"] = (total_pages - len(diffs), total_pages)

        counts_equal = result["counts"]["sqlite"] == result["counts"]["postgres"]
        fp_sqlite, fp_pg = fingerprint_sqlite(sqlite_path), fingerprint_pg(con)
        result["fingerprint_equal"] = fp_sqlite == fp_pg
        result["in_sync"] = bool(verify and counts_equal and not result["mismatches"]
                                 and not result["payload_diffs"] and result["fingerprint_equal"])
        result["fingerprint"] = fp_sqlite
        if result["in_sync"]:
            store_fingerprint(con, fp_sqlite, commit=False)
            con.commit()
            result["committed"] = True
        elif not verify:
            _forget_fingerprint(con)
            con.commit()
            result["committed"] = True
        else:
            con.rollback()                       # the last good copy stays; the result says what differed
        result["seconds_to_commit"] = time.perf_counter() - t0
        con.autocommit = True
        if not result["committed"]:
            _mark_failed(con, "full resync not in sync: counts %s, %d mismatches, %d pages differ, fingerprint %s" % (
                "equal" if counts_equal else "differ", len(result["mismatches"]), len(result["payload_diffs"]),
                "equal" if result["fingerprint_equal"] else "differs"))
        if result["committed"]:
            # the copy is committed and current; a failure here only leaves shape signatures missing
            # (filled at the catalogue service's start, or by `python -m framework.catalog.pg.signatures`)
            try:
                result["signatures_backfill"] = signatures.backfill(con, only_missing=True, d2_provider=d2_provider)
            except Exception as exc:
                result["signatures_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        result["signatures"] = {
            "with_d2": con.execute("SELECT COUNT(DISTINCT part_id) FROM signature WHERE method='d2_v1'").fetchone()[0],
            "with_moments": con.execute(
                "SELECT COUNT(DISTINCT part_id) FROM signature WHERE method='moments_v1'").fetchone()[0],
            "parts": con.execute("SELECT COUNT(*) FROM part_revision").fetchone()[0],
        }
    except BaseException as exc:
        if not result["committed"]:              # it raised before its commit: rolled back, the mark says so
            try:
                if not con.autocommit:
                    con.rollback()
                    con.autocommit = True
            except Exception:
                pass
            _mark_failed(con, "full resync failed: %s: %s" % (type(exc).__name__, exc))
        raise
    finally:
        try:
            if not con.autocommit:
                con.rollback()
                con.autocommit = True
        except Exception:
            pass
        if locked:
            try:
                con.execute("SELECT pg_advisory_unlock(hashtext('buildcore_sync'))")
            except Exception:
                pass
        if own:
            con.close()
    result["seconds"] = time.perf_counter() - t0
    return result


def mirror_running(con):
    """True when another session holds the mirror's advisory lock right now: a write's mirror, a
    catch-up or a full resync is running, so SQLite is ahead until it ends. Takes the lock
    for an instant only when it is free (autocommit connection)."""
    got = con.execute("SELECT pg_try_advisory_lock(hashtext('buildcore_sync'))").fetchone()[0]
    if got:
        con.execute("SELECT pg_advisory_unlock(hashtext('buildcore_sync'))")
    return not got


def status(sqlite_path=DEFAULT_SQLITE):
    """Read-only comparison. -> dict (counts both sides, stored fingerprint and its time, in_sync,
    mirror_running)."""
    from framework.catalog.pg import db
    out = {"database": db.dbname(), "counts": {"sqlite": sqlite_counts(sqlite_path)},
           "stored_fingerprint": None, "refreshed_at": None, "in_sync": False, "mirror_running": False}
    try:
        con = db.connect(autocommit=True, connect_timeout=5)
    except Exception as exc:
        out["error"] = str(exc)
        return out
    try:
        out["counts"]["postgres"] = pg_counts(con)
        stored, when = stored_fingerprint(con)
        out["stored_fingerprint"], out["refreshed_at"] = stored, when
        out["in_sync"] = bool(stored is not None and stored == fingerprint_sqlite(sqlite_path)
                              and out["counts"]["sqlite"] == out["counts"]["postgres"])
        try:
            out["mirror_running"] = sync_running(con)
            out["last_full_failed"] = last_sync_failed(con)
        except Exception:
            pass
    finally:
        con.close()
    return out


# ---------------------------------------------------------------------------
# incremental dual write: hooks, delegation, log, catch-up
# ---------------------------------------------------------------------------

def _default_sqlite():
    """The catalogue that is mirrored: BC_PG_SQLITE_DEFAULT (tests only) or DEFAULT_SQLITE."""
    return os.environ.get("BC_PG_SQLITE_DEFAULT") or DEFAULT_SQLITE


def _off():
    from framework.common.config import pg_off
    return pg_off()


def _mirrored(db=None):
    """True only when BC_PG_OFF is unset and `db` (None = the default) IS the default catalogue.
    Test catalogues and the kernel shadow catalogue are other files: never mirrored."""
    if _off():
        return False
    try:
        return os.path.samefile(db or _default_sqlite(), _default_sqlite())
    except OSError:
        return False


def _have_psycopg():
    try:
        import psycopg  # noqa: F401
        return True
    except Exception:
        return False


_DSN_RX = re.compile(r"(://[^:/@\s]+:)[^@\s]+@")


def _mask(text):
    return _DSN_RX.sub(r"\1***@", str(text))


def _local_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _ms(t0):
    return int((time.perf_counter() - t0) * 1000)


def _fail(verb, ids, exc, t0):
    _log(verb, ids, "failed: %s: %s" % (type(exc).__name__, exc), _ms(t0))
    return False


def _log_path():
    return os.environ.get("BC_PG_SYNC_LOG") or LOG_PATH      # the env knob is for tests only


def _log(verb, ids, outcome, ms=0):
    """One line: local ISO time with offset, verb, ids, milliseconds, outcome. Never raises."""
    try:
        os.makedirs(os.path.dirname(_log_path()), exist_ok=True)
        ids = ",".join(str(i) for i in (ids or [])) or "-"
        text = _mask(outcome).replace("\r", " ").replace("\n", " ")[:300]
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write("%s %s %s %d ms %s\n" % (_local_iso(), verb, ids, ms, text))
    except Exception:
        pass


def last_log_line():
    try:
        with open(_log_path(), "r", encoding="utf-8") as fh:
            lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
        return lines[-1] if lines else None
    except OSError:
        return None


def _delegate(verb, args, sqlite_path=None):
    """Run `python -m framework.catalog.pg.sync <verb> <args> [--sqlite PATH]` with the venv
    interpreter in a hidden subprocess. The child writes its own log line; a failure to run
    it is logged here. -> True when it exited 0."""
    t0 = time.perf_counter()
    py = os.environ.get("MECH_PYTHON") or DEFAULT_MECH_PYTHON
    ids = [a for a in args if not str(a).startswith("--")]
    if not os.path.isfile(py):
        _log(verb, ids, "mirror skipped: no venv interpreter", 0)
        return False
    if os.environ.get("BC_PG_SYNC_CHILD"):
        _log(verb, ids, "mirror skipped: the venv interpreter has no psycopg", 0)
        return False
    cmd = [py, "-m", "framework.catalog.pg.sync", verb] + [str(a) for a in args]
    if sqlite_path:
        cmd += ["--sqlite", os.path.abspath(sqlite_path)]
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "UV_INTERNAL__PYTHONHOME")}
    env["BC_PG_SYNC_CHILD"] = "1"
    try:
        cp = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        return _fail(verb, ids, exc, t0)
    if cp.returncode not in (0, EXIT_LOGGED_FAILURE):
        tail = (cp.stderr.strip().splitlines() or cp.stdout.strip().splitlines() or [""])[-1]
        _log(verb, ids, "failed: delegate exit %d: %s" % (cp.returncode, tail), _ms(t0))
    return cp.returncode == 0


def _changed_docs(con, sqlite_path, doc_ids):
    """Of `doc_ids`, the documents whose Postgres copy is missing or differs (path, checksum,
    ingestion time, number of parts). A document is content-addressed and re-ingested with a new
    ingestion time, so an equal tuple means its parts and features are already mirrored."""
    if not doc_ids:
        return []
    sx = _connect_sqlite(sqlite_path)
    try:
        want = {}
        for did in doc_ids:
            r = sx.execute("SELECT doc_id, path, checksum_sha256, ingested_at, "
                           "(SELECT COUNT(*) FROM parts p WHERE p.doc_id = d.doc_id) "
                           "FROM documents d WHERE doc_id=?", (did,)).fetchone()
            if r:
                want[did] = (r[1], r[2], _norm_ts(r[3]), r[4])
    finally:
        sx.close()
    have = {r[0]: (r[1], r[2], r[3], r[4]) for r in con.execute(
        "SELECT d.doc_id, d.path, d.sha256, to_char(d.ingested_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'), "
        "(SELECT COUNT(*) FROM part_revision pr WHERE pr.doc_id = d.doc_id) "
        "FROM document d WHERE d.doc_id = ANY(%s)", (list(want),)).fetchall()}
    return [d for d in want if have.get(d) != want[d]]


def _sqlite_product_docs(sqlite_path, product_id):
    sx = _connect_sqlite(sqlite_path)
    try:
        return [r[0] for r in sx.execute(
            "SELECT DISTINCT pa.doc_id FROM product_parts pp JOIN parts pa ON pa.part_id = pp.part_id "
            "WHERE pp.product_id = ?", (product_id,))]
    finally:
        sx.close()


def _delta_work(con, sqlite_path, ids=None):
    """Catch-up body: replay only what differs from SQLite (documents whose Postgres copy is
    missing or different, products whose revision/created_at differ, and the products whose bill
    of materials uses a replayed document), prune what SQLite no longer has. Seconds instead of the
    full resync's minutes; the fingerprint decides whether it was enough. `ids` (a list) receives
    the products and documents replayed, for the log line."""
    from framework.catalog.pg import migrate_sqlite
    sx = _connect_sqlite(sqlite_path)
    try:
        doc_ids = [r[0] for r in sx.execute("SELECT doc_id FROM documents")]
        want = {r[0]: (_s(r[1]), _s(r[2])) for r in sx.execute(
            "SELECT product_id, revision, created_at FROM products")}
    finally:
        sx.close()
    docs = _changed_docs(con, sqlite_path, doc_ids)
    have = {r[0]: (_s(r[1]), _s(r[2])) for r in con.execute(
        "SELECT product_id, revision, created_at FROM product").fetchall()}
    products = {pid for pid in want if have.get(pid) != want[pid]}
    if docs:
        products |= set(_sqlite_products_using(sqlite_path, docs))
    products = sorted(products)
    migrate_sqlite.migrate(sqlite_path, con, only_products=products, only_docs=docs, commit=False)
    touched = prune(con, sqlite_path, commit=False)["products_touched"]
    # a design whose occurrences or interfaces the prune removed although its revision and time did not
    # change (writes missed while Postgres was down): its remaining rows are replayed too (interfaces are numbered by
    # order, so the ones after a removed one moved), and its product_doc rebuilt with the others
    extra = sorted(p for p in touched if p in want and p not in products)
    if extra:
        migrate_sqlite.migrate(sqlite_path, con, only_products=extra, only_docs=[], commit=False)
    if ids is not None:
        ids.extend(products + extra + sorted(docs))
    return {"parts": _written_parts(con, docs), "products": sorted(set(products) | set(touched))}


def _split_written(out):
    """A work body's answer -> (part ids, product ids): a list of part ids, or {"parts", "products"}."""
    if isinstance(out, dict):
        return list(out.get("parts") or []), set(out.get("products") or [])
    return list(out or []), set()


def _sqlite_products_using(sqlite_path, doc_ids):
    sx = _connect_sqlite(sqlite_path)
    try:
        return [r[0] for r in sx.execute(
            "SELECT DISTINCT pp.product_id FROM product_parts pp JOIN parts pa ON pa.part_id = pp.part_id "
            "WHERE pa.doc_id IN (%s)" % ",".join("?" * len(doc_ids)), list(doc_ids))]
    finally:
        sx.close()


def _apply(con, sqlite_path, work, delta_fallback=True):
    """ONE transaction: `work(con, sqlite_path)` writes the change and returns the part ids
    it wrote (or {"parts", "products"}); when the fingerprints then differ (writes were missed while
    Postgres was down) and `delta_fallback`, every other difference is replayed too (`_delta_work`).
    Equal fingerprints -> geom_hash/moments_v1 of the parts written, the read models (the class views
    refreshed, product_doc rebuilt only for the designs written or using a part written) and the stored
    fingerprint join the same transaction, which is committed: readers move from the last good copy to
    the new one at once. Unequal -> rolled back, nothing changed. `con` is an autocommit connection
    holding the mirror's lock; it is left in autocommit.
    -> ("ok" | "caught up", part ids written) or (None, []) when it was rolled back."""
    from framework.catalog.pg import read_models     # heavy imports only once Postgres answered
    names = read_models.ensure(con)                   # DDL only when the dictionary changed; before the transaction
    con.autocommit = False
    try:
        outcome = "ok"
        part_ids, products = _split_written(work(con, sqlite_path))
        fp_sqlite = fingerprint_sqlite(sqlite_path)
        if fingerprint_pg(con) != fp_sqlite:
            if not delta_fallback:
                con.rollback()
                return None, []
            more_parts, more_products = _split_written(_delta_work(con, sqlite_path))
            part_ids += more_parts
            products |= more_products
            outcome = "caught up"
            fp_sqlite = fingerprint_sqlite(sqlite_path)
            if fingerprint_pg(con) != fp_sqlite:
                con.rollback()
                return None, []
        part_ids = sorted(set(part_ids))
        if part_ids:                                  # every design that uses a part written
            products |= {r[0] for r in con.execute(
                "SELECT DISTINCT product_id FROM occurrence WHERE part_id = ANY(%s)", (part_ids,)).fetchall()}
        _moments(con, part_ids)
        _refresh_in_txn(con, names)
        read_models.refresh_products(con, sorted(products))   # and drops the docs of designs gone
        store_fingerprint(con, fp_sqlite, commit=False)
        con.commit()
        return outcome, part_ids
    except BaseException:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            con.autocommit = True
        except Exception:
            pass


def _shapes(con, part_ids, d2_provider):
    """After the commit: the shape signatures (d2_v1) of the parts just written that have none, from
    `d2_provider` (the services: one geometry job) or the in-process kernel (None: tests, offline tools).
    Written under the mirror's lock, autocommit. A failure here leaves the copy current; the catalogue
    service fills missing ones at its start (svc_start.fill_missing_d2)."""
    from framework.catalog.pg import signatures
    if part_ids:
        signatures.backfill(con, only_missing=True, part_ids=part_ids, d2_provider=d2_provider)


def _mirror(verb, ids, sqlite_path, work, ok_word="ok", delta_fallback=True, d2_provider=None):
    """Shared body of after_product / after_remove / after_ingest / catch_up. `work(con, sqlite_path)`
    writes the change (in the sync's one transaction, `_apply`) and returns the part ids it wrote (for
    signatures). When the fingerprints then differ (writes were missed while Postgres was down) the
    differences are replayed (`_delta_work`) in the same transaction and, if that is not enough, the
    full resync runs. Then the shape signatures of the parts written (`_shapes`). Never raises. The log
    line gives the total time and when the new copy became visible ("current after N ms").
    -> True (ok / caught up), False (anything else)."""
    t0 = time.perf_counter()
    con, locked, outcome, need_full, current_ms = None, False, None, False, None
    try:
        from framework.catalog.pg import db
        _guard()
        try:
            con = db.connect(autocommit=True, connect_timeout=2)
        except Exception:
            _log(verb, ids, "unreachable", _ms(t0))
            return False
        con.execute("SET lock_timeout = '120s'")
        con.execute("SELECT pg_advisory_lock(hashtext('buildcore_sync'))")
        locked = True
        if con.execute("SELECT to_regclass('public.document')").fetchone()[0] is None:
            need_full = True                       # schema never applied: the full resync creates it
        else:
            res, part_ids = _apply(con, sqlite_path, work, delta_fallback)
            if res is None:
                need_full = True
            else:
                outcome = ok_word if res == "ok" else "caught up"
                current_ms = _ms(t0)
                try:
                    _shapes(con, part_ids, d2_provider)
                except Exception as exc:           # the copy is current; only shape signatures are missing
                    _log(verb, ids, "shape signatures not written: %s: %s" % (type(exc).__name__, exc), _ms(t0))
    except Exception as exc:
        return _fail(verb, ids, exc, t0)
    finally:
        if con is not None:
            try:
                if locked:
                    if not con.autocommit:
                        con.rollback()
                    con.execute("SELECT pg_advisory_unlock(hashtext('buildcore_sync'))")
            except Exception:
                pass
            try:
                con.close()
            except Exception:
                pass
    if need_full:
        # our connection (and its advisory lock) is closed: full() takes the lock itself
        try:
            res = full(sqlite_path, d2_provider=d2_provider)
        except Exception as exc:
            return _fail(verb, ids, exc, t0)
        outcome = "caught up" if res["in_sync"] else "out of sync"
        _log(verb, ids, outcome, _ms(t0))
        return bool(res["in_sync"])
    _log(verb, ids, "current after %d ms, %s" % (current_ms, outcome), _ms(t0))
    return True


def _written_parts(con, docs):
    return [r[0] for r in con.execute(
        "SELECT part_id FROM part_revision WHERE doc_id = ANY(%s)", (list(docs),)).fetchall()]


def after_product(db=None, product_id=None, d2_provider=None):
    """Mirror one product (BOM, interfaces, its documents when changed) after product.load.
    -> None (not mirrored), True, or False. Never raises."""
    try:
        if not _mirrored(db):
            return None
        sqlite_path = os.path.abspath(db or _default_sqlite())
        if not _have_psycopg():
            return _delegate("after-product", [product_id], sqlite_path)

        def work(con, path):
            from framework.catalog.pg import migrate_sqlite
            docs = _changed_docs(con, path, _sqlite_product_docs(path, product_id))
            migrate_sqlite.migrate(path, con, only_products=[product_id], only_docs=docs, commit=False)
            touched = prune(con, path, commit=False)["products_touched"]
            # the product's own parts too, written or not: backfill then keeps their geom_hash / moments_v1 equal to
            # the read-model record's (a load of a design repairs a hash an earlier write left), and fills a missing d2
            own = [r[0] for r in con.execute("SELECT DISTINCT part_id FROM occurrence WHERE product_id = %s", (product_id,)).fetchall()]
            return {"parts": sorted(set(_written_parts(con, docs)) | set(own)), "products": [product_id] + touched}
        return _mirror("after-product", [product_id], sqlite_path, work, d2_provider=d2_provider)
    except Exception as exc:
        return _fail("after-product", [product_id], exc, time.perf_counter())


def after_remove(db=None, product_id=None, d2_provider=None):
    """Mirror an erase (product.remove): the product, its occurrences/interfaces and facts, the
    documents and parts SQLite no longer has. -> None, True or False. Never raises."""
    try:
        if not _mirrored(db):
            return None
        sqlite_path = os.path.abspath(db or _default_sqlite())
        if not _have_psycopg():
            return _delegate("after-remove", [product_id], sqlite_path)

        def work(con, path):
            return {"parts": [], "products": prune(con, path, commit=False)["products_touched"]}
        return _mirror("after-remove", [product_id], sqlite_path, work, d2_provider=d2_provider)
    except Exception as exc:
        return _fail("after-remove", [product_id], exc, time.perf_counter())


def after_ingest(db=None, doc_ids=(), d2_provider=None):
    """Mirror the documents one ingest wrote. -> None, True or False. Never raises."""
    doc_ids = [d for d in (doc_ids or []) if d]
    try:
        if not _mirrored(db):
            return None
        sqlite_path = os.path.abspath(db or _default_sqlite())
        if not _have_psycopg():
            return _delegate("after-ingest", doc_ids, sqlite_path)

        def work(con, path):
            from framework.catalog.pg import migrate_sqlite
            docs = _changed_docs(con, path, doc_ids)
            migrate_sqlite.migrate(path, con, only_products=[], only_docs=docs, commit=False)
            touched = prune(con, path, commit=False)["products_touched"]
            return {"parts": _written_parts(con, docs), "products": touched}
        return _mirror("after-ingest", doc_ids, sqlite_path, work, d2_provider=d2_provider)
    except Exception as exc:
        return _fail("after-ingest", doc_ids, exc, time.perf_counter())


def after_write(db=None, d2_provider=None):
    """Mirror a write nobody labelled (ingest.ingest_file with its own connection, a script that
    writes through ingest.connect()): replay whatever differs from SQLite (`_delta_work`). The log
    line names what was replayed. -> None, True or False. Never raises."""
    try:
        if not _mirrored(db):
            return None
        sqlite_path = os.path.abspath(db or _default_sqlite())
        if not _have_psycopg():
            return _delegate("after-write", [], sqlite_path)
        ids = []
        return _mirror("after-write", ids, sqlite_path, lambda con, path: _delta_work(con, path, ids), d2_provider=d2_provider)
    except Exception as exc:
        return _fail("after-write", [], exc, time.perf_counter())


def on_write(db=None, label=None, d2_provider=None):
    """THE choke point: framework/catalog/ingest.py CatalogueConnection.close() calls this
    after any writer connection that changed rows. `label` (set by the writer) picks the verb:
    ("after-product", pid), ("after-remove", pid), ("after-ingest", [doc_ids]); None -> after_write.
    `d2_provider`: where the shape signatures of the parts this write added come from (see
    `signatures.backfill`); None = the in-process kernel path (tests, offline tools).
    Not the default catalogue (tests, the kernel shadow) or BC_PG_OFF -> None at once.
    -> None, True or False. Never raises."""
    try:
        if not _mirrored(db):
            return None
        verb, arg = label if isinstance(label, (tuple, list)) and len(label) == 2 else (None, None)
        if verb == "after-product" and arg:
            return after_product(db, arg, d2_provider=d2_provider)
        if verb == "after-remove" and arg:
            return after_remove(db, arg, d2_provider=d2_provider)
        if verb == "after-ingest":
            return after_ingest(db, list(arg or []), d2_provider=d2_provider)
        return after_write(db, d2_provider=d2_provider)
    except Exception:
        return None


def catch_up(sqlite_path=None, d2_provider=None, verb="catch-up"):
    """When the mirror is not current (missed writes): replay the differences, and run the full
    resync only if that is not enough. -> None (mirror off), True (current, or brought current) or
    False. Never raises. `d2_provider` as in `on_write` (the catalogue service passes one, so a
    catch-up at its start never measures in its own process); `verb` names the log line."""
    t0 = time.perf_counter()
    try:
        if _off():
            return None
        sqlite_path = os.path.abspath(sqlite_path or _default_sqlite())
        if not _have_psycopg():
            return _delegate("--catch-up", [], sqlite_path)
        from framework.catalog.pg import db
        try:
            db.connect(autocommit=True, connect_timeout=2).close()
        except Exception:
            _log(verb, [], "unreachable", _ms(t0))
            return False
        if is_current(sqlite_path):
            _log(verb, [], "ok (already current)", _ms(t0))
            return True
        return _mirror(verb, [], sqlite_path, _delta_work, ok_word="caught up", delta_fallback=False,
                       d2_provider=d2_provider)
    except Exception as exc:
        return _fail(verb, [], exc, t0)


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def _header():
    from framework.catalog.pg import db
    from psycopg.conninfo import conninfo_to_dict
    info = conninfo_to_dict(db.dsn())
    return "target database: %s (%s:%s)" % (db.dbname(), info.get("host") or "localhost",
                                           info.get("port") or "5432")


def _print_counts(counts):
    print("%-12s %10s %10s" % ("", "sqlite", "postgres"))
    for key in COUNT_KEYS:
        s, p = counts["sqlite"].get(key), counts.get("postgres", {}).get(key)
        print("%-12s %10s %10s%s" % (key, "{:,}".format(s), "-" if p is None else "{:,}".format(p),
                                     "" if s == p else "  <-- DIFFERENT"))


_AFTER = {"after-product": after_product, "after-remove": after_remove, "after-ingest": after_ingest,
          "after-write": after_write}


def _main_after(argv):
    ap = argparse.ArgumentParser(prog="sync")
    ap.add_argument("verb", choices=sorted(_AFTER))
    ap.add_argument("ids", nargs="*", help="product_id (after-product, after-remove), doc_id(s) (after-ingest), "
                                           "nothing (after-write)")
    ap.add_argument("--sqlite", default=None, help="catalog.db that was written (default: the default catalogue)")
    args = ap.parse_args(argv)
    if args.verb == "after-ingest":
        res = after_ingest(args.sqlite, args.ids)
    elif args.verb == "after-write":
        res = after_write(args.sqlite)
    else:
        if len(args.ids) != 1:
            ap.error("%s needs one product_id" % args.verb)
        res = _AFTER[args.verb](args.sqlite, args.ids[0])
    print("%s: %s" % (args.verb, {None: "not mirrored (not the default catalogue, or BC_PG_OFF)",
                                  True: "ok",
                                  False: "not mirrored to Postgres (see %s)" % LOG_PATH}[res]))
    return 0 if res is not False else EXIT_LOGGED_FAILURE


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in _AFTER:
        return _main_after(argv)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--full", action="store_true", help="resync the whole catalogue (re-runnable)")
    mode.add_argument("--status", action="store_true", help="compare counts and the stored fingerprint")
    mode.add_argument("--catch-up", action="store_true",
                      help="full resync only when the mirror missed writes (not current)")
    ap.add_argument("--sqlite", default=DEFAULT_SQLITE, help="catalog.db to mirror (default: %s)" % DEFAULT_SQLITE)
    ap.add_argument("--no-verify", action="store_true", help="skip the verification: commits the rows unverified and removes the stored "
                                                               "fingerprint (the copy is not answered from until a verified sync)")
    args = ap.parse_args(argv)

    if args.catch_up:
        ok = catch_up(args.sqlite)
        print("catch-up: %s" % {None: "mirror is off (BC_PG_OFF)", True: "in sync: yes",
                                False: "in sync: no (see %s)" % LOG_PATH}[ok])
        return 0 if ok is not False else EXIT_LOGGED_FAILURE

    print(_header())
    if args.status:
        st = status(args.sqlite)
        if st.get("error"):
            print("postgres: not reachable (%s)" % st["error"].splitlines()[0][:200])
            return 1
        _print_counts(st["counts"])
        print("stored fingerprint: %s" % (("%s, at %s" % (st["stored_fingerprint"][:12], st["refreshed_at"]))
                                           if st["stored_fingerprint"] else "none (never synced)"))
        print("in sync: %s" % ("yes" if st["in_sync"] else "no"))
        print("mirror running now: %s" % ("yes (a write is being mirrored; SQLite is ahead until it ends, "
                                          "then the log gets its line)" if st["mirror_running"] else "no"))
        if st.get("last_full_failed"):
            print("last full resync failed (%s, at %s): the search and the catalogue page do not answer from a lagging "
                  "copy until a sync succeeds" % st["last_full_failed"])
        print("last log line: %s" % (last_log_line() or "none"))
        return 0 if st["in_sync"] else 1

    res = full(args.sqlite, verify=not args.no_verify)
    if res["created"]:
        print("created database %s" % res["database"])
    _print_counts(res["counts"])
    ins = res["inserted"]
    print("inserted rows: %d (%s); facts opened: %d, facts closed: %d" % (
        sum(ins.values()), ", ".join("%s %d" % (t, n) for t, n in ins.items() if n) or "none",
        res["facts_opened"], res["facts_closed"]))
    if not args.no_verify:
        print("verify: %d mismatches" % len(res["mismatches"]))
        for m in res["mismatches"][:20]:
            print("  " + m)
        ok, total = res["pages"]
        print("product pages: %d/%d identical" % (ok, total))
        for pid in res["payload_diffs"][:20]:
            print("  differs: %s" % pid)
    sig = res["signatures"]
    print("signatures: d2_v1 %d of %d parts (the rest have no reachable geometry), moments_v1 %d" % (
        sig["with_d2"], sig["parts"], sig["with_moments"]))
    pr = res["pruned"]
    print("pruned: %s" % (", ".join("%s %d" % (k, v) for k, v in pr.items() if v and isinstance(v, int)) or "nothing"))
    print("fingerprint equal (sqlite vs postgres): %s" % ("yes" if res["fingerprint_equal"] else "no"))
    print("in sync: %s" % ("yes" if res["in_sync"] else "no"))
    if res["committed"]:
        print("committed after %.1f s (the copy changed at once; shape signatures after that)%s" % (
            res["seconds_to_commit"], "" if res["in_sync"] else "; unverified: no stored fingerprint"))
    else:
        print("rolled back: the copy is as it was before this run (marked: last full resync failed)")
    if res.get("signatures_error"):
        print("shape signatures not written after the commit: %s (the copy is current)" % res["signatures_error"])
    print("elapsed: %.1f s" % res["seconds"])
    return 0 if res["in_sync"] else 1


if __name__ == "__main__":
    sys.exit(main())
