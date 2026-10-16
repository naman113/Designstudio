"""The ONE writer path into the Postgres catalogue schema.

Every entity this package writes -- documents, parts (with their features and,
optionally, per-solid provenance), products (with their occurrences) and interfaces --
goes through the functions below, whether the caller is a live kernel measurement
(`framework.kernel.measure`/`framework.kernel.features`) or `migrate_sqlite.py`
replaying the existing SQLite catalogue. `pg/dictionary.json`'s `record_map`
decides, for every record key, whether it lands in an entity column (`stored_as`
"column:...") or becomes a fact ("fact"); this module never hard-codes that split a
second time.

Every function here takes an already-open `con` and does NOT commit -- the caller
owns the transaction (`con.commit()` after one document/product, per the fact
history model: a half-written document must never appear "current").

History: `_put_fact` compares a new value against the current fact
(`valid_to IS NULL`, the unique partial index `ux_fact_current`) for the same
(entity_kind, entity_id, property_id). Same value AND same method_version -> no-op.
Otherwise the old row is closed (`valid_to = now()`) and the new one inserted with
`valid_from = now()`. Callers that finish writing a whole entity's fields call
`_close_stale_facts` once, for properties that used to be current for that entity but
are no longer present in the new record are closed too.

`write_part`, `write_product` and `write_interfaces` apply that same rule through
`_FactBatch` -- one read of the entities' current facts, the closes in one batch, the new
rows in one COPY -- instead of two statements per fact, so a large design (tens of
thousands of facts) is not two round trips per fact.
"""
import hashlib
import json
import os
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from framework.common import shape_signature as _signature   # geom_hash, moments_vec; no kernel at import

_PG_DIR = os.path.dirname(os.path.abspath(__file__))
DICTIONARY_PATH = os.path.join(_PG_DIR, "dictionary.json")
# framework/catalog/pg/load.py -> framework/catalog/pg -> framework/catalog -> framework -> root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_PG_DIR)))
KERNEL_DIR = os.path.join(PROJECT_ROOT, "framework", "kernel")

_dictionary_cache = None


def _dictionary():
    global _dictionary_cache
    if _dictionary_cache is None:
        with open(DICTIONARY_PATH, "r", encoding="utf-8") as fh:
            _dictionary_cache = json.load(fh)
    return _dictionary_cache


def record_map():
    return _dictionary()["record_map"]


def _properties():
    return {p["property_id"]: p for p in _dictionary()["properties"]}


def _kernel_method_version():
    """kernel@<sha256(measure.py bytes + features.py bytes)[:10]> -- changes
    whenever the extraction logic changes, so a re-ingest after a kernel code change
    is recognised as a new method_version and keeps the old facts as history."""
    h = hashlib.sha256()
    for name in ("measure.py", "features.py"):
        with open(os.path.join(KERNEL_DIR, name), "rb") as fh:
            h.update(fh.read())
    return "kernel@" + h.hexdigest()[:10]


METHOD_VERSION = _kernel_method_version()

# part.surfaces / feature-count derived scalars (mirrors framework/catalog/ingest.py's
# own SURF_COLS -- kept independent, not imported, since that module is not owned by
# this plan and is a scripts-folder module on sys.path, not a package).
_SURF_DERIVED = {
    "Plane": "part.n_plane", "Cylinder": "part.n_cylinder", "Cone": "part.n_cone",
    "Sphere": "part.n_sphere", "Toroid": "part.n_torus", "BSplineSurface": "part.n_bspline",
}
_SHAPE_RATIO_DERIVED = {
    "aspect_1": "part.aspect_1", "aspect_2": "part.aspect_2",
    "fill": "part.fill", "compactness": "part.compactness",
}


def _derived_facts(prop_id, value):
    """Scalar facts written ALONGSIDE a composite (vector/map) property
    (derived scalar properties are written alongside for range indexes; the
    dictionary records derived_from). -> {derived_property_id: scalar_value}."""
    if value is None:
        return {}
    if prop_id == "part.envelope" and len(value) >= 3:
        return {"part.envelope_a": value[0], "part.envelope_b": value[1], "part.envelope_c": value[2]}
    if prop_id == "part.bbox" and len(value) >= 6:
        return {"part.bbox_xmin": value[0], "part.bbox_xmax": value[1], "part.bbox_ymin": value[2],
                "part.bbox_ymax": value[3], "part.bbox_zmin": value[4], "part.bbox_zmax": value[5]}
    if prop_id == "part.moments" and len(value) >= 3:
        return {"part.moment_1": value[0], "part.moment_2": value[1], "part.moment_3": value[2]}
    if prop_id == "part.radius_of_gyration" and len(value) >= 3:
        return {"part.rgyr_1": value[0], "part.rgyr_2": value[1], "part.rgyr_3": value[2]}
    if prop_id == "part.surfaces":
        out = {dprop: value.get(surf_name, 0) for surf_name, dprop in _SURF_DERIVED.items()}
        out["part.n_other_surf"] = sum(v for k, v in value.items() if k not in _SURF_DERIVED)
        return out
    if prop_id == "part.shape_ratios":
        return {dprop: value[key] for key, dprop in _SHAPE_RATIO_DERIVED.items()
                if value.get(key) is not None}
    return {}


