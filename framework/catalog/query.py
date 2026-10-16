"""The one query API over the catalogue facts.

    python -m framework.catalog.query '{"find":"feature","where":{"bore":[38,42]},"group_by":"product"}'
    python -m framework.catalog.query --file request.json --db buildcore
    python -m framework.catalog.query --bore 40 --tol 0.5 --format text     (find.py-style flags)

A request is `{"find", "where", "group_by", "return", "rank", "limit"}` (only "find" and
"where" are usually needed). `find` is part|feature|product|interface. `where` keys are
either a `property_id` (e.g. "feat.diameter", "part.family") or a word from the `synonym`
table (e.g. "bore", "od", "journal", "length", "volume", "family" -- a synonym that carries a
role, such as "bore" -> feat.diameter role=bore, adds that role as an extra condition on the
SAME feature row). Every key is validated against the `property`/`synonym` tables before it
ever touches SQL text -- an unknown word is an error response listing every valid
term, never a silently-dropped filter. Values: a scalar means equality; `[lo, hi]` means
BETWEEN (inclusive); `{"near": v, "tol": t}` means `ABS(x - v) <= t`; `{"like": "text"}` means
`ILIKE '%text%'`. Column-stored properties (`stored_as` = "column:<table>.<column>", e.g.
part.family/part.label) compile straight to that column; fact-stored properties compile to a
join on `fact` (current rows only, `valid_to IS NULL`). Part-scoped filters constrain the
part/feature row already in the query; a feature-scoped filter used while `find=part`
(e.g. "bore") becomes one `EXISTS` against the `feature` table, and every feature-scoped
filter sharing the same role (or sharing no role at all) is folded into the SAME EXISTS/feature
row, per the design brief -- two DIFFERENT roles (bore vs od) get two independent EXISTS
checks, matching find.py's own behaviour of two separate per-role existence checks.

Every response carries `coverage` (products/parts/features searched -- the WHOLE catalogue
scope, not just the matches -- plus how many candidates lack each filtered property) and a
one-line `text` ("N products, M parts searched"), so a caller can never mistake "found nothing"
for "searched nothing".

Display/ranking columns for `find=part` are read from `compat.parts`/`compat.features`
(framework/catalog/pg/read_models.py) -- the exact SQLite-shaped, numerically
exact-equal read models -- so `rank: {"near": ...}` / `{"like": ..., "method": "ratios"}`
(the default when `method` is omitted) reproduce find.py's `near_score`/`shape_dist`
formulas over IDENTICAL numbers, not a re-derivation that could drift.

`like` accepts a `compat.parts.part_id` prefix (unchanged), a bare `ref_des` (e.g. "CRANK",
exact, then any letter case) or a part label (any letter case); the response's `rank_resolved`
says which concrete part was used. `rank.method` "d2" / "moments" (shape-similarity ranking) is
not available and answers a QueryError.

Safety: `limit` is capped at 1000 (default 50 for the JSON API; the CLI's find.py-
style flags default to 10, matching find.py itself) and every query connection gets a 5s
`statement_timeout`.

find.py answers from here through `source_choice` / `handover`: Postgres when
it is reachable and current, else their own unchanged SQLite branch. This file only reads from
Postgres via framework.catalog.pg.db, never sqlite3.
"""
import argparse
import json
import math
import os
import sys
import time

try:  # find.py imports this module from interpreters without psycopg to decide the source
    from psycopg.rows import dict_row

    from framework.catalog.pg import db as pgdb
except ImportError:  # the venv interpreter does the Postgres work
    dict_row = None
    pgdb = None

STEEL = 7.85e-6  # kg/mm3 -- same constant as find.py, for identical near_score(mass_kg=...)
NEAR_COLS = {"length": "bbox_a_mm", "width": "bbox_b_mm", "height": "bbox_c_mm",
             "volume": "volume_mm3", "faces": "n_faces", "area": "area_mm2"}
SHAPE_COLS = ("aspect_1", "aspect_2", "fill", "compactness")

DEFAULT_LIMIT = 50
MAX_LIMIT = 1000

# entity_kind -> (table queried for the "searched" total, id column)
_TOTAL_TABLE = {
    "part": "part_revision", "feature": "feature", "product": "product",
    "interface": "interface", "document": "document", "occurrence": "occurrence",
}
# find -> {applies_to reachable as a direct join in the base query: (entity_kind, id_expr)}
_OWN_SCOPE = {
    "part": {"part": ("part", "pr.part_id")},
    "feature": {"feature": ("feature", "ft.feature_id"), "part": ("part", "pr.part_id")},
    "product": {"product": ("product", "p.product_id")},
    "interface": {"interface": ("interface", "i.interface_id")},
}


class QueryError(Exception):
    """A request that cannot be compiled: an unknown term, an unsupported filter scope, or
    a malformed value. `valid_terms`, when set, lists every property_id/synonym term the
    caller could have used instead (must_have: 'never silently dropped')."""

    def __init__(self, message, valid_terms=None):
        super().__init__(message)
        self.valid_terms = valid_terms or []


# ---------------------------------------------------------------------------
# dictionary lookups (property/synonym tables) -- loaded fresh per request; both tables are
# tiny (dozens of rows), and a dictionary change (a new class_property row, a new synonym)
# must be visible on the very next call, not cached stale (same "rows, never a migration"
# principle the read models follow).
# ---------------------------------------------------------------------------

def _load_properties(con):
    rows = con.execute(
        "SELECT property_id, applies_to, datatype, stored_as FROM property"
    ).fetchall()
    return {pid: {"applies_to": at, "datatype": dt, "stored_as": sa} for pid, at, dt, sa in rows}


def _load_synonyms(con):
    out = {}
    for term, prop_id, role in con.execute("SELECT term, property_id, role FROM synonym").fetchall():
        out.setdefault(term, []).append((prop_id, role))
    return out


def _resolve_term(key, properties, synonyms):
    """-> (property_id, role_or_None). Raises QueryError listing every valid term."""
    if key in properties:
        return key, None
    matches = synonyms.get(key) or synonyms.get(key.lower())
    if matches:
        return matches[0]
    valid = sorted(set(properties) | set(synonyms))
    raise QueryError("unknown term %r; valid terms: %s" % (key, ", ".join(valid)), valid_terms=valid)


# ---------------------------------------------------------------------------
# value forms -> SQL condition (bound parameters only)
# ---------------------------------------------------------------------------

