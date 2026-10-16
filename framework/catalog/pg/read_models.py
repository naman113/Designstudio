"""Generated read models:
`mv_class_<class>` wide materialized views (one column per PART-scoped `class_property`
row), `product_doc` (one JSONB document per product: occurrences -> part facts ->
feature facts, and interfaces with their facts), and `compat.*` views that reconstruct
today's SQLite-shaped `parts`/`features`/`interfaces`/`products`/`product_parts`/
`documents` tables from the fact layer, so old-shape consumers (find.py, the web app)
can read Postgres unchanged.

Nothing here hard-codes a class's DDL: `mv_class_<class>` and its columns come from
`class`/`class_property`/`property` (`_class_properties`, `_class_view_object`) --
insert a new `class_property` row and call `refresh()` and the column appears, no code
change. `product_doc`'s shape is fixed by design (it is not a
per-class object), but its SQL text is still built by `_product_doc_object()`, never
typed into `pg/schema.sql`. `compat.*` reconstructs a FROZEN legacy shape
(`framework/catalog/schema.sql`) from a FIXED set of property ids already in the
dictionary -- its column list is a small Python mapping table per view
(`_compat_parts_ddl` etc, the same pattern as `pg/load.py`'s own `_SURF_DERIVED`), not
a class-driven loop, because that legacy shape does not vary with the dictionary.
`compat.*` are plain views (always current, no refresh needed); `mv_class_*` are
materialized and go through `ensure()`'s change-hash + `refresh()`'s
`REFRESH MATERIALIZED VIEW CONCURRENTLY` loop. `product_doc` is a table with the
same change-hash, kept up to date per product by `refresh_products` (a sync rebuilds only
the docs of the designs it changed).

`raw_json` in `compat.parts` is left NULL: rebuilding the exact legacy audit blob is a
Python-side job (`framework.catalog.pg.records.part_record` + `json.dumps`), not
something worth reproducing as a giant generated SQL expression; nothing reads it
back from SQL.

    python -m framework.catalog.pg.read_models [--db NAME] refresh
    python -m framework.catalog.pg.read_models [--db NAME] ddl
    python -m framework.catalog.pg.read_models [--db NAME] payload <product_id>
"""
import argparse
import hashlib
import json
import os
import time

from framework.common import paths      # paths.basename splits a stored Windows path on Linux

try:
    from framework.catalog.pg import db as _db
except ImportError:      # no psycopg: build_part_card stays importable for the web app's SQLite path
    _db = None

# ---------------------------------------------------------------------------
# small SQL-building helpers -- every DDL string in this module is assembled through
# these, never typed as one literal chunk containing a real class/property name.
# ---------------------------------------------------------------------------


def _pg_literal(s):
    return "'" + s.replace("'", "''") + "'"


def _pg_text_array(items):
    return "ARRAY[" + ",".join(_pg_literal(i) for i in items) + "]::text[]"


def _sanitize(ident):
    out = "".join(c if c.isalnum() else "_" for c in ident.lower())
    out = out.strip("_")
    return out or "x"


def _num(entity_kind, id_expr, prop_id):
    return "(SELECT value_num FROM fact WHERE entity_kind=%s AND entity_id=%s AND property_id=%s AND valid_to IS NULL)" % (
        _pg_literal(entity_kind), id_expr, _pg_literal(prop_id))


def _txt(entity_kind, id_expr, prop_id):
    return "(SELECT value_text FROM fact WHERE entity_kind=%s AND entity_id=%s AND property_id=%s AND valid_to IS NULL)" % (
        _pg_literal(entity_kind), id_expr, _pg_literal(prop_id))


def _vec(entity_kind, id_expr, prop_id, idx1):
    return "(SELECT value_vec[%d] FROM fact WHERE entity_kind=%s AND entity_id=%s AND property_id=%s AND valid_to IS NULL)" % (
        idx1, _pg_literal(entity_kind), id_expr, _pg_literal(prop_id))


def _select(cols):
    return ",\n    ".join("%s AS %s" % (expr, name) for name, expr in cols)


# ---------------------------------------------------------------------------
# mv_class_<class>: one materialized view per class, one column per PART-scoped
# class_property row (a bearing/shaft feat.diameter class_property row is
# feature-scoped, not part-scoped, and is excluded; a class gets a view only when it
# has part-scoped class_property rows).
# ---------------------------------------------------------------------------

