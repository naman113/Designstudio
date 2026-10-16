"""Idempotent SQLite -> Postgres catalogue sync: read the existing
`framework/catalog/catalog.db` (or any catalogue built the same way) READ-ONLY, and
replay every document/part/product/interface/spec row through the ONE writer path
(`framework.catalog.pg.load`). Never writes to the SQLite file. Safe to run again --
`write_part`/`write_product`/`write_interfaces`'s own history rule makes an
unchanged row a no-op, not a duplicate; a genuinely changed row closes the old fact
(`valid_to`) and inserts the new one.

    python -m framework.catalog.pg.migrate_sqlite [--sqlite PATH] [--db NAME] [--with-solids] [--verify]

`--with-solids` reloads each STEP document's shapes (via `framework.kernel.io.load`) so
per-solid provenance (`part_solid` + `solid.*` facts) is written too -- skipped,
with a printed reason, for a non-STEP (e.g. `.fcstd`) document, since the kernel only
reads STEP.
"""
import argparse
import json
import os
import sqlite3
import sys

from framework.catalog.pg import db as _db
from framework.catalog.pg import load as _load
from framework.catalog.pg import sync as _sync   # stdlib-only at top level: no import cycle

# framework/catalog/pg/migrate_sqlite.py -> framework/catalog/pg -> framework/catalog -> framework -> root
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_SQLITE = os.path.abspath(os.environ.get("BC_CATALOG_DB") or os.path.join(ROOT, "framework", "catalog", "catalog.db"))   # BC_CATALOG_DB overrides the default catalogue

# products/interfaces/specs have no single per-row tool_version the way a part's own
# document does (a product can span several documents) -- one fixed migration tag.
SQLITE_METHOD_VERSION = "sqlite:migrate"


def _connect_sqlite(path):
    cx = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    cx.row_factory = sqlite3.Row
    return cx


def _ensure_spec_property(con, key, is_num):
    """specs.<key> -> spec.<key>, auto-registered straight into the DB `property` table
    (dictionary.json is not changed; the dictionary-driven-schema rule still holds: a new
    property is a row, never a migration)."""
    prop_id = "spec.%s" % key
    con.execute(
        """INSERT INTO property (property_id, name, applies_to, datatype, definition, stored_as)
           VALUES (%s, %s, 'part', %s, %s, 'fact')
           ON CONFLICT (property_id) DO NOTHING""",
        (prop_id, key, "real" if is_num else "text",
         "Auto-registered from the legacy SQLite catalogue's specs table"),
    )
    return prop_id


def _feature_ids_for_part(sx, part_id, feature_id_map):
    """Read-only: SQLite feature_id (int) -> pg feature_id (str) for one part (by ordinal)."""
    n = 0
    for ordinal, frow in enumerate(
            sx.execute("SELECT feature_id FROM features WHERE part_id=? ORDER BY feature_id",
                       (part_id,)).fetchall(), start=1):
        feature_id_map[frow["feature_id"]] = "%s/f%d" % (part_id, ordinal)
        n += 1
    return n