def _value_columns(datatype, value):
    """-> (columns dict for the fact row's value_*, comparison key) for `value`
    typed as `datatype`. comparison key is None when there is nothing to store."""
    empty = {"value_num": None, "value_text": None, "value_vec": None, "value_json": None}
    if value is None:
        return empty, None
    if datatype in ("real", "int"):
        v = float(value)
        return dict(empty, value_num=v), ("num", v)
    if datatype == "vector":
        v = [None if x is None else float(x) for x in value]
        return dict(empty, value_vec=v), ("vec", tuple(v))
    if datatype == "map":
        return dict(empty, value_json=Jsonb(value)), ("json", json.dumps(value, sort_keys=True))
    if datatype == "bool":
        v = "true" if value else "false"
        return dict(empty, value_text=v), ("text", v)
    # text / enum / fallback
    v = value if isinstance(value, str) else str(value)
    return dict(empty, value_text=v), ("text", v)


def _num_close(a, b, tol=1e-9):
    return abs(a - b) <= max(tol, tol * max(abs(a), abs(b)))


def _same_value(kind, val, cur_num, cur_text, cur_vec, cur_json):
    if kind == "num":
        return cur_num is not None and _num_close(cur_num, val)
    if kind == "vec":
        if cur_vec is None or len(cur_vec) != len(val):
            return False
        for a, b in zip(cur_vec, val):
            if a is None or b is None:
                if a is not b:
                    return False
                continue
            if not _num_close(a, b):
                return False
        return True
    if kind == "json":
        return cur_json is not None and json.dumps(cur_json, sort_keys=True) == val
    if kind == "text":
        return cur_text == val
    return False


def _put_fact(con, kind, entity_id, prop_id, value, method_version,
              source="measured", confidence=1, measure_method=None, uncertainty=None, now=None,
              datatype=None):
    """Write one current fact for (kind, entity_id, prop_id), following the history
    rule. No-op (returns False) when `value` is None, or when a current fact with the
    identical value AND method_version already exists. Otherwise closes the current
    row (if any) and inserts the new one; returns True.

    `datatype` overrides the dictionary.json lookup -- for a property migrate_sqlite.py
    auto-registers straight into the DB (specs.<key> -> spec.<key>, not a dictionary.json
    entry: that file is not owned by this plan) `_properties()` has never heard of."""
    if datatype is None:
        prop = _properties().get(prop_id)
        if prop is None:
            raise ValueError("unknown property_id %r (not in dictionary.json)" % prop_id)
        datatype = prop["datatype"]
    cols, key = _value_columns(datatype, value)
    if key is None:
        return False
    kind_key, val = key
    row = con.execute(
        """SELECT value_num, value_text, value_vec, value_json, method_version
           FROM fact WHERE entity_kind=%s AND entity_id=%s AND property_id=%s AND valid_to IS NULL""",
        (kind, entity_id, prop_id),
    ).fetchone()
    if row is not None:
        cur_num, cur_text, cur_vec, cur_json, cur_mv = row
        if cur_mv == method_version and _same_value(kind_key, val, cur_num, cur_text, cur_vec, cur_json):
            return False
        con.execute(
            """UPDATE fact SET valid_to = COALESCE(%s, now())
               WHERE entity_kind=%s AND entity_id=%s AND property_id=%s AND valid_to IS NULL""",
            (now, kind, entity_id, prop_id),
        )
    con.execute(
        """INSERT INTO fact (entity_kind, entity_id, property_id, value_num, value_text, value_vec,
                              value_json, source, confidence, measure_method, uncertainty,
                              method_version, valid_from)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s, now()))""",
        (kind, entity_id, prop_id, cols["value_num"], cols["value_text"], cols["value_vec"],
         cols["value_json"], source, confidence, measure_method, uncertainty, method_version, now),
    )
    return True


DERIVED_SOURCE = "derived"   # the part record's own facts (pg/partrecord.py), not the measured record's


def _close_stale_facts(con, kind, entity_id, keep_props, now=None):
    """Close every CURRENT fact of (kind, entity_id) whose property_id is not in
    `keep_props` (facts that were current for this entity but are absent from
    the new record are closed too). Facts with source 'derived' (mass, exact size,
    connection features, family parameters) are not the measured record's: the part record
    (pg/partrecord.py) closes them itself when its inputs change."""
    keep = list(keep_props)
    if not keep:
        con.execute(
            """UPDATE fact SET valid_to = COALESCE(%s, now())
               WHERE entity_kind=%s AND entity_id=%s AND valid_to IS NULL AND source <> %s""",
            (now, kind, entity_id, DERIVED_SOURCE),
        )
        return
    con.execute(
        """UPDATE fact SET valid_to = COALESCE(%s, now())
           WHERE entity_kind=%s AND entity_id=%s AND valid_to IS NULL
             AND NOT (property_id = ANY(%s)) AND source <> %s""",
        (now, kind, entity_id, keep, DERIVED_SOURCE),
    )


_FACT_COPY_COLS = ("entity_kind", "entity_id", "property_id", "value_num", "value_text", "value_vec",
                   "value_json", "source", "confidence", "measure_method", "uncertainty",
                   "method_version", "valid_from", "valid_to")