def _class_properties(con):
    """{class_id: [(property_id, datatype), ...]} for PART-scoped class_property rows,
    ordered by property_id for a stable column order (and a stable ddl hash)."""
    rows = con.execute(
        """SELECT cp.class_id, cp.property_id, p.datatype
           FROM class_property cp JOIN property p ON p.property_id = cp.property_id
           WHERE p.applies_to = 'part'
           ORDER BY cp.class_id, cp.property_id"""
    ).fetchall()
    out = {}
    for class_id, prop_id, datatype in rows:
        out.setdefault(class_id, []).append((prop_id, datatype))
    return out


def _class_value_expr(datatype, prop_id):
    col = prop_id.replace(".", "__")
    lit = _pg_literal(prop_id)
    if datatype in ("real", "int"):
        return "MAX(f.value_num) FILTER (WHERE f.property_id = %s) AS %s" % (lit, col)
    if datatype == "vector":
        return "(array_agg(f.value_vec) FILTER (WHERE f.property_id = %s))[1] AS %s" % (lit, col)
    if datatype == "map":
        return "(array_agg(f.value_json) FILTER (WHERE f.property_id = %s))[1] AS %s" % (lit, col)
    return "MAX(f.value_text) FILTER (WHERE f.property_id = %s) AS %s" % (lit, col)


def _class_view_object(class_id, props):
    view = "mv_class_" + _sanitize(class_id)
    prop_ids = [p for p, _dt in props]
    cols = ",\n    ".join(_class_value_expr(dt, p) for p, dt in props)
    create = (
        "CREATE MATERIALIZED VIEW %s AS\n"
        "SELECT pr.part_id, pr.label, pm.class_id,\n"
        "    %s\n"
        "FROM part_revision pr\n"
        "JOIN part_master pm ON pm.part_no = pr.part_no\n"
        "LEFT JOIN fact f ON f.entity_kind = 'part' AND f.entity_id = pr.part_id\n"
        "    AND f.valid_to IS NULL AND f.property_id = ANY(%s)\n"
        "WHERE pm.class_id = %s\n"
        "GROUP BY pr.part_id, pr.label, pm.class_id"
    ) % (view, cols, _pg_text_array(prop_ids), _pg_literal(class_id))
    index = "CREATE UNIQUE INDEX %s_part ON %s (part_id)" % (view, view)
    return {"name": view, "kind": "materialized", "create": [create, index]}


# ---------------------------------------------------------------------------
# product_doc: one JSONB nested record per product. Fixed shape (not class-driven),
# but the SQL text is still assembled here, never hand-written in schema.sql.
# ---------------------------------------------------------------------------

_FACT_CASE = (
    "CASE WHEN value_num IS NOT NULL THEN to_jsonb(value_num) "
    "WHEN value_vec IS NOT NULL THEN to_jsonb(value_vec) "
    "WHEN value_json IS NOT NULL THEN value_json "
    "ELSE to_jsonb(value_text) END"
)


def _facts_obj(entity_kind, id_expr):
    return (
        "COALESCE((SELECT jsonb_object_agg(property_id, %s) FROM fact "
        "WHERE entity_kind=%s AND entity_id=%s AND valid_to IS NULL), '{}'::jsonb)"
    ) % (_FACT_CASE, _pg_literal(entity_kind), id_expr)


def _product_doc_select():
    """The SELECT of every product's doc (`product_doc`), ending in `FROM product p` so a caller can narrow it."""
    part_facts = _facts_obj("part", "pr.part_id")
    feature_facts = _facts_obj("feature", "ft.feature_id")
    iface_facts = _facts_obj("interface", "i.interface_id")
    return (
        "SELECT p.product_id,\n"
        "  jsonb_build_object(\n"
        "    'product_id', p.product_id, 'name', p.name, 'revision', p.revision,\n"
        "    'description', p.description,\n"
        "    'occurrences', COALESCE((\n"
        "      SELECT jsonb_agg(jsonb_build_object(\n"
        "        'occ_id', o.occ_id, 'ref_des', o.ref_des, 'instance', o.instance,\n"
        "        'placement', to_jsonb(o.placement),\n"
        "        'part', jsonb_build_object(\n"
        "          'part_id', pr.part_id, 'label', pr.label, 'family', pm.class_id,\n"
        "          'doc_path', d.path,\n"
        "          'facts', %s,\n"
        "          'features', COALESCE((\n"
        "            SELECT jsonb_agg(jsonb_build_object('kind', ft.kind, 'role', ft.role,\n"
        "                'facts', %s) ORDER BY ft.ordinal)\n"
        "            FROM feature ft WHERE ft.part_id = pr.part_id\n"
        "          ), '[]'::jsonb)\n"
        "        )\n"
        "      ) ORDER BY o.ref_des COLLATE \"C\", o.instance)\n"
        "      FROM occurrence o\n"
        "      JOIN part_revision pr ON pr.part_id = o.part_id\n"
        "      JOIN part_master pm ON pm.part_no = pr.part_no\n"
        "      LEFT JOIN document d ON d.doc_id = pr.doc_id\n"
        "      WHERE o.product_id = p.product_id\n"
        "    ), '[]'::jsonb),\n"
        "    'interfaces', COALESCE((\n"
        "      SELECT jsonb_agg(jsonb_build_object(\n"
        "        'ordinal', i.ordinal, 'kind', i.kind, 'ref_a', i.ref_a, 'ref_b', i.ref_b,\n"
        "        'facts', %s)\n"
        "        ORDER BY i.kind,\n"
        "          (SELECT value_num FROM fact WHERE entity_kind='interface' AND entity_id=i.interface_id\n"
        "             AND property_id='iface.nominal_a' AND valid_to IS NULL) DESC NULLS LAST,\n"
        "          i.ordinal)\n"
        "      FROM interface i WHERE i.product_id = p.product_id\n"
        "    ), '[]'::jsonb)\n"
        "  ) AS doc\n"
        "FROM product p"
    ) % (part_facts, feature_facts, iface_facts)