def _migrate_documents_and_parts(sx, con, with_solids, only_docs=None, map_docs=None, commit=True):
    """`only_docs` (iterable of doc_id, default None = every document): write only those
    documents with their parts/features/specs. `map_docs` (default None): documents that are NOT
    written but whose feature ids are still put into the returned map (read-only lookups), because
    an interface of a selected product may reference one of their features. `commit`: False
    leaves the whole write to the caller's one transaction (the mirror: nothing half-written is
    ever visible)."""
    counts = {"documents": 0, "parts": 0, "features": 0}
    only = None if only_docs is None else set(only_docs)
    mapped = set(map_docs or ())
    # SQLite feature_id (int, global autoincrement) -> pg feature_id (str) -- GLOBAL,
    # not per-part: an interface can reference features on either of two different
    # parts, so this map must be complete before _migrate_products runs.
    feature_id_map = {}

    for doc in sx.execute("SELECT * FROM documents").fetchall():
        doc_id = doc["doc_id"]
        if only is not None and doc_id not in only:
            if doc_id in mapped:
                for part in sx.execute("SELECT part_id FROM parts WHERE doc_id=?", (doc_id,)).fetchall():
                    _feature_ids_for_part(sx, part["part_id"], feature_id_map)
            continue
        _load.write_document(
            con, doc_id, doc["path"], doc["doc_name"], doc["format"], doc["checksum_sha256"],
            doc["tool_version"], doc["object_count"], doc["part_count"], "sqlite",
            now=_sync.parse_ts(doc["ingested_at"]),   # keep SQLite's ingestion time
        )
        counts["documents"] += 1

        shapes_by_ref = {}
        is_step = (doc["format"] or "").lower() in ("step", "stp")
        if with_solids:
            if is_step and doc["path"] and os.path.isfile(doc["path"]):
                from framework.kernel import io as _kio
                model = _kio.load(doc["path"])
                shapes_by_ref = {p.ref: p.shape for p in model.parts}
            else:
                print("  skip solids for %s: not a readable STEP document (format=%s)" % (
                    doc["path"], doc["format"]))

        method_version = "sqlite:%s" % (doc["tool_version"] or "unknown")
        for part in sx.execute("SELECT rowid AS _rowid, * FROM parts WHERE doc_id=?", (doc_id,)).fetchall():
            record = json.loads(part["raw_json"])
            part_id = "%s:%s" % (doc_id, record["object"])
            shape = shapes_by_ref.get(record["object"])
            _load.write_part(
                con, doc_id, record, family=part["family"], family_why=part["family_why"],
                method_version=method_version, source="measured", shape=shape,
            )
            # SQLite's row order (rowid) is what an unranked find.py prints; keep it so the
            # Postgres side can reproduce it byte for byte
            con.execute("UPDATE part_revision SET seq = %s WHERE part_id = %s",
                        (part["_rowid"], part_id))
            counts["parts"] += 1

            counts["features"] += _feature_ids_for_part(sx, part_id, feature_id_map)

            for spec in sx.execute("SELECT * FROM specs WHERE part_id=?", (part_id,)).fetchall():
                is_num = spec["value_num"] is not None
                value = spec["value_num"] if is_num else spec["value_text"]
                if value is None:
                    continue
                prop_id = _ensure_spec_property(con, spec["key"], is_num)
                _load._put_fact(
                    con, "part", part_id, prop_id, value, SQLITE_METHOD_VERSION,
                    source=spec["source"] or "measured", datatype="real" if is_num else "text",
                )
        if commit:
            con.commit()

    return counts, feature_id_map


def _migrate_products(sx, con, feature_id_map, only_products=None, commit=True):
    counts = {"products": 0, "occurrences": 0, "interfaces": 0}
    for prod in sx.execute("SELECT * FROM products").fetchall():
        pid = prod["product_id"]
        if only_products is not None and pid not in only_products:
            continue

        bom = []
        for pp in sx.execute(
                "SELECT * FROM product_parts WHERE product_id=? ORDER BY ref_des", (pid,)).fetchall():
            placements = [json.loads(pp["placement_json"])] if pp["placement_json"] else None
            bom.append({
                "ref_des": pp["ref_des"], "part_id": pp["part_id"], "qty": pp["qty"] or 1,
                "parent_ref": pp["parent_ref"], "placements": placements, "notes": pp["notes"],
            })
        _load.write_product(
            con, pid, prod["name"], prod["revision"], prod["description"], prod["manifest_path"],
            prod["created_at"], bom, method_version=SQLITE_METHOD_VERSION,
        )
        counts["products"] += 1
        counts["occurrences"] += sum(b["qty"] for b in bom)

        rows = [dict(r) for r in sx.execute(
            "SELECT * FROM interfaces WHERE product_id=? ORDER BY interface_id", (pid,)).fetchall()]
        _load.write_interfaces(con, pid, rows, feature_id_map=feature_id_map,
                                method_version=SQLITE_METHOD_VERSION)
        counts["interfaces"] += len(rows)
        if commit:
            con.commit()

    return counts