class _FactBatch:
    """The fact history rule for many facts at once. `put` and `close_stale` record,
    in order, what `_put_fact` and `_close_stale_facts` would do one statement at a time;
    `flush` reads the current facts of every entity named (one query per entity kind), decides
    each put exactly as `_put_fact` does (same value AND same method_version -> nothing; else the
    current row is closed and a new one inserted), then sends the closes in one batch and the
    new rows in one COPY. A fact put twice in one batch keeps the sequential result (the first
    new row is inserted already closed). Like every writer here it never commits."""

    def __init__(self, con, now=None):
        self.con = con
        self.now = now
        self.ops = []
        self.ids = {}                      # entity_kind -> {entity_id: None}, in first-seen order

    def put(self, kind, entity_id, prop_id, value, method_version, source="measured", confidence=1,
            measure_method=None, uncertainty=None, datatype=None):
        """As `_put_fact`: False (nothing recorded) when `value` is None."""
        if datatype is None:
            prop = _properties().get(prop_id)
            if prop is None:
                raise ValueError("unknown property_id %r (not in dictionary.json)" % prop_id)
            datatype = prop["datatype"]
        cols, key = _value_columns(datatype, value)
        if key is None:
            return False
        self.ops.append((0, kind, entity_id, prop_id, cols, key, method_version, source, confidence,
                         measure_method, uncertainty))
        self.ids.setdefault(kind, {})[entity_id] = None
        return True

    def close_stale(self, kind, entity_id, keep_props):
        """As `_close_stale_facts`, at this point of the sequence."""
        self.ops.append((1, kind, entity_id, frozenset(keep_props)))
        self.ids.setdefault(kind, {})[entity_id] = None

    def flush(self):
        if not self.ops:
            return
        con = self.con
        now = self.now if self.now is not None else con.execute("SELECT now()").fetchone()[0]
        cur = {}                           # (kind, entity_id) -> {property_id: ("db", values, mv) | ("new", row index)}
        for kind, ids in self.ids.items():
            for eid, prop, num, text, vec, js, mv in con.execute(
                    """SELECT entity_id, property_id, value_num, value_text, value_vec, value_json, method_version
                       FROM fact WHERE entity_kind = %s AND entity_id = ANY(%s) AND valid_to IS NULL
                         AND source <> %s""",
                    (kind, list(ids), DERIVED_SOURCE)).fetchall():
                cur.setdefault((kind, eid), {})[prop] = ("db", (num, text, vec, js), mv)
        closes, rows = [], []

        def retire(kind, eid, prop, entry):
            if entry[0] == "db":
                closes.append((now, kind, eid, prop))
            else:
                rows[entry[1]][13] = now   # put earlier in this batch: inserted already closed

        for op in self.ops:
            if op[0] == 0:
                _, kind, eid, prop, cols, key, mv, source, confidence, mmethod, unc = op
                props = cur.setdefault((kind, eid), {})
                entry = props.get(prop)
                if entry is not None:
                    if entry[0] == "db":
                        (num, text, vec, js), emv = entry[1], entry[2]
                    else:
                        r = rows[entry[1]]
                        num, text, vec, js, emv = r[3], r[4], r[5], (r[6].obj if r[6] is not None else None), r[11]
                    if emv == mv and _same_value(key[0], key[1], num, text, vec, js):
                        continue
                    retire(kind, eid, prop, entry)
                rows.append([kind, eid, prop, cols["value_num"], cols["value_text"], cols["value_vec"],
                             cols["value_json"], source, confidence, mmethod, unc, mv, now, None])
                props[prop] = ("new", len(rows) - 1)
            else:
                _, kind, eid, keep = op
                props = cur.get((kind, eid)) or {}
                for prop in [p for p in props if p not in keep]:
                    retire(kind, eid, prop, props.pop(prop))
        with con.cursor() as c:
            if closes:                     # before the inserts: one current row per (entity, property)
                c.executemany(
                    """UPDATE fact SET valid_to = %s
                       WHERE entity_kind=%s AND entity_id=%s AND property_id=%s AND valid_to IS NULL""", closes)
            if rows:
                with c.copy("COPY fact (%s) FROM STDIN" % ", ".join(_FACT_COPY_COLS)) as cp:
                    for r in rows:
                        cp.write_row(r)
        self.ops, self.ids = [], {}


def _ensure_class(con, class_id):
    con.execute(
        "INSERT INTO class (class_id, parent_id, name, irdi) VALUES (%s, NULL, %s, NULL) "
        "ON CONFLICT (class_id) DO NOTHING",
        (class_id, class_id),
    )