def _product_doc_object():
    """A TABLE kept up to date per product (`refresh_products`), not a materialized view: a sync rebuilds the docs
    of the designs it changed, not every product's (a whole refresh grows with the catalogue and would dominate a
    small sync). Same rows, same SELECT."""
    create = "CREATE TABLE product_doc (product_id text PRIMARY KEY, doc jsonb NOT NULL)"
    gin = "CREATE INDEX product_doc_gin ON product_doc USING GIN (doc jsonb_path_ops)"
    return {"name": "product_doc", "kind": "table", "create": [create, gin], "select": _product_doc_select()}


def refresh_products(con, product_ids=None):
    """Rebuild the `product_doc` rows of `product_ids` (None: every product) from the facts; a doc that did not change is
    not rewritten; rows of products that no longer exist are removed and a product without a row gets one. Runs in the
    caller's transaction (a savepoint), or in one of its own on an autocommit connection: a reader never sees a half
    state. -> the number of products asked for (None for every product)."""
    sel = _product_doc_select()
    assert "%" not in sel
    upsert = ("INSERT INTO product_doc (product_id, doc) {sel}{where} ON CONFLICT (product_id) DO UPDATE SET doc = "
              "EXCLUDED.doc WHERE product_doc.doc IS DISTINCT FROM EXCLUDED.doc")
    narrow = upsert.format(sel=sel, where=" WHERE p.product_id = ANY(%s)")
    with con.transaction():
        if product_ids is None:
            con.execute(upsert.format(sel=sel, where=""))
        elif product_ids:
            con.execute(narrow, (list(product_ids),))
        con.execute("DELETE FROM product_doc d WHERE NOT EXISTS (SELECT 1 FROM product p WHERE p.product_id = d.product_id)")
        missing = [r[0] for r in con.execute(
            "SELECT p.product_id FROM product p WHERE NOT EXISTS "
            "(SELECT 1 FROM product_doc d WHERE d.product_id = p.product_id)").fetchall()]
        if missing:
            con.execute(narrow, (missing,))
    return None if product_ids is None else len(product_ids)


# ---------------------------------------------------------------------------
# compat.*: today's SQLite-shaped tables, recreated as plain views over the fact
# layer (framework/catalog/schema.sql is the frozen reference for every column here).
# ---------------------------------------------------------------------------