def migrate(sqlite_path, con, with_solids=False, only_products=None, only_docs=None, commit=True):
    """Read `sqlite_path` (read-only) and write every document/part/product/interface
    into `con`'s database through `framework.catalog.pg.load`. Returns a counts dict
    (documents/parts/features/products/occurrences/interfaces) of what was WRITTEN this
    call (re-running is safe -- see module docstring -- but the counts always reflect
    the full source, not just the changed rows).

    `only_products` (list of product_id) limits the products written; `only_docs` (list of
    doc_id) limits the documents written (one product, or the documents one ingest
    produced, are mirrored without replaying the whole catalogue). The two combine: the feature
    id map is still built, read-only, for every document the selected products' parts live in,
    so their interfaces resolve even when those documents are not rewritten.

    `commit`: True (the default, the CLI) commits after each document and product;
    False writes everything in the caller's transaction (pg/sync.py: the copy changes all at once)."""
    sx = _connect_sqlite(sqlite_path)
    try:
        map_docs = None
        if only_docs is not None and only_products:
            marks = ",".join("?" * len(only_products))
            map_docs = [r[0] for r in sx.execute(
                "SELECT DISTINCT pa.doc_id FROM product_parts pp JOIN parts pa ON pa.part_id=pp.part_id "
                "WHERE pp.product_id IN (%s)" % marks, list(only_products))]
        doc_counts, feature_id_map = _migrate_documents_and_parts(
            sx, con, with_solids, only_docs=only_docs, map_docs=map_docs, commit=commit)
        prod_counts = _migrate_products(sx, con, feature_id_map, only_products=only_products, commit=commit)
    finally:
        sx.close()
    counts = dict(doc_counts)
    counts.update(prod_counts)
    return counts


def _sqlite_counts(sqlite_path):
    sx = _connect_sqlite(sqlite_path)
    try:
        return {
            "documents": sx.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "parts": sx.execute("SELECT COUNT(*) FROM parts").fetchone()[0],
            "products": sx.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "occurrences": sx.execute("SELECT COALESCE(SUM(qty),0) FROM product_parts").fetchone()[0],
            "interfaces": sx.execute("SELECT COUNT(*) FROM interfaces").fetchone()[0],
        }
    finally:
        sx.close()


_PART_SCALAR_FIELDS = ("volume_mm3", "area_mm2", "n_solids", "n_shells", "n_faces",
                        "n_edges", "n_vertexes")
_PART_VECTOR_FIELDS = ("bbox", "bbox_sorted_mm", "com", "moments", "radius_of_gyration")
_FEATURE_SCALAR_FIELDS = ("dia_mm", "radius_mm", "semi_angle_deg", "offset_mm",
                           "extent_mm", "area_mm2", "face_count")
_IFACE_SCALAR_FIELDS = ("nominal_a_mm", "nominal_b_mm", "clearance_mm", "contact_area_mm2",
                         "axis_x", "axis_y", "axis_z", "pos_x", "pos_y", "pos_z")