def record_to_rows(item_map, record, properties, extra_by_prop=None):
    """-> [(prop_id, value, extra_kwargs), ...] for every key of `record` (a part record or
    one of its `features[i]` dicts) mapped by `item_map` (`record_map()["part"|"feature"]`),
    skipping column-stored properties (already written to an entity column elsewhere) and
    unmapped keys, INCLUDING the derived scalar facts a composite (vector/map) property
    carries alongside it -- interleaved right after their own source property, the
    SAME order `write_part`'s own per-record loop always emitted them in. This is the ONE
    place pg/load.py decides "which record key becomes which fact, with what extra
    provenance (measure_method/uncertainty)" -- both `write_part` (history-aware, one row at
    a time via `_put_fact`) and `bulk_writer` (history-free COPY, empty
    targets only) call this same function so there is still one translation, not two that
    could drift."""
    extra_by_prop = extra_by_prop or {}
    out = []
    for key, value in record.items():
        if key == "features":
            continue
        prop_id = item_map.get(key)
        if prop_id is None:
            continue  # unmapped -- seed.py's coverage check keeps this from happening
        prop = properties[prop_id]
        if prop["stored_as"].startswith("column:"):
            continue  # already written to an entity column by the caller
        out.append((prop_id, value, extra_by_prop.get(prop_id, {})))
        for dprop, dval in _derived_facts(prop_id, value).items():
            out.append((dprop, dval, {}))
    return out


def _write_signature(con, part_id, method, vec):
    """One signature(part_id, method, vec) row, vec cast from a literal
    '[...]' array through Postgres (no pgvector-python adapter installed -- text cast is
    the whole interface, framework.catalog.pg.signatures.backfill uses the identical
    pattern for pre-existing parts)."""
    literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
    con.execute(
        "INSERT INTO signature (part_id, method, vec) VALUES (%s, %s, %s::vector) "
        "ON CONFLICT (part_id, method) DO UPDATE SET vec = EXCLUDED.vec",
        (part_id, method, literal),
    )


def write_document(con, doc_id, path, doc_name, format, sha256, tool_version,
                    object_count, part_count, source_system, now=None):
    con.execute(
        """INSERT INTO document (doc_id, path, doc_name, format, sha256, source_system,
                                  tool_version, object_count, part_count, ingested_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s, now()))
           ON CONFLICT (doc_id) DO UPDATE SET
               path=EXCLUDED.path, doc_name=EXCLUDED.doc_name, format=EXCLUDED.format,
               sha256=EXCLUDED.sha256, source_system=EXCLUDED.source_system,
               tool_version=EXCLUDED.tool_version, object_count=EXCLUDED.object_count,
               part_count=EXCLUDED.part_count, ingested_at=EXCLUDED.ingested_at""",
        (doc_id, path, doc_name, format, sha256, source_system, tool_version,
         object_count, part_count, now),
    )


def _write_solids(con, part_id, shape, method_version, source, now=None):
    """Per-solid provenance: one part_solid row + solid.* facts per solid in
    `shape`, from `framework.kernel.measure.mass_props(shape)["solids"]` -- the SAME
    per-solid breakdown measure() itself sums into volume_mm3/area_mm2, so
    sum(solid.volume) always equals the part's own volume fact within float precision."""
    from framework.kernel import measure as _kmeasure

    mp = _kmeasure.mass_props(shape)
    solids = mp["solids"]
    for i, s in enumerate(solids):
        con.execute(
            "INSERT INTO part_solid (part_id, solid_index) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (part_id, i),
        )
        entity_id = "%s#s%d" % (part_id, i)
        _put_fact(con, "solid", entity_id, "solid.volume", s["volume_val"], method_version,
                  source=source, measure_method=s["volume_method"], uncertainty=s["volume_unc"], now=now)
        _put_fact(con, "solid", entity_id, "solid.volume_method", s["volume_method"], method_version,
                  source=source, now=now)
        _put_fact(con, "solid", entity_id, "solid.volume_uncertainty", s["volume_unc"], method_version,
                  source=source, now=now)
        _put_fact(con, "solid", entity_id, "solid.area", s["area_val"], method_version,
                  source=source, measure_method=s["area_method"], uncertainty=s["area_unc"], now=now)
        _put_fact(con, "solid", entity_id, "solid.area_method", s["area_method"], method_version,
                  source=source, now=now)
        _put_fact(con, "solid", entity_id, "solid.area_uncertainty", s["area_unc"], method_version,
                  source=source, now=now)

    existing = {r[0] for r in con.execute(
        "SELECT solid_index FROM part_solid WHERE part_id=%s", (part_id,)).fetchall()}
    for idx in existing - set(range(len(solids))):
        entity_id = "%s#s%d" % (part_id, idx)
        _close_stale_facts(con, "solid", entity_id, set(), now=now)
        con.execute("DELETE FROM part_solid WHERE part_id=%s AND solid_index=%s", (part_id, idx))