def _compat_parts_ddl():
    E, ID = "part", "pr.part_id"
    cols = [
        ("part_id", "pr.part_id"),
        ("doc_id", "pr.doc_id"),
        ("object_name", "pr.object_ref"),
        ("label", "pr.label"),
        ("type_id", "pr.type_id"),
        ("family", "pm.class_id"),
        ("family_why", _txt(E, ID, "part.family_why")),
        ("material", _txt(E, ID, "part.material")),
        ("units", "pr.units"),
        ("volume_mm3", _num(E, ID, "part.volume")),
        ("area_mm2", _num(E, ID, "part.area")),
        ("bbox_a_mm", _num(E, ID, "part.envelope_a")),
        ("bbox_b_mm", _num(E, ID, "part.envelope_b")),
        ("bbox_c_mm", _num(E, ID, "part.envelope_c")),
        ("bbox_xmin", _num(E, ID, "part.bbox_xmin")),
        ("bbox_xmax", _num(E, ID, "part.bbox_xmax")),
        ("bbox_ymin", _num(E, ID, "part.bbox_ymin")),
        ("bbox_ymax", _num(E, ID, "part.bbox_ymax")),
        ("bbox_zmin", _num(E, ID, "part.bbox_zmin")),
        ("bbox_zmax", _num(E, ID, "part.bbox_zmax")),
        ("com_x_mm", _vec(E, ID, "part.com", 1)),
        ("com_y_mm", _vec(E, ID, "part.com", 2)),
        ("com_z_mm", _vec(E, ID, "part.com", 3)),
        ("moment_1", _num(E, ID, "part.moment_1")),
        ("moment_2", _num(E, ID, "part.moment_2")),
        ("moment_3", _num(E, ID, "part.moment_3")),
        ("rgyr_1", _num(E, ID, "part.rgyr_1")),
        ("rgyr_2", _num(E, ID, "part.rgyr_2")),
        ("rgyr_3", _num(E, ID, "part.rgyr_3")),
        ("n_solids", _num(E, ID, "part.n_solids")),
        ("n_shells", _num(E, ID, "part.n_shells")),
        ("n_faces", _num(E, ID, "part.n_faces")),
        ("n_edges", _num(E, ID, "part.n_edges")),
        ("n_vertexes", _num(E, ID, "part.n_vertexes")),
        ("n_plane", _num(E, ID, "part.n_plane")),
        ("n_cylinder", _num(E, ID, "part.n_cylinder")),
        ("n_cone", _num(E, ID, "part.n_cone")),
        ("n_sphere", _num(E, ID, "part.n_sphere")),
        ("n_torus", _num(E, ID, "part.n_torus")),
        ("n_bspline", _num(E, ID, "part.n_bspline")),
        ("n_other_surf", _num(E, ID, "part.n_other_surf")),
        ("aspect_1", _num(E, ID, "part.aspect_1")),
        ("aspect_2", _num(E, ID, "part.aspect_2")),
        ("fill", _num(E, ID, "part.fill")),
        ("compactness", _num(E, ID, "part.compactness")),
        ("added", "NULL::text"),
        ("raw_json", "NULL::text"),  # see module docstring: use records.part_record for this
        # SQLite's parts rowid; LAST because CREATE OR REPLACE VIEW only appends
        ("seq", "pr.seq"),
    ]
    return (
        "CREATE OR REPLACE VIEW compat.parts AS\n"
        "SELECT\n    %s\n"
        "FROM part_revision pr\n"
        "JOIN part_master pm ON pm.part_no = pr.part_no"
    ) % _select(cols)


def _compat_features_ddl():
    E, ID = "feature", "ft.feature_id"
    cols = [
        ("feature_id", "(ROW_NUMBER() OVER (ORDER BY ft.part_id, ft.ordinal))::integer"),
        ("part_id", "ft.part_id"),
        ("kind", "ft.kind"),
        ("role", "ft.role"),
        ("dia_mm", _num(E, ID, "feat.diameter")),
        ("radius_mm", _num(E, ID, "feat.radius")),
        ("semi_angle_deg", _num(E, ID, "feat.semi_angle_legacy")),
        ("axis_x", _vec(E, ID, "feat.axis_legacy", 1)),
        ("axis_y", _vec(E, ID, "feat.axis_legacy", 2)),
        ("axis_z", _vec(E, ID, "feat.axis_legacy", 3)),
        ("pos_x", _vec(E, ID, "feat.position", 1)),
        ("pos_y", _vec(E, ID, "feat.position", 2)),
        ("pos_z", _vec(E, ID, "feat.position", 3)),
        ("offset_mm", _num(E, ID, "feat.offset")),
        ("extent_mm", _num(E, ID, "feat.length")),
        ("area_mm2", _num(E, ID, "feat.area")),
        ("face_count", _num(E, ID, "feat.face_count")),
        ("bb_xmin", _vec(E, ID, "feat.bbox", 1)),
        ("bb_xmax", _vec(E, ID, "feat.bbox", 2)),
        ("bb_ymin", _vec(E, ID, "feat.bbox", 3)),
        ("bb_ymax", _vec(E, ID, "feat.bbox", 4)),
        ("bb_zmin", _vec(E, ID, "feat.bbox", 5)),
        ("bb_zmax", _vec(E, ID, "feat.bbox", 6)),
    ]
    return "CREATE OR REPLACE VIEW compat.features AS\nSELECT\n    %s\nFROM feature ft" % _select(cols)