def verify(sqlite_path, con):
    """Every numeric SQLite parts/features/interfaces column, re-derived from
    `records.part_record`/`records.product_rows` (the SAME facts a caller reads),
    compared to its SQLite source value with EXACT equality. -> list of mismatch
    strings; empty means the migration lost or altered nothing measurable."""
    from framework.catalog.pg import records as _records

    mismatches = []
    sx = _connect_sqlite(sqlite_path)
    try:
        for row in sx.execute("SELECT part_id, raw_json FROM parts").fetchall():
            part_id = row["part_id"]
            sqlite_rec = json.loads(row["raw_json"])
            pg_rec = _records.part_record(con, part_id)
            if pg_rec is None:
                mismatches.append("%s: missing from Postgres" % part_id)
                continue
            for field in _PART_SCALAR_FIELDS:
                if sqlite_rec.get(field) != pg_rec.get(field):
                    mismatches.append("%s %s: sqlite=%s pg=%s" % (
                        part_id, field, sqlite_rec.get(field), pg_rec.get(field)))
            for field in _PART_VECTOR_FIELDS:
                sv, pv = sqlite_rec.get(field), pg_rec.get(field)
                if (sv is None) != (pv is None) or (sv is not None and list(sv) != list(pv)):
                    mismatches.append("%s %s: sqlite=%s pg=%s" % (part_id, field, sv, pv))
            for surf_key, count in (sqlite_rec.get("surfaces") or {}).items():
                pg_count = (pg_rec.get("surfaces") or {}).get(surf_key)
                if count != pg_count:
                    mismatches.append("%s surfaces.%s: sqlite=%s pg=%s" % (
                        part_id, surf_key, count, pg_count))

            s_feats = sqlite_rec.get("features") or []
            p_feats = pg_rec.get("features") or []
            if len(s_feats) != len(p_feats):
                mismatches.append("%s features count: sqlite=%d pg=%d" % (
                    part_id, len(s_feats), len(p_feats)))
                continue
            for i, (sf, pf) in enumerate(zip(s_feats, p_feats)):
                for field in _FEATURE_SCALAR_FIELDS:
                    if sf.get(field) != pf.get(field):
                        mismatches.append("%s feature[%d].%s: sqlite=%s pg=%s" % (
                            part_id, i, field, sf.get(field), pf.get(field)))
                for field in ("axis", "pos"):
                    sv, pv = sf.get(field), pf.get(field)
                    if (sv or None) != (pv or None) and list(sv or []) != list(pv or []):
                        mismatches.append("%s feature[%d].%s: sqlite=%s pg=%s" % (
                            part_id, i, field, sv, pv))

        by_product = {}
        for r in sx.execute("SELECT * FROM interfaces ORDER BY product_id, interface_id").fetchall():
            by_product.setdefault(r["product_id"], []).append(r)
        for pid, rows in by_product.items():
            _bom, pg_ifaces = _records.product_rows(con, pid)
            if len(pg_ifaces) != len(rows):
                mismatches.append("%s interfaces count: sqlite=%d pg=%d" % (pid, len(rows), len(pg_ifaces)))
                continue
            for i, (sr, pr) in enumerate(zip(rows, pg_ifaces)):
                for field in _IFACE_SCALAR_FIELDS:
                    if sr[field] != pr.get(field):
                        mismatches.append("%s interface[%d].%s: sqlite=%s pg=%s" % (
                            pid, i, field, sr[field], pr.get(field)))
    finally:
        sx.close()
    return mismatches


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite", default=DEFAULT_SQLITE, help="path to a catalog.db (default: %s)" %
                     DEFAULT_SQLITE)
    ap.add_argument("--db", default=None,
                     help="Postgres database to migrate into (default: the BC_PG_DSN database)")
    ap.add_argument("--with-solids", action="store_true",
                     help="reload each STEP document's shapes for per-solid provenance")
    ap.add_argument("--verify", action="store_true",
                     help="compare every numeric SQLite column against its fact after migrating")
    args = ap.parse_args(argv)

    _db.refuse_main_db_in_worktree(args.db or _db.dbname())
    con = _db.connect(dbname=args.db, autocommit=False)
    try:
        _db.apply_schema(con)
        from framework.catalog.pg import seed as _seed
        _seed.seed(con)
        con.commit()

        counts = migrate(args.sqlite, con, with_solids=args.with_solids)
        sqlite_counts = _sqlite_counts(args.sqlite)

        mismatch = False
        print("%-12s %10s %10s" % ("", "sqlite", "postgres"))
        for key in ("documents", "parts", "products", "occurrences", "interfaces"):
            s, p = sqlite_counts[key], counts.get(key)
            same = s == p
            mismatch = mismatch or not same
            print("%-12s %10s %10s%s" % (key, s, p, "" if same else "  <-- MISMATCH"))

        if args.verify:
            mismatches = verify(args.sqlite, con)
            print("verify: %d mismatches" % len(mismatches))
            for m in mismatches[:100]:
                print("  " + m)
            mismatch = mismatch or bool(mismatches)
    finally:
        con.close()
    return 1 if mismatch else 0


if __name__ == "__main__":
    sys.exit(main())