def _value_form(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return "range", value[0], value[1]
    if isinstance(value, dict):
        if "near" in value:
            return "near", value["near"], value.get("tol", 0.5)
        if "like" in value:
            return "like", value["like"]
        if value.get("min") is not None or value.get("max") is not None:
            # an open range, the catalogue page's "from" / "to" with one end left empty
            lo, hi = value.get("min"), value.get("max")
            if lo is not None and hi is not None:
                return "range", lo, hi
            return ("min", lo) if lo is not None else ("max", hi)
        raise QueryError("unsupported filter value form: %r" % (value,))
    return "eq", value


def _where_items(where):
    """`where` as the usual dict, or a list of [term, value] pairs, so one term can carry two conditions:
    the catalogue page ANDs the search box ("bore 20") with its filters (a bore range), and two conditions of the same
    feature role still land on the SAME feature row (one EXISTS, as below)."""
    if not where:
        return []
    if isinstance(where, dict):
        return list(where.items())
    if isinstance(where, (list, tuple)) and all(
            isinstance(x, (list, tuple)) and len(x) == 2 and isinstance(x[0], str) for x in where):
        return [(x[0], x[1]) for x in where]
    raise QueryError("where must be an object or a list of [term, value] pairs")


def _value_condition(ref, value):
    kind, *rest = _value_form(value)
    if kind == "range":
        lo, hi = rest
        return "%s BETWEEN %%s AND %%s" % ref, [lo, hi]
    if kind == "min":
        return "%s >= %%s" % ref, [rest[0]]
    if kind == "max":
        return "%s <= %%s" % ref, [rest[0]]
    if kind == "near":
        v, tol = rest
        return "ABS(%s - %%s) <= %%s" % ref, [v, tol]
    if kind == "like":
        text = rest[0]
        return "%s ILIKE %%s" % ref, ["%" + text + "%"]
    (v,) = rest
    return "%s = %%s" % ref, [v]


def _column_ref(stored_as, alias_map):
    _, rest = stored_as.split(":", 1)
    table, col = rest.split(".", 1)
    alias = alias_map.get(table)
    if alias is None:
        raise QueryError("property stored on table %r is not reachable in this query" % table)
    return "%s.%s" % (alias, col)


def _compile_filter(n, prop_id, meta, value, entity_kind, id_expr, alias_map):
    """-> (join_sql_or_None, join_params, cond_sql, cond_params). `n` makes the fact-join
    alias unique within one compiled query."""
    stored_as, datatype = meta["stored_as"], meta["datatype"]
    if stored_as.startswith("column:"):
        ref = _column_ref(stored_as, alias_map)
        cond, params = _value_condition(ref, value)
        return None, [], cond, params
    alias = "vf%d" % n
    join = ("JOIN fact %s ON %s.entity_kind=%%s AND %s.entity_id=%s "
            "AND %s.property_id=%%s AND %s.valid_to IS NULL") % (alias, alias, alias, id_expr, alias, alias)
    join_params = [entity_kind, prop_id]
    if datatype in ("real", "int"):
        ref = "%s.value_num" % alias
    elif datatype == "vector":
        ref = "%s.value_vec" % alias
    else:
        ref = "%s.value_text" % alias
    cond, params = _value_condition(ref, value)
    return join, join_params, cond, params


def _assemble(base_from, specs, base_conds=None, base_params=None):
    """specs: [(join_or_None, join_params, cond, cond_params), ...]. Every JOIN precedes
    every WHERE condition in the emitted text, so params must be ordered the same way:
    all join params (in spec order) then all condition params (in spec order)."""
    joins, join_params = [], []
    conds = list(base_conds) if base_conds else ["1=1"]
    cond_params = list(base_params) if base_params else []
    for join, jp, cond, cp in specs:
        if join:
            joins.append(join)
            join_params.extend(jp)
        conds.append(cond)
        cond_params.extend(cp)
    sql = base_from
    if joins:
        sql += " " + " ".join(joins)
    sql += " WHERE " + " AND ".join(conds)
    return sql, join_params + cond_params


def _feature_exists_clause(gi, role, filters, alias_map):
    """One EXISTS(...) testing that a SINGLE feature of the owning part satisfies every
    condition in `filters` (all feature-scoped filters that share this role, or share no
    role at all -- see module docstring)."""
    alias = "ftx%d" % gi
    local_alias_map = dict(alias_map, feature=alias)
    specs = [
        _compile_filter(gi * 1000 + j, prop_id, meta, value, "feature", "%s.feature_id" % alias, local_alias_map)
        for j, (prop_id, _role, meta, value) in enumerate(filters)
    ]
    base_conds = ["%s.part_id = pr.part_id" % alias]
    base_params = []
    if role is not None:
        base_conds.append("%s.role = %%s" % alias)
        base_params.append(role)
    inner_sql, inner_params = _assemble("SELECT 1 FROM feature %s" % alias, specs, base_conds, base_params)
    return "EXISTS (%s)" % inner_sql, inner_params


# ---------------------------------------------------------------------------
# The unranked find.py order is SQLite's plan order for `SELECT p.* FROM parts p WHERE ...`
# (measured with EXPLAIN QUERY PLAN over every subset of the seven CLI filters): a
# `family =` filter walks ix_parts_family (family, rowid); else a `bbox_a BETWEEN` walks ix_parts_bbox_a
# (bbox_a, rowid); else a `volume BETWEEN` walks ix_parts_volume (volume, rowid); anything else is a
# table scan (rowid). rowid is `compat.parts.seq`. Ranked results sort stably on top of this base
# order, exactly as find.py's list.sort does, so ties come out the same way.
# ---------------------------------------------------------------------------

def _sqlite_part_order(order_hints):
    """order_hints: [(property_id, value_form)] of a request's `where` -> the compat.parts
    columns that reproduce SQLite's row order, e.g. ["volume_mm3", "seq"]."""
    forms = set(order_hints)
    if ("part.family", "eq") in forms:
        return ["seq"]
    if ("part.envelope_a", "range") in forms:
        return ["bbox_a_mm", "seq"]
    if ("part.volume", "range") in forms:
        return ["volume_mm3", "seq"]
    return ["seq"]


def _order_key(columns):
    def key(row):
        return tuple((row[c] is None, row[c] if row[c] is not None else 0) for c in columns)
    return key


# ---------------------------------------------------------------------------
# compile_request: JSON request -> (SQL text, params, everything run() needs to execute it)
# ---------------------------------------------------------------------------

def compile_request(request, con):
    properties = _load_properties(con)
    synonyms = _load_synonyms(con)

    find = request.get("find", "part")
    if find not in _OWN_SCOPE:
        raise QueryError("find must be one of part|feature|product|interface, got %r" % (find,))
    group_by = request.get("group_by")
    if group_by in ("none", ""):
        group_by = None
    if group_by not in (None, "product", "part"):
        raise QueryError("group_by must be one of product|part|none, got %r" % (group_by,))

    if find == "part":
        base_from = "FROM part_revision pr JOIN part_master pm ON pm.part_no = pr.part_no"
        alias_map = {"part_revision": "pr", "part_master": "pm"}
        select_expr, order_expr = "DISTINCT pr.part_id", "pr.part_id"
    elif find == "feature":
        base_from = ("FROM feature ft JOIN part_revision pr ON pr.part_id = ft.part_id "
                     "JOIN part_master pm ON pm.part_no = pr.part_no")
        alias_map = {"feature": "ft", "part_revision": "pr", "part_master": "pm"}
        if group_by == "product":
            base_from += " JOIN occurrence o ON o.part_id = pr.part_id JOIN product p ON p.product_id = o.product_id"
            alias_map["occurrence"] = "o"
            alias_map["product"] = "p"
            select_expr, order_expr = "DISTINCT p.product_id, o.ref_des, pr.part_id, ft.feature_id", "p.product_id, o.ref_des"
        else:
            select_expr, order_expr = "DISTINCT ft.feature_id, pr.part_id", "ft.feature_id"
    elif find == "product":
        base_from = "FROM product p"
        alias_map = {"product": "p"}
        select_expr, order_expr = "DISTINCT p.product_id", "p.product_id"
    else:  # interface
        base_from = "FROM interface i"
        alias_map = {"interface": "i"}
        select_expr, order_expr = "DISTINCT i.interface_id", "i.interface_id"

    reachable = _OWN_SCOPE[find]
    specs = []
    feature_groups = {}
    resolved_for_coverage = []
    order_hints = []
    n = 0
    for key, value in _where_items(request.get("where")):
        prop_id, role = _resolve_term(key, properties, synonyms)
        meta = properties[prop_id]
        applies_to = meta["applies_to"]
        resolved_for_coverage.append((prop_id, applies_to, meta["stored_as"]))
        order_hints.append((prop_id, _value_form(value)[0]))
        if applies_to in reachable:
            entity_kind, id_expr = reachable[applies_to]
            specs.append(_compile_filter(n, prop_id, meta, value, entity_kind, id_expr, alias_map))
            n += 1
            if role is not None:
                # A synonym role hint (e.g. "bore" -> feat.diameter role=bore) must narrow
                # to that role even when the filter compiles directly (find=feature) rather
                # than through the EXISTS grouping below (find=part) -- otherwise "bore" would
                # match ANY feature's diameter, od/face/chamfer included (a part with a 40 mm
                # OD would otherwise match a "bore 38-42" request).
                specs.append((None, [], "%s.role = %%s" % alias_map["feature"], [role]))
        elif find == "part" and applies_to == "feature":
            group_key = role if role is not None else "_default_"
            feature_groups.setdefault(group_key, []).append((prop_id, role, meta, value))
        else:
            valid = sorted(set(properties) | set(synonyms))
            raise QueryError(
                "%r (scope=%s) cannot be filtered when find=%s" % (prop_id, applies_to, find),
                valid_terms=valid,
            )

    for gi, (group_key, filters) in enumerate(sorted(feature_groups.items())):
        role = None if group_key == "_default_" else group_key
        cond, params = _feature_exists_clause(gi, role, filters, alias_map)
        specs.append((None, [], cond, params))

    sql_from, params = _assemble(base_from, specs)
    sql = "SELECT %s %s ORDER BY %s" % (select_expr, sql_from, order_expr)

    limit = request.get("limit", DEFAULT_LIMIT)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise QueryError("limit must be an integer, got %r" % (limit,))
    limit = min(max(limit, 1), MAX_LIMIT)
    rank = request.get("rank")
    if isinstance(rank, dict) and rank.get("method") in ("d2", "moments"):
        raise QueryError("shape-similarity ranking is not available")

    return {
        "find": find, "group_by": group_by, "sql": sql, "params": params,
        "resolved_filters": resolved_for_coverage, "rank": request.get("rank"),
        "part_order": _sqlite_part_order(order_hints), "limit": limit, "return_fields": request.get("return") or [],
        "properties": properties, "synonyms": synonyms,
    }


# ---------------------------------------------------------------------------
# coverage: how much of the catalogue this request searched, and what each filtered
# property is missing on (must_have: every response says how much it searched)
# ---------------------------------------------------------------------------

def coverage(con, resolved_filters):
    totals = {}
    for applies_to, table in _TOTAL_TABLE.items():
        totals[applies_to] = con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]

    missing = {}
    seen = set()
    for prop_id, applies_to, stored_as in resolved_filters:
        if prop_id in seen:
            continue
        seen.add(prop_id)
        total = totals.get(applies_to)
        if total is None:
            continue
        if stored_as.startswith("column:"):
            table, col = stored_as[len("column:"):].split(".", 1)
            have = con.execute("SELECT COUNT(*) FROM %s WHERE %s IS NOT NULL" % (table, col)).fetchone()[0]
        else:
            have = con.execute(
                "SELECT COUNT(DISTINCT entity_id) FROM fact "
                "WHERE entity_kind=%s AND property_id=%s AND valid_to IS NULL",
                (applies_to, prop_id),
            ).fetchone()[0]
        missing[prop_id] = max(total - have, 0)

    return {
        "products_searched": totals["product"],
        "parts_searched": totals["part"],
        "features_searched": totals["feature"],
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# ranking -- ported verbatim from find.py's algorithms (near_score / shape_dist), operating
# on compat.parts/compat.features rows so the numbers are the SAME numbers find.py itself
# reads (compat.* is exact-equal to the SQLite snapshot).
# ---------------------------------------------------------------------------

def _feature_dias(con, part_id, role):
    return [r[0] for r in con.execute(
        "SELECT dia_mm FROM compat.features WHERE part_id=%s AND role=%s", (part_id, role)
    ).fetchall()]


def _feature_dias_bulk(con, part_ids, roles):
    """{(part_id, role): [dia_mm, ...]} for many parts in ONE query, each list in the part's own
    feature order (= SQLite's rowid order). compat.features numbers every feature with a window over
    the whole table (~0.1-2.7 s per lookup), so ranking 353 candidates one by one took 36 s."""
    out = {}
    if not part_ids or not roles:
        return out
    rows = con.execute(
        "SELECT ft.part_id, ft.role, (SELECT value_num FROM fact WHERE entity_kind='feature' "
        "AND entity_id=ft.feature_id AND property_id='feat.diameter' AND valid_to IS NULL) "
        "FROM feature ft WHERE ft.part_id = ANY(%s) AND ft.role = ANY(%s) "
        "ORDER BY ft.part_id, ft.ordinal", (list(part_ids), list(roles))).fetchall()
    for part_id, role, dia in rows:
        if dia is not None:
            out.setdefault((part_id, role), []).append(dia)
    return out


def _near_score(con, row, spec, dias_map=None):
    errs, detail = [], []
    for key, target in spec.items():
        if key in ("bore", "od"):
            dias = (dias_map.get((row["part_id"], key), []) if dias_map is not None
                    else _feature_dias(con, row["part_id"], key))
            if not dias:
                errs.append(1.0); detail.append("%s:none" % key); continue
            best = min(dias, key=lambda d: abs(d - target))
            e = abs(best - target) / max(target, 1e-9)
            errs.append(min(e, 1.0)); detail.append("%s %.4g vs %.4g" % (key, best, target))
        elif key == "mass_kg":
            got = (row["volume_mm3"] or 0) * STEEL
            e = abs(got - target) / max(target, 1e-9)
            errs.append(min(e, 1.0)); detail.append("mass %.3g vs %.3g" % (got, target))
        else:
            col = NEAR_COLS.get(key)
            if col is None:
                continue
            got = row[col]
            if got is None:
                errs.append(1.0); detail.append("%s:none" % key); continue
            e = abs(got - target) / max(abs(target), 1e-9)
            errs.append(min(e, 1.0)); detail.append("%s %.4g vs %.4g" % (key, got, target))
    if not errs:
        return 0.0, ""
    return sum(errs) / len(errs), "; ".join(detail)


def _shape_vec(row):
    v = [row[c] for c in SHAPE_COLS]
    m = [row["moment_1"], row["moment_2"], row["moment_3"]]
    if all(x is not None for x in m) and m[0]:
        v += [m[1] / m[0], m[2] / m[0]]
    else:
        v += [None, None]
    return v


def _shape_dist(a, b):
    n = d = 0
    for x, y in zip(a, b):
        if x is None or y is None:
            continue
        d += (x - y) ** 2
        n += 1
    return math.sqrt(d / n) if n else None


def _like_escape(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _resolve_like(con, like_value, sig_method=None):
    """-> (part_id, how) for a `rank.like` value: a `compat.parts.part_id` prefix (a full
    or partial part_id, e.g. a crankshaft's own part_id)
    takes priority; failing that, a bare ref_des (e.g. "CRANK") resolves to the part
    occupying that ref_des, first by product_id when more than one product uses it (the
    response reports which one was used -- callers surface `how` back to the caller, never
    silently pick one). Raises QueryError when neither resolves.

    With `sig_method` (a shape search), among several matching parts the
    first one that HAS that signature wins (then part_id / product_id order as before), so
    "like LAYSHAFT" never resolves to a part with nothing to compare. A ref_des is matched
    exactly first, then in any letter case; failing both, a part label in any letter case.
    `how` says which rule matched and how many parts matched."""
    def pick(rows):
        """The first row (in the given order) whose part has the signature; else the first row."""
        if sig_method:
            signed = {r[0] for r in con.execute(
                "SELECT part_id FROM signature WHERE method = %s AND part_id = ANY(%s)",
                (sig_method, sorted({r[0] for r in rows}))).fetchall()}
            for r in rows:
                if r[0] in signed:
                    return r
        return rows[0]

    def several(how, rows):
        n = len({r[0] for r in rows})
        if n > 1:
            how += " (%d parts carry it%s)" % (n, "; the first with a shape signature" if sig_method else "")
        return how

    rows = con.execute("SELECT part_id FROM compat.parts WHERE part_id LIKE %s ORDER BY part_id",
                       (_like_escape(like_value) + "%",)).fetchall()
    if rows:
        return pick(rows)[0], "part_id prefix %r" % like_value

    for cond, word in (("o.ref_des = %s", "ref_des"), ("lower(o.ref_des) = lower(%s)", "ref_des (any case)")):
        rows = con.execute("SELECT o.part_id, o.product_id FROM occurrence o WHERE " + cond +
                           " ORDER BY o.product_id, o.instance", (like_value,)).fetchall()
        if rows:
            part_id, product_id = pick(rows)
            return part_id, several("%s %r on product %r" % (word, like_value, product_id), rows)

    rows = con.execute("SELECT part_id FROM compat.parts WHERE lower(label) = lower(%s) ORDER BY part_id",
                       (like_value,)).fetchall()
    if rows:
        return pick(rows)[0], several("label %r" % like_value, rows)
    raise QueryError("no such part: %r" % (like_value,))


def _part_results(con, part_ids, rank, limit, part_order=("seq",), raw_scores=False, filtered=True):
    matched = len(part_ids)
    if not part_ids:
        return [], matched, {}
    cur = con.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM compat.parts WHERE part_id = ANY(%s)", (part_ids,))
    by_id = {r["part_id"]: r for r in cur.fetchall()}
    ordered = sorted((by_id[pid] for pid in part_ids if pid in by_id), key=_order_key(part_order))

    meta = {}
    if rank and rank.get("near"):
        spec = rank["near"]
        dias_map = _feature_dias_bulk(
            con, [r["part_id"] for r in ordered], [k for k in spec if k in ("bore", "od")])
        scored = []
        for r in ordered:
            s, why = _near_score(con, r, spec, dias_map)
            scored.append((s, why, r))
        scored.sort(key=lambda t: t[0])
    elif rank and rank.get("like"):
        base_part_id, how = _resolve_like(con, rank["like"])
        meta["resolved"] = {"part_id": base_part_id, "how": how}
        cur.execute("SELECT * FROM compat.parts WHERE part_id = %s", (base_part_id,))
        base_row = cur.fetchone()
        if base_row is None:
            raise QueryError("no such part")
        bv = _shape_vec(base_row)
        scored = []
        for r in ordered:
            if r["part_id"] == base_row["part_id"]:
                continue
            d = _shape_dist(bv, _shape_vec(r))
            if d is not None:
                scored.append((d, "shape distance", r))
        scored.sort(key=lambda t: t[0])
    else:
        scored = [(0.0, "", r) for r in ordered]

    rows = [
        {"part_id": r["part_id"], "score": round(score, 6), "why": why,
         "label": r["label"], "family": r["family"],
         "volume_mm3": r["volume_mm3"], "bbox_a_mm": r["bbox_a_mm"]}
        for score, why, r in scored[:limit]
    ]
    if raw_scores:  # the text renderer prints 4 decimals of the exact score, as find.py does
        for row, (score, _why, _r) in zip(rows, scored[:limit]):
            row["score_raw"] = score
    return rows, matched, meta


def _project_value(con, properties, entity_kind, entity_id, prop_id):
    """Best-effort projection of one property's current value for `return`, outside the
    small fixed set compat.parts/compat.features already carry."""
    meta = properties[prop_id]
    stored_as = meta["stored_as"]
    if stored_as.startswith("column:"):
        table, col = stored_as[len("column:"):].split(".", 1)
        pk = {"part_revision": "part_id", "feature": "feature_id", "product": "product_id",
              "interface": "interface_id", "document": "doc_id", "occurrence": "occ_id"}.get(table)
        if pk is None:
            return None
        row = con.execute("SELECT %s FROM %s WHERE %s = %%s" % (col, table, pk), (entity_id,)).fetchone()
        return row[0] if row else None
    row = con.execute(
        "SELECT value_num, value_text, value_vec, value_json FROM fact "
        "WHERE entity_kind=%s AND entity_id=%s AND property_id=%s AND valid_to IS NULL",
        (entity_kind, entity_id, prop_id),
    ).fetchone()
    if row is None:
        return None
    value_num, value_text, value_vec, value_json = row
    datatype = meta["datatype"]
    if datatype == "real":
        return value_num
    if datatype == "int":
        return int(value_num) if value_num is not None else None
    if datatype == "vector":
        return list(value_vec) if value_vec is not None else None
    if datatype == "map":
        return dict(value_json) if isinstance(value_json, dict) else value_json
    return value_text


# ---------------------------------------------------------------------------
# run(): compile + execute + shape the response
# ---------------------------------------------------------------------------

def _product_info(con, product_ids):
    """{product_id: (name, revision)} for the grouped responses (one query on the product table)."""
    if not product_ids:
        return {}
    rows = con.execute("SELECT product_id, name, revision FROM product WHERE product_id = ANY(%s)",
                       (list(product_ids),)).fetchall()
    return {pid: (name, revision) for pid, name, revision in rows}


def _add_product_info(con, groups):
    info = _product_info(con, [g["product_id"] for g in groups])
    for g in groups:
        g["name"], g["revision"] = info.get(g["product_id"], (None, None))


def _group_parts_by_product(con, result_rows, ranked):
    """Part rows (already ranked/limited, in their order) -> product groups. Each occurrence of a
    part in a product is one entry {ref_des, part_id, label, score}; the part order inside a group
    is the result order; product order = first appearance for ranked requests, product_id otherwise."""
    part_ids = [r["part_id"] for r in result_rows]
    occ = {}
    if part_ids:
        for part_id, product_id, ref_des in con.execute(
                "SELECT part_id, product_id, ref_des FROM occurrence WHERE part_id = ANY(%s) "
                "ORDER BY product_id, ref_des, occ_id", (part_ids,)).fetchall():
            occ.setdefault(part_id, []).append((product_id, ref_des))
    groups_map = {}
    for r in result_rows:
        for product_id, ref_des in occ.get(r["part_id"], ()):
            groups_map.setdefault(product_id, []).append(
                {"ref_des": ref_des, "part_id": r["part_id"], "label": r["label"], "score": r["score"]})
    order = list(groups_map) if ranked else sorted(groups_map)
    groups = [{"product_id": pid, "parts": groups_map[pid]} for pid in order]
    _add_product_info(con, groups)
    return groups


def run(request, con=None, db=None, raw_scores=False):
    close_con = False
    if con is None:
        con = pgdb.connect(dbname=db, autocommit=True)
        close_con = True
    t0 = time.perf_counter()
    try:
        con.execute("SET statement_timeout = '5s'")
        try:
            compiled = compile_request(request, con)
        except QueryError as exc:
            return {"error": str(exc), "valid_terms": exc.valid_terms}

        find = compiled["find"]
        group_by = compiled["group_by"]
        limit = compiled["limit"]
        rank = compiled["rank"]
        properties, synonyms = compiled["properties"], compiled["synonyms"]

        def _text(cov):
            return "%d products, %d parts searched" % (cov["products_searched"], cov["parts_searched"])

        if find == "part":
            part_ids = [r[0] for r in con.execute(compiled["sql"], compiled["params"]).fetchall()]
            try:
                result_rows, matched, rank_meta = _part_results(
                    con, part_ids, rank, limit, compiled["part_order"], raw_scores,
                    filtered=bool(compiled["resolved_filters"]))
            except QueryError as exc:
                return {"error": str(exc)}
            cov = coverage(con, compiled["resolved_filters"])
            cov["missing"].update(rank_meta.get("sig_missing") or {})
            if group_by == "product":
                response = {"groups": _group_parts_by_product(con, result_rows, bool(rank)),
                            "coverage": cov, "text": _text(cov), "matched": matched,
                            "ms": round((time.perf_counter() - t0) * 1000, 2)}
                if rank_meta.get("resolved"):
                    response["rank_resolved"] = rank_meta["resolved"]
                if rank_meta.get("note"):
                    response["note"] = rank_meta["note"]          # one plain sentence instead of a silent empty list
                return response
            response = {"results": result_rows, "coverage": cov, "text": _text(cov), "matched": matched,
                        "ms": round((time.perf_counter() - t0) * 1000, 2)}
            if rank_meta.get("resolved"):
                response["rank_resolved"] = rank_meta["resolved"]
            if rank_meta.get("note"):
                response["note"] = rank_meta["note"]
            return response

        if find == "feature" and group_by == "product":
            rows = con.execute(compiled["sql"], compiled["params"]).fetchall()
            groups_map = {}
            for product_id, ref_des, part_id, feature_id in rows:
                bucket = groups_map.setdefault(product_id, {})
                entry = bucket.setdefault(
                    ref_des, {"ref_des": ref_des, "part_id": part_id, "feature_id": feature_id, "count": 0})
                entry["count"] += 1
            for field in compiled["return_fields"]:
                if field in ("part.ref", "count"):
                    continue
                try:
                    prop_id, _role = _resolve_term(field, properties, synonyms)
                except QueryError:
                    continue
                applies_to = properties[prop_id]["applies_to"]
                for bucket in groups_map.values():
                    for entry in bucket.values():
                        entity_id = entry["feature_id"] if applies_to == "feature" else (
                            entry["part_id"] if applies_to == "part" else None)
                        if entity_id is not None:
                            entry[prop_id] = _project_value(con, properties, applies_to, entity_id, prop_id)
            groups = [{"product_id": pid, "parts": list(bucket.values())}
                      for pid, bucket in sorted(groups_map.items())]
            _add_product_info(con, groups)
            cov = coverage(con, compiled["resolved_filters"])
            return {"groups": groups, "coverage": cov, "text": _text(cov),
                    "ms": round((time.perf_counter() - t0) * 1000, 2)}

        if find == "feature":
            rows = con.execute(compiled["sql"], compiled["params"]).fetchall()
            results = [{"feature_id": fid, "part_id": pid} for fid, pid in rows][:limit]
            cov = coverage(con, compiled["resolved_filters"])
            return {"results": results, "coverage": cov, "text": _text(cov),
                    "ms": round((time.perf_counter() - t0) * 1000, 2)}

        # product / interface: a plain id list (no ranking/grouping defined for these yet)
        rows = con.execute(compiled["sql"], compiled["params"]).fetchall()
        ids = [r[0] for r in rows][:limit]
        cov = coverage(con, compiled["resolved_filters"])
        return {"results": [{"id": i} for i in ids], "coverage": cov, "text": _text(cov),
                "ms": round((time.perf_counter() - t0) * 1000, 2)}
    finally:
        if close_con:
            con.close()


# ---------------------------------------------------------------------------
# The catalogue page -- parts and assemblies with their key numbers, both counted, one list's page at a
# time; the families; one part's numbers. Read only, every value a bound parameter, no DDL. The numbers come from the fact
# layer through the read models (compat.parts, the feature facts) exactly as the product page and the part card read them;
# nothing here measures or computes a shown number except counts.
# ---------------------------------------------------------------------------

BROWSE_LIMIT = 25
BROWSE_MAX_LIMIT = 100
BROWSE_SORTS = ("name", "volume_desc", "volume_asc", "length_desc", "used")
BROWSE_FILTER_TERMS = ("family", "bore", "od", "length", "width", "height", "volume", "mass", "holes", "material")
SHAPE_DESIGNS_TOP = 20     # a shape search's assemblies: the designs using one of the 20 closest parts (the studio lists 20)
BLEND_FACTOR = 1.05        # read_models.build_part_card: a cylinder wider than the part itself (x1.05) is a blend, not a bore


def _nzf(value):
    """A stored double as the page shows it: -0.0 -> 0.0 (read_models._nz), None stays None."""
    return None if value is None else float(value) + 0.0


def _visible_owners(con, part_ids, hidden):
    """-> ({part_id: [(product_id, first ref), ...]} over the designs NOT in the trash, product order; {part_ids used only
    by trashed designs}). A part in no design at all is visible with no owners."""
    owners, used = {}, set()
    if not part_ids:
        return owners, set()
    for part_id, product_id, ref in con.execute(
            "SELECT part_id, product_id, MIN(ref_des) FROM occurrence WHERE part_id = ANY(%s) "
            "GROUP BY part_id, product_id ORDER BY part_id, product_id", (list(part_ids),)).fetchall():
        used.add(part_id)
        if product_id not in hidden:
            owners.setdefault(part_id, []).append((product_id, ref))
    return owners, {p for p in used if p not in owners}


def _diameters(con, part_ids, envelopes):
    """{part_id: {"bore": [{"dia_mm", "count"}], "od": [...]}}, largest first: the part's bore and outside-diameter features
    grouped to 0.01 mm exactly as the part card groups its cylinders (read_models.build_part_card: `round(dia, 2)`, and a
    cylinder wider than the part x1.05 is a blend, left out)."""
    dias = _feature_dias_bulk(con, part_ids, ["bore", "od"])
    out = {}
    for pid in part_ids:
        size = max((x or 0) for x in envelopes.get(pid) or (0,))
        entry = {}
        for role in ("bore", "od"):
            groups = {}
            for d in dias.get((pid, role), []):
                if not d or (size and d > size * BLEND_FACTOR):
                    continue
                k = round(d, 2)
                groups[k] = groups.get(k, 0) + 1
            entry[role] = [{"dia_mm": k + 0.0, "count": n} for k, n in sorted(groups.items(), key=lambda kv: -kv[0])]
        out[pid] = entry
    return out


def _design_list(members, owners, names):
    """[{product_id, name, ref}] for a part (or a group of identical parts, the shown one first): each design once, with the
    ref of the first listed part it uses; ordered by name."""
    seen = {}
    for pid in members:
        for product_id, ref in owners.get(pid, ()):
            if product_id not in seen:
                seen[product_id] = {"product_id": product_id, "name": names.get(product_id) or product_id, "ref": ref}
    return sorted(seen.values(), key=lambda d: ((d["name"] or "").lower(), d["product_id"]))


def _browse_request(request):
    """Validate the page's request -> (pairs, rank, words, kind, same, sort, offset, limit). QueryError on a bad one."""
    if not isinstance(request, dict):
        raise QueryError("not a valid request")
    pairs = _where_items(request.get("where"))
    filters = request.get("filters") or {}
    if not isinstance(filters, dict):
        raise QueryError("filters must be an object")
    for key, value in filters.items():
        if key not in BROWSE_FILTER_TERMS:
            raise QueryError("unknown filter %r" % (key,))
        if key in ("family", "material"):
            if not isinstance(value, str) or not value:
                raise QueryError("%s must be a name" % key)
        else:
            ends = [value.get("min"), value.get("max")] if isinstance(value, dict) else None
            if not ends or any(x is not None and (isinstance(x, bool) or not isinstance(x, (int, float))) for x in ends) \
                    or all(x is None for x in ends):
                raise QueryError("a size filter is {min, max} in numbers")
        pairs.append((key, value))
    # material equals, whatever the case: stored names are materials.json's (lower case), "material Steel" finds "steel"
    pairs = [(k, v.strip().lower() if k in ("material", "part.material") and isinstance(v, str) else v) for k, v in pairs]
    rank = request.get("rank") or None
    if rank is not None and not isinstance(rank, dict):
        raise QueryError("rank must be an object")
    if rank is not None and rank.get("method") in ("d2", "moments"):
        raise QueryError("shape-similarity ranking is not available")
    words = request.get("words") or None
    if words is not None and (not isinstance(words, list) or not all(isinstance(w, str) and w for w in words)):
        raise QueryError("words must be a list of words")
    kind = request.get("kind") or "parts"
    same = request.get("same") or "once"
    sort = request.get("sort") or "name"
    if kind not in ("parts", "assemblies") or same not in ("once", "all") or sort not in BROWSE_SORTS:
        raise QueryError("kind, same or sort is not one of the choices")
    try:
        offset = max(int(request.get("offset") or 0), 0)
        limit = min(max(int(request.get("limit") or BROWSE_LIMIT), 1), BROWSE_MAX_LIMIT)
    except (TypeError, ValueError):
        raise QueryError("offset and limit must be integers")
    return pairs, rank, words, kind, same, sort, offset, limit


def browse(con, request, hidden=()):
    """The catalogue page's search (catalogue.q.browse). request:
        where    the search box's conditions (the studio's phrase reading: a dict, or [term, value] pairs)
        filters  the page's filters, ANDed with `where`: {"family": name, "material": name,
                 "bore"|"od"|"length"|"width"|"height"|"volume"|"mass"|"holes": {"min", "max"}} (either end may be left out)
        rank     any rank run() takes ("d2" / "moments" are not available); the list keeps the rank's order
        words    the box's plain words: designs whose name holds every word come first (the studio's mergeNames rule)
        kind     "parts" (default) | "assemblies": which list's page is returned; both lists are counted
        same     "once" (default: identical shapes, one geom_hash, shown once with every design that uses any of them) | "all"
        sort     name (default) | volume_desc | volume_asc | length_desc | used   (a ranked search keeps its rank)
        offset, limit (25, at most 100)
    `hidden`: designs in the trash; a part used only by them is left out, as the studio search leaves it out.
    -> {"parts": {"total", "parts", "offset", "limit", "items"}, "assemblies": {"total", "offset", "limit", "items"},
        "named" (up to 5 designs named by every word), "kind", "same", "sort", "ranked", "coverage", "text", "ms"
        [, "reference", "note"]}"""
    t0 = time.perf_counter()
    hidden = set(hidden or ())
    con.execute("SET statement_timeout = '5s'")
    pairs, rank, words, kind, same, sort, offset, limit = _browse_request(request)
    compiled = compile_request({"find": "part", "where": pairs, "limit": MAX_LIMIT}, con)
    cand = [r[0] for r in con.execute(compiled["sql"], compiled["params"]).fetchall()]

    # every candidate's label, sizes for sorting, identical-shape key (compat.parts: the read model the product page reads)
    base = {}
    if cand:
        for pid, label, vol, length, gh in con.execute(
                "SELECT p.part_id, p.label, p.volume_mm3, p.bbox_a_mm, pr.geom_hash FROM compat.parts p "
                "JOIN part_revision pr ON pr.part_id = p.part_id WHERE p.part_id = ANY(%s)", (cand,)).fetchall():
            base[pid] = {"label": label, "volume": vol, "length": length, "hash": gh}
    hashes = sorted({b["hash"] for b in base.values() if b["hash"]})
    members = {}                                   # geom_hash -> every part with that shape (the whole catalogue)
    if hashes:
        for pid, gh in con.execute("SELECT part_id, geom_hash FROM part_revision WHERE geom_hash = ANY(%s) ORDER BY part_id",
                                   (hashes,)).fetchall():
            members.setdefault(gh, []).append(pid)
    everyone = set(cand) | {p for ms in members.values() for p in ms}
    owners, trashed = _visible_owners(con, everyone, hidden)
    names = dict(con.execute("SELECT product_id, name FROM product").fetchall())
    cand = [p for p in cand if p in base and p not in trashed]
    for gh in members:
        members[gh] = [p for p in members[gh] if p not in trashed]

    # the parts' order: the rank's, else the chosen sort (ties by name, then part id)
    scores, meta, ranked = {}, {}, bool(rank)
    by_name = sorted(cand, key=lambda p: ((base[p]["label"] or "").lower(), p))
    if ranked:
        rows, _matched, meta = _part_results(con, cand, rank, MAX_LIMIT, compiled["part_order"], filtered=True)
        order = [r["part_id"] for r in rows if r["part_id"] in base]
        if rank.get("like"):                      # a shape search's distance is shown; a "near" score is only an order
            scores = {r["part_id"]: r["score"] for r in rows}
    else:
        order = by_name

    # rows: one per shape (same="once", the first part of the order shows it) or one per part
    def group_of(pid):
        h = base[pid]["hash"]
        return (members.get(h) if h else None) or [pid]
    out_rows, seen = [], set()
    for pid in order:
        key = (base[pid]["hash"] or "part:" + pid) if same == "once" else pid
        if key in seen:
            continue
        seen.add(key)
        group = group_of(pid)
        shown_members = [pid] + [p for p in group if p != pid] if same == "once" else [pid]
        out_rows.append({"part_id": pid, "members": shown_members, "same_shape": max(len(group) - 1, 0),
                         "designs": _design_list(shown_members, owners, names)})
    if not ranked and sort != "name":
        def size(pid, k):
            v = base[pid][k]
            return (v is None, -(v or 0))
        keys = {"volume_desc": lambda r: size(r["part_id"], "volume"),
                "volume_asc": lambda r: (base[r["part_id"]]["volume"] is None, base[r["part_id"]]["volume"] or 0),
                "length_desc": lambda r: size(r["part_id"], "length"),
                "used": lambda r: -len(r["designs"])}
        out_rows.sort(key=keys[sort])             # stable: ties keep the name order

    # the assemblies: designs using a matching part; plain words put the designs named by every word first
    no_conditions = not pairs and not ranked and not words
    named = []
    if no_conditions:
        hit_order = sorted((p for p in names if p not in hidden), key=lambda p: ((names[p] or "").lower(), p))
        hits = {}
    else:
        hits = {}
        for pid in (order[:SHAPE_DESIGNS_TOP] if ranked else order):
            for product_id, _ref in owners.get(pid, ()):
                hits.setdefault(product_id, set()).add(pid)
        if ranked:
            hit_order = list(hits)                # first appearance = closest part first
        else:
            hit_order = sorted(hits, key=lambda p: ((names.get(p) or "").lower(), p))
        if words:
            conds = " AND ".join(["name ILIKE %s"] * len(words))
            named = [r[0] for r in con.execute("SELECT product_id FROM product WHERE " + conds,
                                               ["%" + _like_escape(w) + "%" for w in words]).fetchall()]
            named = sorted((p for p in named if p not in hidden), key=lambda p: ((names.get(p) or "").lower(), p))
            hit_order = named + [p for p in hit_order if p not in set(named)]
    answer = {"kind": kind, "same": same, "sort": "rank" if ranked else sort, "ranked": ranked,
              "parts": {"total": len(out_rows), "parts": len(order), "offset": offset, "limit": limit, "items": []},
              "assemblies": {"total": len(hit_order), "offset": offset, "limit": limit, "items": []},
              # the designs named by every word (first on the assemblies list), shown above the parts too: typing a design's
              # name finds it at once
              "named": [{"product_id": p, "name": names.get(p), "revision": None} for p in named[:5]]}
    if answer["named"]:
        revs = dict(con.execute("SELECT product_id, revision FROM product WHERE product_id = ANY(%s)", (named[:5],)).fetchall())
        for d in answer["named"]:
            d["revision"] = revs.get(d["product_id"])
    named = set(named)

    if kind == "parts":
        page = out_rows[offset:offset + limit]
        ids = [r["part_id"] for r in page]
        facts = {}
        if ids:
            cur = con.cursor(row_factory=dict_row)
            cur.execute("SELECT part_id, label, family, volume_mm3, area_mm2, bbox_a_mm, bbox_b_mm, bbox_c_mm "
                        "FROM compat.parts WHERE part_id = ANY(%s)", (ids,))
            facts = {r["part_id"]: r for r in cur.fetchall()}
        env = {p: (facts[p]["bbox_a_mm"], facts[p]["bbox_b_mm"], facts[p]["bbox_c_mm"]) for p in facts}
        dias = _diameters(con, ids, env)
        for r in page:
            f = facts.get(r["part_id"])
            if f is None:
                continue
            answer["parts"]["items"].append({
                "part_id": r["part_id"], "label": f["label"], "family": f["family"],
                "volume_mm3": _nzf(f["volume_mm3"]), "area_mm2": _nzf(f["area_mm2"]),
                "envelope_mm": [_nzf(f["bbox_a_mm"]), _nzf(f["bbox_b_mm"]), _nzf(f["bbox_c_mm"])],
                "bores": dias[r["part_id"]]["bore"], "ods": dias[r["part_id"]]["od"],
                "designs": r["designs"], "same_shape": r["same_shape"],
                "distance": scores.get(r["part_id"])})
    else:
        page = hit_order[offset:offset + limit]
        if page:
            got = {r[0]: r for r in con.execute(
                "SELECT p.product_id, p.name, p.revision, "
                "(SELECT COUNT(DISTINCT o.ref_des) FROM occurrence o WHERE o.product_id = p.product_id), "
                "(SELECT COUNT(*) FROM occurrence o WHERE o.product_id = p.product_id), "
                "(SELECT COUNT(*) FROM interface i WHERE i.product_id = p.product_id) "
                "FROM product p WHERE p.product_id = ANY(%s)", (page,)).fetchall()}
            for pid in page:
                g = got.get(pid)
                if g is None:
                    continue
                answer["assemblies"]["items"].append({
                    "product_id": pid, "name": g[1], "revision": g[2], "parts": g[3], "pieces": g[4],
                    "interfaces": g[5], "matching_parts": len(hits.get(pid, ())), "name_matches": pid in named})

    cov = coverage(con, compiled["resolved_filters"])
    cov["missing"].update(meta.get("sig_missing") or {})
    if hidden:   # what the page searched leaves out the hidden designs and the parts only they use
        cov["products_searched"] = con.execute("SELECT COUNT(*) FROM product WHERE NOT (product_id = ANY(%s))",
                                               (list(hidden),)).fetchone()[0]
        all_parts = [r[0] for r in con.execute("SELECT part_id FROM part_revision").fetchall()]
        cov["parts_searched"] = len(all_parts) - len(_visible_owners(con, all_parts, hidden)[1])
    answer["coverage"] = cov
    answer["text"] = "%d products, %d parts searched" % (cov["products_searched"], cov["parts_searched"])
    if meta.get("resolved"):
        ref_id = meta["resolved"]["part_id"]
        row = con.execute("SELECT label FROM part_revision WHERE part_id = %s", (ref_id,)).fetchone()
        answer["reference"] = {"part_id": ref_id, "label": row[0] if row else None}
    if meta.get("note"):
        answer["note"] = meta["note"]
    answer["ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return answer


def families(con, hidden=()):
    """catalogue.q.families: {"families": [{"family", "parts"}] (families that have parts, by name), "parts", "designs"};
    parts used only by designs in the trash are not counted, designs in the trash neither."""
    hidden = set(hidden or ())
    rows = con.execute("SELECT pr.part_id, pm.class_id FROM part_revision pr "
                       "JOIN part_master pm ON pm.part_no = pr.part_no").fetchall()
    _owners, trashed = _visible_owners(con, [r[0] for r in rows], hidden)
    counts = {}
    for pid, fam in rows:
        if pid not in trashed:
            counts[fam] = counts.get(fam, 0) + 1
    designs = [r[0] for r in con.execute("SELECT product_id FROM product").fetchall() if r[0] not in hidden]
    return {"families": [{"family": f, "parts": n} for f, n in sorted(counts.items(), key=lambda kv: (kv[0] or ""))],
            "parts": sum(counts.values()), "designs": len(designs)}


AXIS_ALIGNED = 0.999       # a chamfer's axis along x, y or z: its stored face box gives its diameter and length


def _feature_rows(con, part_id):
    """One part's cylinder and cone features from the fact layer (the lookups compat.features makes, read per part:
    compat.features numbers all 32,000 features per request). -> [{kind, role, dia, length, angle, axis (3), box (6)}]
    in feature order."""
    def num(prop):
        return ("(SELECT value_num FROM fact WHERE entity_kind = 'feature' AND entity_id = ft.feature_id "
                "AND property_id = '%s' AND valid_to IS NULL)" % prop)

    def vec(prop, i):
        return ("(SELECT value_vec[%d] FROM fact WHERE entity_kind = 'feature' AND entity_id = ft.feature_id "
                "AND property_id = '%s' AND valid_to IS NULL)" % (i, prop))
    cols = [num("feat.diameter"), num("feat.length"), num("feat.semi_angle_legacy")] + \
        [vec("feat.axis_legacy", i) for i in (1, 2, 3)] + [vec("feat.bbox", i) for i in range(1, 7)]
    rows = con.execute("SELECT ft.kind, ft.role, %s FROM feature ft WHERE ft.part_id = %%s AND ft.kind IN ('cylinder', 'cone') "
                       "ORDER BY ft.ordinal" % ", ".join(cols), (part_id,)).fetchall()
    return [{"kind": r[0], "role": r[1], "dia": r[2], "length": r[3], "angle": r[4], "axis": r[5:8], "box": r[8:14]}
            for r in rows]


def _cylinder_lengths(rows, size):
    """{"bore"|"od": {dia rounded to 0.01: [lengths]}}: the stored lengths (`feat.length`) of each bore and outside-diameter
    group of the part card's rule (0.01 mm; a cylinder wider than the part x1.05 is a blend), rounded to 0.1 mm, distinct,
    longest first."""
    out = {"bore": {}, "od": {}}
    for r in rows:
        if r["kind"] != "cylinder" or r["role"] not in out or not r["dia"] or (size and r["dia"] > size * BLEND_FACTOR):
            continue
        g = out[r["role"]].setdefault(round(r["dia"], 2), set())
        if r["length"] is not None:
            g.add(round(r["length"], 1) + 0.0)
    return {role: {k: sorted(v, reverse=True) for k, v in groups.items()} for role, groups in out.items()}


def _chamfer_groups(rows):
    """Chamfers (cone faces): their angle (the stored semi-angle), and, where the cone's axis runs along x, y or z and its
    face goes all round (the two cross sizes of its stored face box agree within 0.01 mm), its diameter (that cross size)
    and its length (the box's size along the axis). Grouped by angle (0.1 deg), diameter (0.01 mm) and length (0.01 mm),
    largest diameter first. -> [{"angle_deg", "dia_mm", "length_mm", "count"}] (dia/length None when not derivable)."""
    groups = {}
    for r in rows:
        if r["kind"] != "cone" or r["role"] != "chamfer":
            continue
        angle = None if r["angle"] is None else round(abs(r["angle"]), 1) + 0.0
        dia = length = None
        ax, b = r["axis"], r["box"]
        if all(v is not None for v in ax) and all(v is not None for v in b):
            k = max(range(3), key=lambda i: abs(ax[i]))
            if abs(ax[k]) >= AXIS_ALIGNED:
                sizes = [b[2 * i + 1] - b[2 * i] for i in range(3)]
                cross = [sizes[i] for i in range(3) if i != k]
                if abs(cross[0] - cross[1]) <= 0.01:
                    dia, length = round(max(cross), 2) + 0.0, round(sizes[k], 2) + 0.0
        key = (angle, dia, length)
        groups[key] = groups.get(key, 0) + 1
    return [{"angle_deg": a, "dia_mm": d, "length_mm": l, "count": n}
            for (a, d, l), n in sorted(groups.items(), key=lambda kv: (-(kv[0][1] or -1), -(kv[0][2] or -1), kv[0][0] or 0))]


def part_facts(con, part_id, hidden=()):
    """catalogue.q.catpart: one part's numbers for the catalogue page's part view -- None when there is no such part, or
    it is used only by designs in the trash.
    -> {part_id, label, family, material, volume_mm3, area_mm2, envelope_mm, faces, edges, surfaces {plane, cylinder, cone,
        sphere, torus, bspline, other}, bores, ods, chamfers, designs [{product_id, name, ref}], identical [{part_id, label,
        designs}],
        box_mm [xmin, xmax, ymin, ymax, zmin, zmax], centre_mm [x, y, z], moments_mm5 [3] and
        gyration_mm [3] (per unit density), topology {solids, shells, faces, edges, vertices}, ratios {aspect_1, aspect_2,
        fill, compactness}, each bore / outside diameter group's lengths_mm, chamfer_list [{angle_deg, dia_mm, length_mm,
        count}]}
    Every value is the catalogue's own (compat.parts, the feature facts); nothing is measured here."""
    hidden = set(hidden or ())
    con.execute("SET statement_timeout = '5s'")
    cur = con.cursor(row_factory=dict_row)
    cur.execute("SELECT p.*, pr.geom_hash FROM compat.parts p JOIN part_revision pr ON pr.part_id = p.part_id "
                "WHERE p.part_id = %s", (part_id,))
    f = cur.fetchone()
    if f is None:
        return None
    group = [part_id]
    if f["geom_hash"]:
        group += [r[0] for r in con.execute("SELECT part_id FROM part_revision WHERE geom_hash = %s AND part_id <> %s "
                                            "ORDER BY part_id", (f["geom_hash"], part_id)).fetchall()]
    owners, trashed = _visible_owners(con, group, hidden)
    if part_id in trashed:
        return None
    names = dict(con.execute("SELECT product_id, name FROM product").fetchall())
    labels = dict(con.execute("SELECT part_id, label FROM part_revision WHERE part_id = ANY(%s)", (group,)).fetchall())
    env = {part_id: (f["bbox_a_mm"], f["bbox_b_mm"], f["bbox_c_mm"])}
    dias = _diameters(con, [part_id], env)[part_id]
    chamfers = con.execute("SELECT COUNT(*) FROM feature WHERE part_id = %s AND role = 'chamfer'", (part_id,)).fetchone()[0]
    as_int = lambda v: None if v is None else int(v)
    identical = sorted(({"part_id": p, "label": labels.get(p), "designs": _design_list([p], owners, names)}
                        for p in group[1:] if p not in trashed),
                       key=lambda x: ((x["label"] or "").lower(), x["part_id"]))
    feats = _feature_rows(con, part_id)
    lengths = _cylinder_lengths(feats, max((x or 0) for x in env[part_id]))
    out = {"part_id": part_id, "label": f["label"], "family": f["family"], "material": f["material"],
           "volume_mm3": _nzf(f["volume_mm3"]), "area_mm2": _nzf(f["area_mm2"]),
           "envelope_mm": [_nzf(f["bbox_a_mm"]), _nzf(f["bbox_b_mm"]), _nzf(f["bbox_c_mm"])],
           "faces": as_int(f["n_faces"]), "edges": as_int(f["n_edges"]),
           "surfaces": {"plane": as_int(f["n_plane"]), "cylinder": as_int(f["n_cylinder"]), "cone": as_int(f["n_cone"]),
                        "sphere": as_int(f["n_sphere"]), "torus": as_int(f["n_torus"]),
                        "bspline": as_int(f["n_bspline"]), "other": as_int(f["n_other_surf"])},
           # the diameter groups, each with the lengths of its cylinders
           "bores": [dict(g, lengths_mm=lengths["bore"].get(g["dia_mm"], [])) for g in dias["bore"]],
           "ods": [dict(g, lengths_mm=lengths["od"].get(g["dia_mm"], [])) for g in dias["od"]],
           "chamfers": chamfers, "designs": _design_list([part_id], owners, names), "identical": identical,
           # the rest of the part's record
           "box_mm": [_nzf(f[k]) for k in ("bbox_xmin", "bbox_xmax", "bbox_ymin", "bbox_ymax", "bbox_zmin", "bbox_zmax")],
           "centre_mm": [_nzf(f[k]) for k in ("com_x_mm", "com_y_mm", "com_z_mm")],
           "moments_mm5": [_nzf(f[k]) for k in ("moment_1", "moment_2", "moment_3")],
           "gyration_mm": [_nzf(f[k]) for k in ("rgyr_1", "rgyr_2", "rgyr_3")],
           "topology": {"solids": as_int(f["n_solids"]), "shells": as_int(f["n_shells"]), "faces": as_int(f["n_faces"]),
                        "edges": as_int(f["n_edges"]), "vertices": as_int(f["n_vertexes"])},
           "ratios": {k: _nzf(f[k]) for k in ("aspect_1", "aspect_2", "fill", "compactness")},
           "chamfer_list": _chamfer_groups(feats)}
    out.update(_part_card(con, part_id, set(out_d["product_id"] for out_d in out["designs"])))
    return out


def _part_card(con, part_id, shown_designs):
    """The part's record (framework/catalog/pg/partcards.py, table part_card) for the part view.
    -> {mass {kg, assumed, material, density_kg_m3}, exact_size_mm, exact_box_mm,
        connections {hole_patterns, keyways, flats, threads_note, ...}, used_in [{product_id, design, refs, count, fits,
        contacts, fits_total, contacts_total}] (designs in the trash left out), family_params [{name, value, unit}]}.
    A database without the record's tables (the step never ran), or a part with no record yet, answers {} and the page
    shows what it showed before."""
    if con.execute("SELECT to_regclass('part_card')").fetchone()[0] is None:
        return {}
    row = con.execute("SELECT facts_json, facts_hash FROM part_card WHERE part_id = %s", (part_id,)).fetchone()
    if row is None or not row[1]:
        return {}
    rec = json.loads(row[0])
    tb = con.execute("SELECT value_vec FROM fact WHERE entity_kind = 'part' AND entity_id = %s AND property_id = "
                     "'part.tight_bbox' AND valid_to IS NULL", (part_id,)).fetchone()
    conn = dict(rec.get("connections") or {})
    conn.pop("totals", None)
    out = {"mass": {"kg": rec.get("mass_kg"), "assumed": rec["material"]["assumed"], "material": rec["material"]["name"],
                    "density_kg_m3": rec["material"]["density_kg_m3"]},
           "exact_size_mm": rec["size_mm"] if rec.get("size_exact") else None,
           "exact_box_mm": [_nzf(x) for x in tb[0]] if tb and tb[0] else None,
           "connections": conn,
           "used_in": [d for d in rec.get("used_in") or [] if d.get("product_id") in shown_designs],
           "family_params": rec.get("family_params") or []}
    return out


def part_shape(con, part_id, hidden=()):
    """catalogue.q.partshape: where the part's exact shape can be read, for its own 3D view and STEP file
    (framework/catalog/pg/signatures.py shape_sources: [[path, object], ...], fastest first), and its label (the
    download's name). None when there is no such part or it is used only by designs in the trash; `sources` is empty when
    no shape was kept. The paths go to the gateway and the geometry service only, never to the page."""
    from framework.catalog.pg import signatures
    row = con.execute("SELECT label FROM part_revision WHERE part_id = %s", (part_id,)).fetchone()
    if row is None:
        return None
    _owners, trashed = _visible_owners(con, [part_id], set(hidden or ()))
    if part_id in trashed:
        return None
    return {"part_id": part_id, "label": row[0], "sources": [[p, o] for p, o in signatures.shape_sources(con, part_id)]}


def design_facts(con, product_id, hidden=()):
    """catalogue.q.catdesign: an assembly's view on the catalogue page -- None when there is no such design or it
    is in the trash. -> {product_id, name, revision, description, parts (its lines: distinct refs, as the page's assembly
    rows count them), pieces, interfaces, lines [{ref, part_id, label, family, count, volume_mm3, envelope_mm}] by ref}."""
    if product_id in set(hidden or ()):
        return None
    con.execute("SET statement_timeout = '5s'")
    head = con.execute("SELECT product_id, name, revision, description FROM product WHERE product_id = %s",
                       (product_id,)).fetchone()
    if head is None:
        return None
    occ = con.execute("SELECT ref_des, part_id, COUNT(*) FROM occurrence WHERE product_id = %s GROUP BY ref_des, part_id "
                      "ORDER BY ref_des, part_id", (product_id,)).fetchall()
    ids = sorted({r[1] for r in occ})
    cur = con.cursor(row_factory=dict_row)
    cur.execute("SELECT part_id, label, family, volume_mm3, bbox_a_mm, bbox_b_mm, bbox_c_mm FROM compat.parts "
                "WHERE part_id = ANY(%s)", (ids,))
    facts = {r["part_id"]: r for r in cur.fetchall()}
    lines = []
    for ref, pid, n in occ:
        fp = facts.get(pid) or {}
        lines.append({"ref": ref, "part_id": pid, "label": fp.get("label"), "family": fp.get("family"), "count": int(n),
                      "volume_mm3": _nzf(fp.get("volume_mm3")),
                      "envelope_mm": [_nzf(fp.get("bbox_a_mm")), _nzf(fp.get("bbox_b_mm")), _nzf(fp.get("bbox_c_mm"))]})
    interfaces = con.execute("SELECT COUNT(*) FROM interface WHERE product_id = %s", (product_id,)).fetchone()[0]
    return {"product_id": head[0], "name": head[1], "revision": head[2], "description": head[3],
            "parts": len({r[0] for r in occ}), "pieces": sum(r[2] for r in occ), "interfaces": interfaces, "lines": lines}


# ---------------------------------------------------------------------------
# find.py plumbing: which source answers, hand-over to the venv
# interpreter, legacy-shaped SELECTs against compat.*, and the grouped "by product" text.
# Everything here that find.py calls before they know the source is stdlib only.
# ---------------------------------------------------------------------------

DEFAULT_MECH_PYTHON = (r"C:\venvs\buildcore\Scripts\python.exe" if os.name == "nt"     # per-OS default
                       else os.path.join(os.path.expanduser("~"), "venvs", "buildcore", "bin", "python"))


def _truthy(value):
    from framework.common.config import truthy
    return truthy(value)


def _trace(script, source, reason):
    if _truthy(os.environ.get("BC_PG_TRACE")):
        print("[catalogue] %s: source=%s (%s)" % (os.path.basename(script), source, reason), file=sys.stderr)


def mech_python():
    """The venv interpreter (MECH_PYTHON, default the product venv) or None when it is missing."""
    path = os.environ.get("MECH_PYTHON") or DEFAULT_MECH_PYTHON
    return path if os.path.isfile(path) else None


def source_choice(sqlite_path, script="find.py"):
    """-> 'postgres' | 'sqlite' | ('handover', interpreter). Never raises.
    BC_PG_OFF -> sqlite; no psycopg here -> hand the whole run to the venv interpreter once
    (BC_PG_REEXEC guards the loop), else sqlite; psycopg present -> postgres only when the
    database answers within 2 s AND its stored fingerprint equals the SQLite catalogue's."""
    from framework.common.config import pg_off
    if pg_off():
        _trace(script, "sqlite", "BC_PG_OFF set")
        return "sqlite"
    if pgdb is None:
        py = None if os.environ.get("BC_PG_REEXEC") else mech_python()
        if py is None:
            _trace(script, "sqlite", "no database driver in this interpreter and no venv to hand over to")
            return "sqlite"
        _trace(script, "handover", "no database driver in this interpreter; running under %s" % py)
        return ("handover", py)
    try:
        from framework.catalog.pg import sync
        current = sync.is_current(sqlite_path)
    except Exception as exc:  # noqa: BLE001 - a reader must never fail because of the mirror
        _trace(script, "sqlite", "mirror check failed: %s" % exc)
        return "sqlite"
    if current:
        _trace(script, "postgres", "shared catalogue reachable and current")
        return "postgres"
    _trace(script, "sqlite", "shared catalogue unreachable or not current")
    return "sqlite"


def handover(interpreter, script, argv):
    """Run `script argv` under `interpreter` in a hidden subprocess; its stdout/stderr bytes are
    passed through unchanged. -> its exit code."""
    import subprocess
    env = dict(os.environ, BC_PG_REEXEC="1")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    done = subprocess.run([interpreter, script] + list(argv), env=env, capture_output=True,
                          creationflags=flags)
    for data, stream in ((done.stdout, sys.stdout), (done.stderr, sys.stderr)):
        if not data:
            continue
        raw = getattr(stream, "buffer", None)
        if raw is not None:
            stream.flush()
            raw.write(data)
            raw.flush()
        else:
            stream.write(data.decode("utf-8", "replace"))
    return done.returncode


def open_connection(dbname=None):
    """An autocommit connection with the 5 s statement timeout."""
    con = pgdb.connect(dbname=dbname, autocommit=True)
    con.execute("SET statement_timeout = '5s'")
    return con


class Row(dict):
    """A dict row that also answers `row[0]` like sqlite3.Row, so legacy code runs unchanged."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)


def compat_rows(con, sql, args=()):
    """Run a legacy-shaped SELECT (qmark placeholders, `compat.*` tables) -> [Row]. The 5 s
    statement timeout applies. The caller supplies any ORDER BY that must reproduce SQLite's row order."""
    con.execute("SET statement_timeout = '5s'")
    cur = con.cursor(row_factory=dict_row)
    cur.execute(sql.replace("%", "%%").replace("?", "%s"), list(args))
    return [Row(r) for r in cur.fetchall()]


def _nearest_dia(dias, target, tol):
    near = [d for d in dias if abs(d - target) <= tol]
    return min(near, key=lambda d: (abs(d - target), d)) if near else None


def part_dia_text(a, dias_by_role):
    """'bore 40.000 mm, od 42.000 mm' for the bore/od filters of the CLI args `a`, or ''."""
    out = []
    for role, target in (("bore", a.bore), ("od", a.od)):
        if target is None:
            continue
        best = _nearest_dia(dias_by_role.get(role, ()), target, a.tol)
        if best is not None:
            out.append("%s %.3f mm" % (role, best))
    return ", ".join(out)


def render_by_product(a, products, n_products, n_parts):
    """The text of `find.py --by-product`, shared by both sources so it cannot differ.
    products: [{"product_id", "name", "revision", "parts": [{"ref_des", "label", "dias": {role: [dia]}}]}]"""
    lines = []
    for prod in sorted(products, key=lambda p: p["product_id"]):
        lines.append("%s  %s  rev %s" % (prod["product_id"], prod["name"] or "",
                                          prod["revision"] if prod["revision"] is not None else "-"))
        for part in sorted(prod["parts"], key=lambda r: r["ref_des"]):
            line = "  %s  %s" % (part["ref_des"], part["label"] or "")
            dia = part_dia_text(a, part["dias"])
            lines.append(line + ("  " + dia if dia else ""))
    if not products:
        lines.append("no products match")
    lines.append("")
    lines.append("%d products, %d parts searched" % (n_products, n_parts))
    return "\n".join(lines)


def by_product(con, a):
    """`find.py --by-product` data from the shared catalogue -> (products, n_products, n_parts)."""
    request = _request_from_cli(a)
    request.pop("rank", None)
    compiled = compile_request(request, con)
    part_ids = [r[0] for r in con.execute(compiled["sql"], compiled["params"]).fetchall()]
    cov = coverage(con, compiled["resolved_filters"])
    products = {}
    if part_ids:
        for r in compat_rows(
                con,
                "SELECT pp.product_id, pr.name, pr.revision, pp.ref_des, pp.part_id, pa.label "
                "FROM compat.product_parts pp JOIN compat.products pr ON pr.product_id = pp.product_id "
                "JOIN compat.parts pa ON pa.part_id = pp.part_id WHERE pp.part_id = ANY(?)", (part_ids,)):
            prod = products.setdefault(r["product_id"], {
                "product_id": r["product_id"], "name": r["name"], "revision": r["revision"], "parts": []})
            prod["parts"].append({"ref_des": r["ref_des"], "label": r["label"], "part_id": r["part_id"],
                                  "dias": {}})
        roles = [role for role, val in (("bore", a.bore), ("od", a.od)) if val is not None]
        dias = {}
        if roles and products:
            wanted = sorted({p["part_id"] for prod in products.values() for p in prod["parts"]})
            for r in compat_rows(con, "SELECT part_id, role, dia_mm FROM compat.features "
                                      "WHERE part_id = ANY(?) AND role = ANY(?)", (wanted, roles)):
                if r["dia_mm"] is not None:
                    dias.setdefault((r["part_id"], r["role"]), []).append(r["dia_mm"])
        for prod in products.values():
            for part in prod["parts"]:
                part["dias"] = {role: dias.get((part["part_id"], role), []) for role in roles}
    return list(products.values()), cov["products_searched"], cov["parts_searched"]


# ---------------------------------------------------------------------------
# CLI -- JSON request, --file, or find.py-style flags; --format text mirrors find.py's own
# layout exactly (same header phrase, same column widths, same indented part_id lines) so a
# caller (or a test) that already knows how to read find.py's output can read this too.
# ---------------------------------------------------------------------------

def _parse_range(s):
    """Ported verbatim from find.py's parse_range (same '-' splitting quirk for a leading
    negative number) -- not our bug to fix here, and every GOLDEN range is positive."""
    if "-" in s[1:]:
        a, b = s.split("-", 1) if not s.startswith("-") else (s, s)
        return float(a), float(b)
    v = float(s)
    return v, v


def _parse_near_spec(s):
    spec = {}
    for tok in s.split(","):
        k, _, v = tok.partition("=")
        spec[k.strip()] = float(v)
    return spec


def _request_from_cli(a):
    where = {}
    if a.family:
        where["part.family"] = a.family
    if a.label:
        where["part.label"] = {"like": a.label}
    if a.material:
        where["part.material"] = {"like": a.material}
    if a.volume:
        lo, hi = _parse_range(a.volume)
        where["part.volume"] = [lo, hi]
    if a.bbox:
        lo, hi = _parse_range(a.bbox)
        where["part.envelope_a"] = [lo, hi]
    if a.bore is not None:
        where["bore"] = {"near": a.bore, "tol": a.tol}
    if a.od is not None:
        where["od"] = {"near": a.od, "tol": a.tol}
    request = {"find": "part", "where": where, "limit": a.limit}
    if a.near:
        request["rank"] = {"near": _parse_near_spec(a.near)}
    elif a.like:
        request["rank"] = {"like": a.like, "method": "ratios"}
    return request


def _render_text_cli(con, a, result):
    if "error" in result:
        return result["error"]
    if a.like:
        cur = con.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM compat.parts WHERE part_id LIKE %s ORDER BY part_id LIMIT 1", (a.like + "%",))
        base = cur.fetchone()
        head = "shape-similar to %s (%s)" % (base["label"], base["part_id"]) if base else "shape-similar to %s" % a.like
    elif a.near:
        head = "nearest to %s" % a.near
    else:
        head = "matches"
    matched = result.get("matched", len(result.get("results", [])))
    lines = ["%s  (%d candidate%s)" % (head, matched, "" if matched == 1 else "s")]
    lines.append("%-7s %-22s %-17s %9s %8s  %s" % ("SCORE", "LABEL", "FAMILY", "VOL mm3", "LEN mm", "WHY"))
    for row in result.get("results", []):
        lines.append("%-7.4f %-22s %-17s %9.0f %8.1f  %s" % (
            row.get("score_raw", row["score"]), (row["label"] or "")[:22], (row["family"] or "")[:17],
            row["volume_mm3"] or 0, row["bbox_a_mm"] or 0, row["why"]))
        lines.append("        %s" % row["part_id"])
    return "\n".join(lines)


def _build_arg_parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("request", nargs="?", help="JSON request string")
    ap.add_argument("--file", help="path to a JSON request file")
    ap.add_argument("--db", default=None, help="database to query (default: the BC_PG_DSN database)")
    ap.add_argument("--format", choices=["json", "text"], default="json")
    ap.add_argument("--family"); ap.add_argument("--label"); ap.add_argument("--material")
    ap.add_argument("--bore", type=float); ap.add_argument("--od", type=float)
    ap.add_argument("--tol", type=float, default=0.5)
    ap.add_argument("--volume"); ap.add_argument("--bbox")
    ap.add_argument("--near"); ap.add_argument("--like")
    ap.add_argument("--limit", type=int, default=10)
    return ap


def main(argv=None):
    ap = _build_arg_parser()
    a = ap.parse_args(argv)
    if a.request:
        request = json.loads(a.request)
    elif a.file:
        with open(a.file, "r", encoding="utf-8") as fh:
            request = json.load(fh)
    else:
        request = _request_from_cli(a)

    con = pgdb.connect(dbname=a.db, autocommit=True)
    try:
        result = run(request, con=con)
        if a.format == "text":
            print(_render_text_cli(con, a, result))
        else:
            print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        con.close()
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