def _compat_interfaces_ddl():
    E, ID = "interface", "i.interface_id"
    cols = [
        ("interface_id", "(ROW_NUMBER() OVER (ORDER BY i.product_id, i.ordinal))::integer"),
        ("product_id", "i.product_id"),
        ("kind", "i.kind"),
        ("ref_a", "i.ref_a"),
        ("feature_a", "fa.n"),
        ("ref_b", "i.ref_b"),
        ("feature_b", "fb.n"),
        ("nominal_a_mm", _num(E, ID, "iface.nominal_a")),
        ("nominal_b_mm", _num(E, ID, "iface.nominal_b")),
        ("clearance_mm", _num(E, ID, "iface.clearance")),
        ("fit_class", _txt(E, ID, "iface.fit_class")),
        ("contact_area_mm2", _num(E, ID, "iface.contact_area")),
        ("axis_x", _vec(E, ID, "iface.axis", 1)),
        ("axis_y", _vec(E, ID, "iface.axis", 2)),
        ("axis_z", _vec(E, ID, "iface.axis", 3)),
        ("pos_x", _vec(E, ID, "iface.position", 1)),
        ("pos_y", _vec(E, ID, "iface.position", 2)),
        ("pos_z", _vec(E, ID, "iface.position", 3)),
    ]
    return (
        "CREATE OR REPLACE VIEW compat.interfaces AS\n"
        "WITH fnum AS (SELECT feature_id, ROW_NUMBER() OVER (ORDER BY part_id, ordinal) AS n FROM feature)\n"
        "SELECT\n    %s\n"
        "FROM interface i\n"
        "LEFT JOIN fnum fa ON fa.feature_id = i.feature_a\n"
        "LEFT JOIN fnum fb ON fb.feature_id = i.feature_b"
    ) % _select(cols)


def _compat_documents_ddl():
    return (
        "CREATE OR REPLACE VIEW compat.documents AS\n"
        "SELECT doc_id, path, doc_name, format, sha256 AS checksum_sha256, object_count, part_count,\n"
        "    tool_version, to_char(ingested_at, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS ingested_at\n"
        "FROM document"
    )


def _compat_products_ddl():
    return (
        "CREATE OR REPLACE VIEW compat.products AS\n"
        "SELECT product_id, name, revision, description, manifest_path, created_at\n"
        "FROM product"
    )


def _compat_product_parts_ddl():
    return (
        "CREATE OR REPLACE VIEW compat.product_parts AS\n"
        "SELECT product_id, ref_des,\n"
        "    (array_agg(part_id ORDER BY instance))[1] AS part_id,\n"
        "    COUNT(*)::integer AS qty,\n"
        "    (array_agg(parent_ref ORDER BY instance))[1] AS parent_ref,\n"
        "    (array_agg(placement ORDER BY instance))[1] AS placement_json,\n"
        "    (array_agg(notes ORDER BY instance))[1] AS notes\n"
        "FROM occurrence\n"
        "GROUP BY product_id, ref_des"
    )


def _compat_objects():
    return [
        {"name": "compat.documents", "kind": "view", "create": [_compat_documents_ddl()]},
        {"name": "compat.products", "kind": "view", "create": [_compat_products_ddl()]},
        {"name": "compat.product_parts", "kind": "view", "create": [_compat_product_parts_ddl()]},
        {"name": "compat.parts", "kind": "view", "create": [_compat_parts_ddl()]},
        {"name": "compat.features", "kind": "view", "create": [_compat_features_ddl()]},
        {"name": "compat.interfaces", "kind": "view", "create": [_compat_interfaces_ddl()]},
    ]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def _all_objects(con):
    objs = []
    for class_id, props in sorted(_class_properties(con).items()):
        objs.append(_class_view_object(class_id, props))
    objs.append(_product_doc_object())
    objs.extend(_compat_objects())
    return objs


def generate_ddl(con):
    """-> list of every SQL statement this module would run, for review (`ddl` CLI
    verb). Never edited by hand -- change the dictionary or the `_compat_*_ddl`
    column tables above, not this output."""
    ddl = ["CREATE SCHEMA IF NOT EXISTS compat"]
    for obj in _all_objects(con):
        ddl.extend(obj["create"])
        if obj.get("select"):
            ddl.append("INSERT INTO %s (product_id, doc) %s" % (obj["name"], obj["select"]))
    return ddl