def write_part(con, doc_id, record, family=None, family_why=None, method_version=None,
               source="measured", shape=None, now=None):
    """One kernel/SQLite part record -> part_master + part_revision + facts + feature
    entities (+ per-solid provenance when `shape` is given). `record` is exactly
    `framework.kernel.measure.measure()`'s own dict (or an SQLite `parts.raw_json`
    decode, migrate_sqlite's case) -- every key not stored as a column becomes a fact
    via `record_map()["part"]`, plus derived scalar facts for the composite (vector/
    map) properties."""
    if method_version is None:
        method_version = METHOD_VERSION
    if now is None:
        now = datetime.now(timezone.utc)

    part_id = "%s:%s" % (doc_id, record["object"])
    class_id = family or "unclassified"
    _ensure_class(con, class_id)
    con.execute(
        """INSERT INTO part_master (part_no, class_id, name) VALUES (%s,%s,%s)
           ON CONFLICT (part_no) DO UPDATE SET class_id=EXCLUDED.class_id, name=EXCLUDED.name""",
        (part_id, class_id, record.get("label")),
    )
    # geom_hash: computed at write time from the record's own invariant
    # numbers (volume/area/moments/rgyr/topology/surfaces/feature multiset) -- no shape
    # needed for this one; a new part always gets it, regardless of whether `shape` (below)
    # happens to be available this call.
    geom_hash = _signature.geom_hash(record)
    con.execute(
        """INSERT INTO part_revision (part_id, part_no, revision, doc_id, object_ref, label,
                                       type_id, units, geom_hash)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (part_id) DO UPDATE SET
               part_no=EXCLUDED.part_no, revision=EXCLUDED.revision, doc_id=EXCLUDED.doc_id,
               object_ref=EXCLUDED.object_ref, label=EXCLUDED.label, type_id=EXCLUDED.type_id,
               units=EXCLUDED.units, geom_hash=EXCLUDED.geom_hash""",
        (part_id, part_id, "A", doc_id, record.get("object"), record.get("label"),
         record.get("type_id"), record.get("units"), geom_hash),
    )

    pmap = record_map()["part"]
    properties = _properties()
    extra_by_prop = {
        "part.volume": {"measure_method": record.get("volume_method"),
                         "uncertainty": record.get("volume_uncertainty_mm3")},
        "part.area": {"measure_method": record.get("area_method"),
                       "uncertainty": record.get("area_uncertainty_mm2")},
    }
    batch = _FactBatch(con, now)
    seen_props = set()
    for prop_id, value, extra in record_to_rows(pmap, record, properties, extra_by_prop):
        batch.put("part", part_id, prop_id, value, method_version, source=source, **extra)
        if value is not None:
            seen_props.add(prop_id)
    # family/family_why are NOT keys of the kernel record (measure() never produces
    # them; classify() derives them separately) -- part.family is the part_master.
    # class_id column, already set above via `class_id`; part.family_why is a fact.
    if family_why is not None:
        batch.put("part", part_id, "part.family_why", family_why, method_version, source=source)
        seen_props.add("part.family_why")
    batch.close_stale("part", part_id, seen_props)

    # Features (feature_id = "<part_id>/f<ordinal>", ordinal = 1-based position
    # in record["features"], the SAME order SQLite inserted them in).
    fmap = record_map()["feature"]
    seen_feature_ids = set()
    feature_rows = []
    for i, feat in enumerate(record.get("features") or []):
        ordinal = i + 1
        feature_id = "%s/f%d" % (part_id, ordinal)
        seen_feature_ids.add(feature_id)
        feature_rows.append((feature_id, part_id, ordinal, feat.get("kind"), feat.get("role")))
        seen_fprops = set()
        for prop_id, value, extra in record_to_rows(fmap, feat, properties):
            batch.put("feature", feature_id, prop_id, value, method_version, source=source, **extra)
            if value is not None:
                seen_fprops.add(prop_id)
        batch.close_stale("feature", feature_id, seen_fprops)
    if feature_rows:
        with con.cursor() as cur:
            cur.executemany(
                """INSERT INTO feature (feature_id, part_id, ordinal, kind, role)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (feature_id) DO UPDATE SET
                       ordinal=EXCLUDED.ordinal, kind=EXCLUDED.kind, role=EXCLUDED.role""",
                feature_rows)

    existing_features = {r[0] for r in con.execute(
        "SELECT feature_id FROM feature WHERE part_id=%s", (part_id,)).fetchall()}
    gone_features = sorted(existing_features - seen_feature_ids)
    for fid in gone_features:
        batch.close_stale("feature", fid, set())
    batch.flush()
    if gone_features:
        con.execute("DELETE FROM feature WHERE feature_id = ANY(%s)", (gone_features,))

    if shape is not None:
        _write_solids(con, part_id, shape, method_version, source, now=now)
        # moments_v1 is derivable from the record alone, but written here (not only via
        # framework.catalog.pg.signatures.backfill) whenever the caller already has the
        # shape in hand -- d2_v1 needs the actual mesh, so both go together.
        _write_signature(con, part_id, "moments_v1", _signature.moments_vec(record))
        from framework.kernel.signature import d2_histogram   # the migration's own path (--with-solids); never in the services
        _write_signature(con, part_id, "d2_v1", d2_histogram(shape))

    return part_id


def _placement_for(placements, instance):
    """occurrence.placement for 1-based `instance`: the per-instance entry when
    `placements` has one, else the single shared placement when there is exactly one
    (SQLite's own placement_json, applied to every instance -- migrate_sqlite's case),
    else None."""
    if not placements:
        return None
    if instance - 1 < len(placements):
        return placements[instance - 1]
    if len(placements) == 1:
        return placements[0]
    return None


