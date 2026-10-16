"""Seed the Postgres catalogue dictionary (property/class/class_property/synonym) into Postgres, and
prove every key the kernel/SQLite catalogue writes today maps to exactly one property_id
(the design is in dictionary.json).

CLI: python -m framework.catalog.pg.seed [--db NAME] [--check-sqlite PATH]
"""
import argparse
import json
import os
import sqlite3

from framework.catalog.pg import db

_PG_DIR = os.path.dirname(os.path.abspath(__file__))
DICTIONARY_PATH = os.path.join(_PG_DIR, "dictionary.json")
# framework/catalog/pg/seed.py -> framework/catalog/pg -> framework/catalog -> framework -> root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_PG_DIR)))
FAMILIES_PATH = os.path.join(PROJECT_ROOT, "framework", "catalog", "families.json")

_dictionary_cache = None


def _dictionary():
    global _dictionary_cache
    if _dictionary_cache is None:
        with open(DICTIONARY_PATH, "r", encoding="utf-8") as fh:
            _dictionary_cache = json.load(fh)
    return _dictionary_cache


def record_map():
    """record_map()["part"|"feature"|"interface"|"product"|"document"|"occurrence"]
    -> {record_key: property_id}, loaded from dictionary.json."""
    return _dictionary()["record_map"]


def property_ids():
    """Every property_id declared in dictionary.json."""
    return {p["property_id"] for p in _dictionary()["properties"]}


def families():
    """Distinct family names from framework/catalog/families.json, in rule order, read with
    json.load (never hand-typed -- a literal '\\b' in a hand-edited JSON file is a backspace,
    not a regex word boundary; see that file's own header)."""
    with open(FAMILIES_PATH, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    seen = []
    for rule in doc["rules"]:
        name = rule["family"]
        if name not in seen:
            seen.append(name)
    return seen


def class_ids():
    """One class per family, plus 'unclassified' (families.json already lists
    it as its final catch-all rule, but this is defensive if that ever changes)."""
    ids = families()
    if "unclassified" not in ids:
        ids.append("unclassified")
    return ids


def unmapped_keys(records):
    """records: iterable of part records as stored in parts.raw_json (dicts with a 'features'
    list of feature records). Returns sorted 'part.<key>' / 'feature.<key>' for every key
    missing from dictionary.json's record_map -- empty means every key maps to a property."""
    rmap = record_map()
    part_map = rmap.get("part", {})
    feature_map = rmap.get("feature", {})
    missing = set()
    for rec in records:
        for key in rec:
            if key == "features":
                continue
            if key not in part_map:
                missing.add(f"part.{key}")
        for feat in rec.get("features") or []:
            for key in feat:
                if key not in feature_map:
                    missing.add(f"feature.{key}")
    return sorted(missing)


def seed(con):
    """Upsert every dictionary.json row into `con` (ON CONFLICT DO UPDATE -- safe to call
    again). Classes come from families.json at seed time, not from dictionary.json, so the
    family list never drifts between the two files. Returns row counts for the four tables."""
    d = _dictionary()
    ids = class_ids()

    with con.cursor() as cur:
        for p in d["properties"]:
            cur.execute(
                """
                INSERT INTO property (property_id, name, applies_to, datatype, quantity_kind,
                                       unit, definition, irdi, stored_as, derived_from)
                VALUES (%(property_id)s, %(name)s, %(applies_to)s, %(datatype)s, %(quantity_kind)s,
                        %(unit)s, %(definition)s, %(irdi)s, %(stored_as)s, %(derived_from)s)
                ON CONFLICT (property_id) DO UPDATE SET
                    name = EXCLUDED.name, applies_to = EXCLUDED.applies_to,
                    datatype = EXCLUDED.datatype, quantity_kind = EXCLUDED.quantity_kind,
                    unit = EXCLUDED.unit, definition = EXCLUDED.definition, irdi = EXCLUDED.irdi,
                    stored_as = EXCLUDED.stored_as, derived_from = EXCLUDED.derived_from
                """,
                {
                    "property_id": p["property_id"],
                    "name": p["name"],
                    "applies_to": p["applies_to"],
                    "datatype": p["datatype"],
                    "quantity_kind": p.get("quantity_kind"),
                    "unit": p.get("unit"),
                    "definition": p["definition"],
                    "irdi": p.get("irdi"),
                    "stored_as": p.get("stored_as", "fact"),
                    "derived_from": p.get("derived_from"),
                },
            )

        for cid in ids:
            cur.execute(
                """
                INSERT INTO class (class_id, parent_id, name, irdi) VALUES (%s, NULL, %s, NULL)
                ON CONFLICT (class_id) DO UPDATE SET name = EXCLUDED.name
                """,
                (cid, cid),
            )

        for cp in d.get("class_properties", []):
            targets = ids if cp["class_id"] == "*" else [cp["class_id"]]
            for cid in targets:
                cur.execute(
                    """
                    INSERT INTO class_property (class_id, property_id, required)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (class_id, property_id) DO UPDATE SET required = EXCLUDED.required
                    """,
                    (cid, cp["property_id"], cp.get("required", False)),
                )

        for s in d.get("synonyms", []):
            cur.execute(
                """
                INSERT INTO synonym (term, property_id, role) VALUES (%s, %s, %s)
                ON CONFLICT (term, property_id) DO UPDATE SET role = EXCLUDED.role
                """,
                (s["term"], s["property_id"], s.get("role")),
            )

    if not con.autocommit:
        con.commit()

    counts = {}
    with con.cursor() as cur:
        for table in ("property", "class", "class_property", "synonym"):
            cur.execute(f"SELECT count(*) FROM {table}")
            counts[table] = cur.fetchone()[0]
    return counts


def _load_sqlite_parts(path):
    cx = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = cx.execute("SELECT raw_json FROM parts").fetchall()
    finally:
        cx.close()
    return [json.loads(r[0]) for r in rows]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="database to seed (default: the BC_PG_DSN database)")
    ap.add_argument("--check-sqlite", default=None,
                     help="path to a catalog.db; prints unmapped record keys, exit 1 if any")
    args = ap.parse_args(argv)

    db.refuse_main_db_in_worktree(args.db or db.dbname())
    con = db.connect(dbname=args.db, autocommit=True)
    try:
        db.apply_schema(con)
        counts = seed(con)
    finally:
        con.close()
    print(
        f"seeded {args.db or db.dbname()}: property={counts['property']} class={counts['class']} "
        f"class_property={counts['class_property']} synonym={counts['synonym']}"
    )

    if args.check_sqlite:
        records = _load_sqlite_parts(args.check_sqlite)
        missing = unmapped_keys(records)
        print(f"parts checked: {len(records)}")
        print(f"unmapped keys: {missing if missing else '[]'}")
        if missing:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