def ensure(con):
    """Create every managed object; a materialized view is DROPped and recreated only
    when its generated DDL text changed since the last `ensure()` (readmodel_meta's
    sha256: "a new property is a row, never a migration" extended to read
    models: only a REAL dictionary change touches DDL). `compat.*` views are always
    `CREATE OR REPLACE` -- cheap, no materialized data to lose, always current.
    `product_doc` is a table (`_ensure_table`), rebuilt the same way when its text changed.
    -> list of materialized view names, for `refresh()`."""
    con.execute("CREATE SCHEMA IF NOT EXISTS compat")
    mv_names = []
    for obj in _all_objects(con):
        if obj["kind"] == "table":
            _ensure_table(con, obj)
            continue
        if obj["kind"] != "materialized":
            for stmt in obj["create"]:
                con.execute(stmt)
            continue
        name = obj["name"]
        mv_names.append(name)
        ddl_text = ";\n".join(obj["create"])
        digest = hashlib.sha256(ddl_text.encode("utf-8")).hexdigest()
        row = con.execute(
            "SELECT ddl_hash FROM readmodel_meta WHERE name=%s", (name,)
        ).fetchone()
        if row is not None and row[0] == digest:
            continue
        con.execute("DROP MATERIALIZED VIEW IF EXISTS %s CASCADE" % name)
        for stmt in obj["create"]:
            con.execute(stmt)
        con.execute(
            """INSERT INTO readmodel_meta (name, ddl_hash, refreshed_at) VALUES (%s, %s, now())
               ON CONFLICT (name) DO UPDATE SET ddl_hash = EXCLUDED.ddl_hash,
                   refreshed_at = EXCLUDED.refreshed_at""",
            (name, digest),
        )
    if not con.autocommit:
        con.commit()
    return mv_names