def write_product(con, product_id, name, revision, description, manifest_path, created_at,
                   bom, method_version=None, now=None):
    """`bom`: [{ref_des, part_id, qty, parent_ref, placements: [16-float lists] or None,
    notes}] -> product row + one occurrence per instance. Stale occurrences of
    this product (a ref_des dropped from the BOM, or qty reduced) are removed."""
    if method_version is None:
        method_version = METHOD_VERSION
    if now is None:
        now = datetime.now(timezone.utc)

    con.execute(
        """INSERT INTO product (product_id, name, revision, description, manifest_path, created_at)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (product_id) DO UPDATE SET
               name=EXCLUDED.name, revision=EXCLUDED.revision, description=EXCLUDED.description,
               manifest_path=EXCLUDED.manifest_path, created_at=EXCLUDED.created_at""",
        (product_id, name, revision, description, manifest_path, created_at),
    )

    seen_occ = set()
    occ_rows = []
    batch = _FactBatch(con, now)
    for row in bom:
        ref_des = row["ref_des"]
        qty = row.get("qty") or 1
        placements = row.get("placements")
        for i in range(1, qty + 1):
            occ_id = "%s/%s#%d" % (product_id, ref_des, i)
            seen_occ.add(occ_id)
            placement = _placement_for(placements, i)
            occ_rows.append((occ_id, product_id, ref_des, i, row.get("part_id"), row.get("parent_ref"),
                             placement, row.get("notes")))
        batch.put("occurrence", "%s/%s#1" % (product_id, ref_des), "occurrence.qty", qty,
                  method_version, source="measured")
    if occ_rows:
        with con.cursor() as cur:
            cur.executemany(
                """INSERT INTO occurrence (occ_id, product_id, ref_des, instance, part_id,
                                            parent_ref, placement, notes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (occ_id) DO UPDATE SET
                       part_id=EXCLUDED.part_id, parent_ref=EXCLUDED.parent_ref,
                       placement=EXCLUDED.placement, notes=EXCLUDED.notes""",
                occ_rows)

    existing = {r[0] for r in con.execute(
        "SELECT occ_id FROM occurrence WHERE product_id=%s", (product_id,)).fetchall()}
    gone = sorted(existing - seen_occ)
    for occ_id in gone:
        batch.close_stale("occurrence", occ_id, set())
    batch.flush()
    if gone:
        con.execute("DELETE FROM occurrence WHERE occ_id = ANY(%s)", (gone,))


def write_interfaces(con, product_id, rows, feature_id_map=None, method_version=None, now=None):
    """`rows`: SQLite-shaped interface rows (kind, ref_a, ref_b, feature_a, feature_b,
    nominal_a_mm, nominal_b_mm, clearance_mm, fit_class, contact_area_mm2, axis_x/y/z,
    pos_x/y/z), in SQLite interface_id order (its own ordinal). `feature_id_map`:
    {sqlite feature_id (int): pg feature_id (str)} for feature_a/feature_b -- the
    caller (migrate_sqlite) builds it per part; kernel callers pass the pg feature ids
    already (a no-op map)."""
    if method_version is None:
        method_version = METHOD_VERSION
    if now is None:
        now = datetime.now(timezone.utc)
    feature_id_map = feature_id_map or {}
    imap = record_map()["interface"]

    seen = set()
    iface_rows = []
    batch = _FactBatch(con, now)
    for ordinal, row in enumerate(rows, start=1):
        interface_id = "%s/i%d" % (product_id, ordinal)
        seen.add(interface_id)
        ref_a, ref_b = row.get("ref_a"), row.get("ref_b")
        occ_a = "%s/%s#1" % (product_id, ref_a) if ref_a else None
        occ_b = "%s/%s#1" % (product_id, ref_b) if ref_b else None
        feature_a = feature_id_map.get(row.get("feature_a"), row.get("feature_a"))
        feature_b = feature_id_map.get(row.get("feature_b"), row.get("feature_b"))
        iface_rows.append((interface_id, product_id, ordinal, row.get("kind"), occ_a, occ_b,
                           ref_a, ref_b, feature_a, feature_b))
        seen_props = set()
        for key in ("nominal_a_mm", "nominal_b_mm", "clearance_mm", "fit_class", "contact_area_mm2"):
            value = row.get(key)
            if value is None:
                continue
            prop_id = imap[key]
            batch.put("interface", interface_id, prop_id, value, method_version)
            seen_props.add(prop_id)
        axis = [row.get("axis_x"), row.get("axis_y"), row.get("axis_z")]
        if any(a is not None for a in axis):
            batch.put("interface", interface_id, "iface.axis", axis, method_version)
            seen_props.add("iface.axis")
        pos = [row.get("pos_x"), row.get("pos_y"), row.get("pos_z")]
        if any(p is not None for p in pos):
            batch.put("interface", interface_id, "iface.position", pos, method_version)
            seen_props.add("iface.position")
        batch.close_stale("interface", interface_id, seen_props)
    if iface_rows:
        with con.cursor() as cur:
            cur.executemany(
                """INSERT INTO interface (interface_id, product_id, ordinal, kind, occ_a, occ_b,
                                           ref_a, ref_b, feature_a, feature_b)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (interface_id) DO UPDATE SET
                       ordinal=EXCLUDED.ordinal, kind=EXCLUDED.kind, occ_a=EXCLUDED.occ_a,
                       occ_b=EXCLUDED.occ_b, ref_a=EXCLUDED.ref_a, ref_b=EXCLUDED.ref_b,
                       feature_a=EXCLUDED.feature_a, feature_b=EXCLUDED.feature_b""",
                iface_rows)

    existing = {r[0] for r in con.execute(
        "SELECT interface_id FROM interface WHERE product_id=%s", (product_id,)).fetchall()}
    gone = sorted(existing - seen)
    for iid in gone:
        batch.close_stale("interface", iid, set())
    batch.flush()
    if gone:
        con.execute("DELETE FROM interface WHERE interface_id = ANY(%s)", (gone,))