def _ensure_table(con, obj):
    """A read model kept as a table (product_doc): (re)created and filled, in one transaction, when its generated
    DDL or SELECT changed since the last `ensure()` or it is still the materialized view it was before."""
    name = obj["name"]
    digest = hashlib.sha256((";\n".join(obj["create"]) + ";\n" + obj["select"]).encode("utf-8")).hexdigest()
    row = con.execute("SELECT ddl_hash FROM readmodel_meta WHERE name=%s", (name,)).fetchone()
    rel = con.execute("SELECT c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                      "WHERE n.nspname = 'public' AND c.relname = %s", (name,)).fetchone()
    if row is not None and row[0] == digest and rel is not None and rel[0] == "r":
        return
    with con.transaction():
        if rel is not None:
            con.execute("DROP %s %s CASCADE" % ("MATERIALIZED VIEW" if rel[0] == "m" else "TABLE", name))
        for stmt in obj["create"]:
            con.execute(stmt)
        con.execute("INSERT INTO %s (product_id, doc) %s" % (name, obj["select"]))
        con.execute(
            """INSERT INTO readmodel_meta (name, ddl_hash, refreshed_at) VALUES (%s, %s, now())
               ON CONFLICT (name) DO UPDATE SET ddl_hash = EXCLUDED.ddl_hash,
                   refreshed_at = EXCLUDED.refreshed_at""",
            (name, digest),
        )


def refresh(con, product_ids=None):
    """`ensure(con)` then `REFRESH MATERIALIZED VIEW CONCURRENTLY` every managed
    materialized view, and the `product_doc` rows of `product_ids` (None = every
    product, a list = only those: what one sync changed). -> {"seconds", "objects"}."""
    t0 = time.perf_counter()
    names = ensure(con)
    for name in names:
        con.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY %s" % name)
        if not con.autocommit:
            con.commit()
    refresh_products(con, product_ids)
    if not con.autocommit:
        con.commit()
    elapsed = time.perf_counter() - t0
    if names:
        con.execute(
            "UPDATE readmodel_meta SET refreshed_at = now() WHERE name = ANY(%s)", (names,)
        )
        if not con.autocommit:
            con.commit()
    return {"seconds": elapsed, "objects": len(names)}


def _flt(value):
    """Coerce a fact value read back out of `product_doc`'s JSONB to a Python float,
    matching SQLite's REAL columns (`sqlite3` always returns a float for a REAL
    column, even a whole number like 110). A whole-number `double precision` fact
    round-trips through `to_jsonb`/JSON text as e.g. "110" (no decimal point), which
    `json` parses as an `int` -- left uncoerced, `json.dumps` would then render it as
    "110" instead of SQLite's "110.0", a false diff for identical numbers."""
    return None if value is None else float(value) + 0.0   # + 0.0: -0.0 -> 0.0 (see _nz)


def product_payload(con, product_id):
    """-> dict matching framework/web/app.py's `api_product` JSON minus `mesh_url`
    (that field comes from a mesh file on disk, not the catalogue -- app.py adds it), read from `product_doc`. None when the product does not exist."""
    row = con.execute(
        "SELECT doc FROM product_doc WHERE product_id=%s", (product_id,)
    ).fetchone()
    if row is None:
        return None
    doc = row[0]

    bom = []
    by_ref = {}
    for occ in doc.get("occurrences") or []:
        ref = occ["ref_des"]
        entry = by_ref.get(ref)
        if entry is None:
            part = occ.get("part") or {}
            facts = part.get("facts") or {}
            doc_path = part.get("doc_path")
            entry = {
                "ref": ref,
                "qty": 0,
                "label": part.get("label"),
                "family": part.get("family"),
                "volume_mm3": _flt(facts.get("part.volume")),
                "bbox_a_mm": _flt(facts.get("part.envelope_a")),
                "bbox_b_mm": _flt(facts.get("part.envelope_b")),
                "bbox_c_mm": _flt(facts.get("part.envelope_c")),
                "source": paths.basename(doc_path) if doc_path else None,
            }
            by_ref[ref] = entry
            bom.append(entry)
        entry["qty"] += 1

    interfaces = []
    for iface in doc.get("interfaces") or []:
        facts = iface.get("facts") or {}
        interfaces.append({
            "kind": iface.get("kind"),
            "ref_a": iface.get("ref_a"),
            "ref_b": iface.get("ref_b"),
            "nominal_a_mm": _flt(facts.get("iface.nominal_a")),
            "nominal_b_mm": _flt(facts.get("iface.nominal_b")),
            "clearance_mm": _flt(facts.get("iface.clearance")),
            "fit_class": facts.get("iface.fit_class"),
            "contact_area_mm2": _flt(facts.get("iface.contact_area")),
        })

    return {
        "product_id": doc.get("product_id"),
        "name": doc.get("name"),
        "revision": doc.get("revision"),
        "description": doc.get("description"),
        "bom": bom,
        "interfaces": interfaces,
    }


def product_list(con):
    """-> the rows framework/web/app.py's `api_products` returns today (every product, same keys,
    same order: created_at DESC, name). `parts` = BOM lines (distinct ref_des), `interfaces` = count.
    The sort mirrors SQLite (BINARY collation = "C"; NULLs smallest). The hidden (trashed) products
    are removed by the caller."""
    rows = con.execute(
        """SELECT p.product_id, p.name, p.revision, p.description,
              (SELECT COUNT(*) FROM (SELECT DISTINCT o.ref_des FROM occurrence o
                                     WHERE o.product_id = p.product_id) t),
              (SELECT COUNT(*) FROM interface i WHERE i.product_id = p.product_id)
           FROM product p
           ORDER BY p.created_at DESC NULLS LAST, p.name COLLATE "C" NULLS FIRST, p.product_id"""
    ).fetchall()
    return [{"product_id": r[0], "name": r[1], "revision": r[2], "description": r[3],
             "parts": r[4], "interfaces": r[5]} for r in rows]


def build_part_card(pp, cyl_rows, kinds):
    """The part card JSON of framework/web/app.py's `api_part`, computed from plain values so the
    SQLite path and the Postgres path run the SAME grouping and rounding (they cannot drift).
    `pp`: mapping with ref_des, qty, part_id, label, family, volume_mm3, area_mm2, bbox_a_mm,
    bbox_b_mm, bbox_c_mm, n_faces. `cyl_rows`: the part's cylinder features IN FEATURE ORDER, each a
    mapping with role, dia_mm, extent_mm, axis_x/y/z, pos_x/y/z, bb_xmin..bb_zmax. `kinds`:
    {feature kind: count}. Cylinder faces larger than the part itself are gentle blends, not
    features, and are left out (counted)."""
    size = max(pp["bbox_a_mm"] or 0, pp["bbox_b_mm"] or 0, pp["bbox_c_mm"] or 0)
    groups, blends = {}, 0
    for f in cyl_rows:
        if not f["dia_mm"]:
            continue
        if size and f["dia_mm"] > size * 1.05:
            blends += 1
            continue
        k = (f["role"] or "cyl", round(f["dia_mm"], 2))
        g = groups.setdefault(k, {"role": k[0], "dia_mm": k[1], "count": 0, "length_mm": 0, "where": []})
        g["count"] += 1
        # where it is, in the same frame as the viewer mesh: axis line (direction + a point
        # on it), length, and the face's bounding box (its centre locates it along the axis)
        g["where"].append({"axis": [f["axis_x"], f["axis_y"], f["axis_z"]], "pos": [f["pos_x"], f["pos_y"], f["pos_z"]],
                           "length_mm": f["extent_mm"],
                           "bbox": [f["bb_xmin"], f["bb_xmax"], f["bb_ymin"], f["bb_ymax"], f["bb_zmin"], f["bb_zmax"]]})
        g["length_mm"] = max(g["length_mm"], round(f["extent_mm"] or 0, 1))
    return {"ref": pp["ref_des"], "qty": pp["qty"], "label": pp["label"], "family": pp["family"],
            "volume_mm3": pp["volume_mm3"], "area_mm2": pp["area_mm2"],
            "bbox_mm": [pp["bbox_a_mm"], pp["bbox_b_mm"], pp["bbox_c_mm"]], "faces": pp["n_faces"],
            "cylinders": sorted(groups.values(), key=lambda g: -g["dia_mm"]), "blends_omitted": blends,
            "planes": kinds.get("plane", 0), "cones": kinds.get("cone", 0)}


_CYL_COLS = (
    ("role", None), ("dia_mm", "feat.diameter"), ("extent_mm", "feat.length"),
    ("axis_x", ("feat.axis_legacy", 1)), ("axis_y", ("feat.axis_legacy", 2)), ("axis_z", ("feat.axis_legacy", 3)),
    ("pos_x", ("feat.position", 1)), ("pos_y", ("feat.position", 2)), ("pos_z", ("feat.position", 3)),
    ("bb_xmin", ("feat.bbox", 1)), ("bb_xmax", ("feat.bbox", 2)), ("bb_ymin", ("feat.bbox", 3)),
    ("bb_ymax", ("feat.bbox", 4)), ("bb_zmin", ("feat.bbox", 5)), ("bb_zmax", ("feat.bbox", 6)),
)


def _cyl_select():
    """The cylinder-feature SELECT: the same fact lookups `compat.features` uses (same property ids,
    same `_num`/`_vec` text), read per part so Postgres does not number all 28,000 features per
    request (compat.features carries a window function that cannot take a part_id filter)."""
    cols = []
    for name, spec in _CYL_COLS:
        if spec is None:
            cols.append("ft.role")
        elif isinstance(spec, str):
            cols.append(_num("feature", "ft.feature_id", spec))
        else:
            cols.append(_vec("feature", "ft.feature_id", spec[0], spec[1]))
    return "SELECT %s\nFROM feature ft WHERE ft.part_id = %%s AND ft.kind = 'cylinder' ORDER BY ft.ordinal" % ",\n  ".join(cols)


def _nz(value):
    """Negative zero -> 0.0. SQLite's REAL columns keep no sign on a zero (-0.0 is stored as 0), the fact
    layer keeps the double as measured, and JSON would print "-0.0" against "0.0": same number, so the
    page's JSON must not differ."""
    return value + 0.0 if isinstance(value, float) else value


def part_card(con, product_id, ref):
    """-> the exact dict `api_part` returns for (product_id, ref), read from the compat views and the
    fact layer; None when the product has no such part."""
    row = con.execute(
        """SELECT pp.ref_des, pp.qty, pa.part_id, pa.label, pa.family, pa.volume_mm3, pa.area_mm2,
                  pa.bbox_a_mm, pa.bbox_b_mm, pa.bbox_c_mm, pa.n_faces
           FROM compat.product_parts pp JOIN compat.parts pa ON pa.part_id = pp.part_id
           WHERE pp.product_id = %s AND pp.ref_des = %s""", (product_id, ref)).fetchone()
    if row is None:
        return None
    keys = ("ref_des", "qty", "part_id", "label", "family", "volume_mm3", "area_mm2",
            "bbox_a_mm", "bbox_b_mm", "bbox_c_mm", "n_faces")
    pp = {k: _nz(v) for k, v in zip(keys, row)}
    if pp["n_faces"] is not None:        # SQLite INTEGER column; the fact is a double
        pp["n_faces"] = int(pp["n_faces"])
    names = [n for n, _s in _CYL_COLS]
    cyl_rows = [{n: _nz(v) for n, v in zip(names, r)}
                for r in con.execute(_cyl_select(), (pp["part_id"],)).fetchall()]
    kinds = dict(con.execute("SELECT kind, COUNT(*) FROM feature WHERE part_id = %s GROUP BY kind",
                             (pp["part_id"],)).fetchall())
    return build_part_card(pp, cyl_rows, kinds)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="database to operate on (default: the BC_PG_DSN database)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refresh", help="ensure() + REFRESH MATERIALIZED VIEW CONCURRENTLY every mv")
    sub.add_parser("ddl", help="print the generated SQL, for review (never hand-edit it)")
    p_payload = sub.add_parser("payload", help="print product_payload(con, product_id) as JSON")
    p_payload.add_argument("product_id")
    args = ap.parse_args(argv)

    if args.cmd == "refresh":
        _db.refuse_main_db_in_worktree(args.db or _db.dbname())
    con = _db.connect(dbname=args.db, autocommit=True)
    try:
        if args.cmd == "ddl":
            for stmt in generate_ddl(con):
                print(stmt + ";\n")
        elif args.cmd == "refresh":
            result = refresh(con)
            print("refresh: %.3fs, %d materialized views" % (result["seconds"], result["objects"]))
        elif args.cmd == "payload":
            payload = product_payload(con, args.product_id)
            print(json.dumps(payload, indent=2, sort_keys=True))
    finally:
        con.close()


if __name__ == "__main__":
    main()