# ---------------------------------------------------------------------------
# bulk_writer: a second entry point into the SAME dictionary-driven
# translation (`record_to_rows`) as `write_part`/`write_product` above, for loading many
# parts/products at once via `COPY ... FROM STDIN` instead of one `INSERT`/`UPDATE` pair per
# fact. The history model (compare-then-close-then-insert) is deliberately NOT applied
# here -- there is no "current fact to compare against" the first time a database is filled,
# and comparing anyway would cost one SELECT per fact for no reason. `bulk_writer` therefore
# REFUSES a database that already has any current fact row (`valid_to IS NULL`) at all: it is
# for an empty target only (a fresh `buildcore_scale`), never a re-ingest of a database `write_part`
# has already written history into. Every row this writer emits still goes through
# `record_to_rows`, so a change to the dictionary/derived-facts translation is felt by both
# writer paths at once, per this module's own docstring ("this module never hard-codes that
# split a second time").
# ---------------------------------------------------------------------------

_BULK_TABLES = (
    # (table, columns) -- COPY order matters: parents before children (FK order).
    ("document", ("doc_id", "path", "doc_name", "format", "sha256", "source_system",
                  "tool_version", "object_count", "part_count", "ingested_at")),
    ("class", ("class_id", "parent_id", "name", "irdi")),
    ("part_master", ("part_no", "class_id", "name")),
    ("part_revision", ("part_id", "part_no", "revision", "doc_id", "object_ref", "label",
                       "type_id", "units", "geom_hash")),
    ("feature", ("feature_id", "part_id", "ordinal", "kind", "role")),
    ("signature", ("part_id", "method", "vec")),
    ("fact", ("entity_kind", "entity_id", "property_id", "value_num", "value_text",
              "value_vec", "value_json", "source", "confidence", "measure_method",
              "uncertainty", "method_version", "valid_from")),
    ("product", ("product_id", "name", "revision", "description", "manifest_path", "created_at")),
    ("occurrence", ("occ_id", "product_id", "ref_des", "instance", "part_id", "parent_ref",
                    "placement", "notes")),
)


def _bulk_refuse_if_not_empty(con):
    row = con.execute("SELECT EXISTS (SELECT 1 FROM fact WHERE valid_to IS NULL)").fetchone()
    if row and row[0]:
        raise ValueError(
            "bulk_writer refuses a database that already has current facts -- it is for an "
            "empty target only (a fresh database, e.g. buildcore_scale right after apply_schema()"
            " + seed()); re-ingesting into a database write_part has already written history "
            "into must go through write_part/write_product instead"
        )


class BulkWriter:
    """Buffers rows for the `_BULK_TABLES` above; `flush()` (also called by `__exit__`) sends
    each table through one `COPY ... FROM STDIN`. Every `add_*` method mirrors its `write_*`
    sibling's non-history bits (same part_id/feature_id/occ_id construction) but skips
    `_put_fact`'s current-fact lookup entirely -- a fact row here is simply INSERTed with
    `valid_from=now, valid_to=NULL`, since `_bulk_refuse_if_not_empty` already proved there is
    no prior history to reconcile against."""

    def __init__(self, con, method_version=None):
        self.con = con
        self.method_version = method_version or METHOD_VERSION
        self.rows = {table: [] for table, _cols in _BULK_TABLES}
        # pg/seed.py already inserts one `class` row per families.json family (a bulk target
        # is empty of FACTS, per _bulk_refuse_if_not_empty, but never of dictionary/class rows
        # -- schema+seed always runs first) -- COPY has no ON CONFLICT, so any class this
        # writer's own callers ask for that seed.py already created must be skipped, not
        # re-queued, or COPY raises a duplicate-key error on the second one.
        self._seen_classes = {r[0] for r in con.execute("SELECT class_id FROM class").fetchall()}
        self._n_parts = 0
        self._n_products = 0

    # -- documents / classes -------------------------------------------------
    def add_document(self, doc_id, path=None, doc_name=None, format="synthetic",
                      sha256=None, source_system="scale", tool_version=None,
                      object_count=None, part_count=None, now=None):
        now = now or datetime.now(timezone.utc)
        self.rows["document"].append((doc_id, path, doc_name, format, sha256, source_system,
                                       tool_version, object_count, part_count, now))

    def _ensure_class(self, class_id):
        if class_id not in self._seen_classes:
            self._seen_classes.add(class_id)
            self.rows["class"].append((class_id, None, class_id, None))

    def _fact_row(self, kind, entity_id, prop_id, value, method_version, source, extra, now):
        prop = _properties().get(prop_id)
        if prop is None:
            raise ValueError("unknown property_id %r (not in dictionary.json)" % prop_id)
        cols, key = _value_columns(prop["datatype"], value)
        if key is None:
            return
        self.rows["fact"].append((
            kind, entity_id, prop_id, cols["value_num"], cols["value_text"], cols["value_vec"],
            cols["value_json"], source, extra.get("confidence", 1), extra.get("measure_method"),
            extra.get("uncertainty"), method_version, now,
        ))

    # -- parts ----------------------------------------------------------------
    def add_part(self, doc_id, record, family=None, method_version=None, source="measured",
                 signatures=None, now=None):
        """Same translation as `write_part` (record_to_rows over `record_map()["part"|
        "feature"]`, geom_hash at write time) minus history and minus per-solid provenance
        (`_write_solids` needs a live shape; the scale test's variant cache carries
        `d2_v1`/`moments_v1` vectors, already computed once at variant-measurement time, not
        the shape itself -- pass them via `signatures={"d2_v1": [...], "moments_v1": [...]}`).
        -> part_id."""
        method_version = method_version or self.method_version
        now = now or datetime.now(timezone.utc)
        part_id = "%s:%s" % (doc_id, record["object"])
        class_id = family or "unclassified"
        self._ensure_class(class_id)
        geom_hash = _signature.geom_hash(record)
        self.rows["part_master"].append((part_id, class_id, record.get("label")))
        self.rows["part_revision"].append((
            part_id, part_id, "A", doc_id, record.get("object"), record.get("label"),
            record.get("type_id"), record.get("units"), geom_hash,
        ))

        pmap = record_map()["part"]
        properties = _properties()
        extra_by_prop = {
            "part.volume": {"measure_method": record.get("volume_method"),
                             "uncertainty": record.get("volume_uncertainty_mm3")},
            "part.area": {"measure_method": record.get("area_method"),
                           "uncertainty": record.get("area_uncertainty_mm2")},
        }
        for prop_id, value, extra in record_to_rows(pmap, record, properties, extra_by_prop):
            self._fact_row("part", part_id, prop_id, value, method_version, source, extra, now)

        fmap = record_map()["feature"]
        for i, feat in enumerate(record.get("features") or [], start=1):
            feature_id = "%s/f%d" % (part_id, i)
            self.rows["feature"].append((feature_id, part_id, i, feat.get("kind"), feat.get("role")))
            for prop_id, value, extra in record_to_rows(fmap, feat, properties):
                self._fact_row("feature", feature_id, prop_id, value, method_version,
                                source, extra, now)

        for method, vec in (signatures or {}).items():
            literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
            self.rows["signature"].append((part_id, method, literal))

        self._n_parts += 1
        return part_id

    # -- products ---------------------------------------------------------------
    def add_product(self, product_id, name, revision, description, manifest_path, created_at,
                     bom, method_version=None, now=None):
        """Same shape as `write_product`'s `bom` (`[{ref_des, part_id, qty, parent_ref,
        placements, notes}, ...]`); `occurrence.qty` is written as a fact via `_fact_row`
        (bulk has no per-entity `_close_stale_facts` -- nothing stale exists yet)."""
        method_version = method_version or self.method_version
        now = now or datetime.now(timezone.utc)
        self.rows["product"].append((product_id, name, revision, description, manifest_path, created_at))
        for row in bom:
            ref_des = row["ref_des"]
            qty = row.get("qty") or 1
            placements = row.get("placements")
            for i in range(1, qty + 1):
                occ_id = "%s/%s#%d" % (product_id, ref_des, i)
                placement = _placement_for(placements, i)
                self.rows["occurrence"].append((
                    occ_id, product_id, ref_des, i, row.get("part_id"), row.get("parent_ref"),
                    placement, row.get("notes"),
                ))
            self._fact_row("occurrence", "%s/%s#1" % (product_id, ref_des), "occurrence.qty",
                            qty, method_version, "measured", {}, now)
        self._n_products += 1

    # -- flush ------------------------------------------------------------------
    def flush(self):
        with self.con.cursor() as cur:
            for table, cols in _BULK_TABLES:
                rows = self.rows[table]
                if not rows:
                    continue
                col_list = ", ".join(cols)
                with cur.copy("COPY %s (%s) FROM STDIN" % (table, col_list)) as copy:
                    for r in rows:
                        copy.write_row(r)
                rows.clear()
        self.con.commit()

    def counts(self):
        return {"parts": self._n_parts, "products": self._n_products}


class bulk_writer:
    """Context manager: `with bulk_writer(con) as bw: bw.add_part(...); bw.add_product(...)` --
    flushes every buffered row through `COPY` on a clean exit (an exception skips the flush,
    same "caller owns the transaction" contract as `write_part`/`write_product`: `con` is never
    committed on the way out of a failed `with` block). Refuses (`ValueError`) a `con` whose
    database already holds a current fact -- bulk mode is for an empty target only (see
    `_bulk_refuse_if_not_empty`); a re-ingest into a database `write_part` has already written
    history into must go through the single-row writer path instead."""

    def __init__(self, con, method_version=None):
        self.con = con
        self.method_version = method_version
        self._writer = None

    def __enter__(self):
        _bulk_refuse_if_not_empty(self.con)
        self._writer = BulkWriter(self.con, method_version=self.method_version)
        return self._writer

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._writer.flush()
        return False
