"""Search the part catalogue.

    find.py --family crankshaft
    find.py --bore 40                     parts with a ~40 mm bore
    find.py --od 39.9 --tol 0.2
    find.py --volume 100000-300000
    find.py --near "bore=40,length=140"   RANKED nearest match, not a filter
    find.py --like <part_id>              shape-similar parts, any size
    find.py --show <part_id>              full record
    find.py --by-product --bore 40        every matching product with its matching parts,
                                          then "N products, M parts searched"
    find.py --query '{"find":"feature","where":{"bore":[38,42]},"group_by":"product"}'
                                          grouped search with coverage, as JSON

--near keys: bore, od, length, width, height, volume, faces, mass_kg
Filters are AND-ed. --near ranks whatever survives the filters.
"""
import argparse
import json
import math
import os
import sqlite3
import sys

DB = os.path.abspath(os.environ.get("BC_CATALOG_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.db"))   # BC_CATALOG_DB, default the file beside this module
STEEL = 7.85e-6  # kg/mm3

NEAR_COLS = {"length": "bbox_a_mm", "width": "bbox_b_mm", "height": "bbox_c_mm",
             "volume": "volume_mm3", "faces": "n_faces", "area": "area_mm2"}
# scale-free, so a 40 mm part can match a 400 mm one of the same form
SHAPE_COLS = ("aspect_1", "aspect_2", "fill", "compactness")


def rows(con, sql, args=()):
    con.row_factory = sqlite3.Row
    return con.execute(sql, args).fetchall()


def parse_range(s):
    if "-" in s[1:]:
        a, b = s.split("-", 1) if not s.startswith("-") else (s, s)
        return float(a), float(b)
    v = float(s)
    return v, v


def build_filters(a):
    where, args = ["1=1"], []
    if a.family:
        where.append("p.family = ?"); args.append(a.family)
    if a.label:
        where.append("p.label LIKE ?"); args.append("%%%s%%" % a.label)
    if a.material:
        where.append("p.material LIKE ?"); args.append("%%%s%%" % a.material)
    if a.volume:
        lo, hi = parse_range(a.volume)
        where.append("p.volume_mm3 BETWEEN ? AND ?"); args += [lo, hi]
    if a.bbox:
        lo, hi = parse_range(a.bbox)
        where.append("p.bbox_a_mm BETWEEN ? AND ?"); args += [lo, hi]
    for role, val in (("bore", a.bore), ("od", a.od)):
        if val is None:
            continue
        where.append("""EXISTS (SELECT 1 FROM features f WHERE f.part_id=p.part_id
                        AND f.role=? AND ABS(f.dia_mm - ?) <= ?)""")
        args += [role, val, a.tol]
    return " AND ".join(where), args


def feature_dias(con, pid, role):
    return [r[0] for r in con.execute(
        "SELECT dia_mm FROM features WHERE part_id=? AND role=?", (pid, role))]


def near_score(con, row, spec):
    """Mean relative error across the requested parameters. Lower is better."""
    errs, detail = [], []
    for key, target in spec.items():
        if key in ("bore", "od"):
            dias = feature_dias(con, row["part_id"], key)
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


def shape_vec(row):
    v = [row[c] for c in SHAPE_COLS]
    m = [row["moment_1"], row["moment_2"], row["moment_3"]]
    if all(x is not None for x in m) and m[0]:
        v += [m[1] / m[0], m[2] / m[0]]
    else:
        v += [None, None]
    return v


def shape_dist(a, b):
    n = d = 0
    for x, y in zip(a, b):
        if x is None or y is None:
            continue
        d += (x - y) ** 2
        n += 1
    return math.sqrt(d / n) if n else None


def _parser():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--family"); ap.add_argument("--label"); ap.add_argument("--material")
    ap.add_argument("--bore", type=float); ap.add_argument("--od", type=float)
    ap.add_argument("--tol", type=float, default=0.5)
    ap.add_argument("--volume"); ap.add_argument("--bbox")
    ap.add_argument("--near"); ap.add_argument("--like"); ap.add_argument("--show")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--by-product", action="store_true")
    ap.add_argument("--query")
    ap.add_argument("-h", "--help", action="store_true")
    return ap


def _fmt(value, spec, width=0):
    """A measured number, or '-' (padded like the number) when the catalogue has none."""
    if value is None:
        return "%-*s" % (width, "-")
    return spec % value


def _show_part(con, a):
    r = rows(con, "SELECT * FROM parts WHERE part_id LIKE ?", (a.show + "%",))
    if not r:
        print("no such part"); return 1
    d = dict(r[0]); raw = json.loads(d.pop("raw_json") or "{}")
    for k, v in d.items():
        if v not in (None, ""):
            print("  %-18s %s" % (k, v))
    print("  features:")
    for f in rows(con, "SELECT * FROM features WHERE part_id=? ORDER BY role, dia_mm", (d["part_id"],)):
        # planes and cones have no diameter and cones no length: print "-" instead of failing
        print("    %-5s dia %s axis(%s,%s,%s) faces=%d len~%s"
              % (f["role"], _fmt(f["dia_mm"], "%-8.3f", 8),
                 _fmt(f["axis_x"], "%.2f"), _fmt(f["axis_y"], "%.2f"), _fmt(f["axis_z"], "%.2f"),
                 f["face_count"] or 0, _fmt(f["extent_mm"], "%.2f")))
    return 0


def _query_module():
    """framework.catalog.query (importable without psycopg; Postgres is optional there)."""
    here = os.path.abspath(globals().get("__file__") or DB)
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from framework.catalog import query
    return query


def _by_product_sqlite(con, a):
    """The --by-product data from this catalogue -> (products, n_products, n_parts)."""
    where, args = build_filters(a)
    part_ids = {r[0] for r in con.execute("SELECT p.part_id FROM parts p WHERE " + where, args)}
    roles = [role for role, val in (("bore", a.bore), ("od", a.od)) if val is not None]
    dias = {}
    for pid, role, dia in con.execute("SELECT part_id, role, dia_mm FROM features WHERE role IN ('bore','od')"):
        if role in roles and pid in part_ids and dia is not None:
            dias.setdefault((pid, role), []).append(dia)
    products = {}
    for pid, name, rev, ref, part_id, label in con.execute(
            """SELECT pp.product_id, pr.name, pr.revision, pp.ref_des, pp.part_id, pa.label
               FROM product_parts pp JOIN products pr ON pr.product_id=pp.product_id
               JOIN parts pa ON pa.part_id=pp.part_id"""):
        if part_id not in part_ids:
            continue
        prod = products.setdefault(pid, {"product_id": pid, "name": name, "revision": rev, "parts": []})
        prod["parts"].append({"ref_des": ref, "label": label,
                              "dias": {role: dias.get((part_id, role), []) for role in roles}})
    n_products = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    n_parts = con.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
    return list(products.values()), n_products, n_parts


NO_GROUPED = "The grouped search is not available right now; use --by-product."


def _cliclient():
    """framework.common.cliclient (stdlib only at import), or None."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(globals().get("__file__") or DB))))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from framework.common import cliclient
        return cliclient
    except Exception:
        return None


def main(argv):
    cc = _cliclient()
    if cc is not None:
        cc.watch_direct("find.py")
        if cc.wanted():       # with BC_SERVICES=1 the question goes to the catalogue service
            return cc.relay("catalogue.find", argv, script=os.path.abspath(globals().get("__file__") or "find.py"))
    ap = _parser()
    a = ap.parse_args(argv)
    if a.help or not argv:
        print(__doc__); return 0
    if not os.path.exists(DB):
        print("no catalogue yet: run ingest.py first"); return 1
    if a.show or not (1 <= a.limit <= 1000):
        return _main_sqlite(argv)   # full records and unusual limits are answered from the local catalogue
    try:
        query = _query_module()
        source = query.source_choice(DB, "find.py")
    except Exception:
        source = "sqlite"
    if isinstance(source, tuple):
        return query.handover(source[1], os.path.abspath(globals().get("__file__") or "find.py"), argv)
    if source == "postgres":
        try:
            text, code = _main_pg(query, a)
        except Exception as exc:  # noqa: BLE001 - never leave the user without an answer
            query._trace("find.py", "sqlite", "shared catalogue failed: %s" % exc)
        else:
            print(text); return code
    return _main_sqlite(argv)


def _main_pg(query, a):
    """The answer from the shared catalogue as (text, exit code). Nothing is printed here, so a
    failure part-way falls back to the local catalogue with no partial output."""
    con = query.open_connection()
    try:
        if a.query:
            try:
                request = json.loads(a.query)
            except ValueError:
                return "That request is not valid JSON.", 1
            result = query.run(request, con=con)
            return json.dumps(result, indent=1, default=str), (1 if "error" in result else 0)
        if a.by_product:
            products, n_products, n_parts = query.by_product(con, a)
            return query.render_by_product(a, products, n_products, n_parts), 0
        if a.like:
            a.near = None   # --like wins over --near, as in the local branch
            base = query.compat_rows(
                con, "SELECT part_id FROM compat.parts WHERE part_id ILIKE ? ORDER BY seq LIMIT 1",
                (a.like + "%",))
            if not base:
                return "no such part", 1
            a.like = base[0]["part_id"]
        result = query.run(query._request_from_cli(a), con=con, raw_scores=True)
        if "error" in result:
            raise RuntimeError(result["error"])
        return query._render_text_cli(con, a, result), 0
    finally:
        con.close()


def _main_sqlite(argv):
    ap = _parser()
    a = ap.parse_args(argv)
    if a.help or not argv:
        print(__doc__); return 0
    if not os.path.exists(DB):
        print("no catalogue yet: run ingest.py first"); return 1
    con = sqlite3.connect(DB)

    if a.show:
        return _show_part(con, a)
    if a.query:
        print(NO_GROUPED); return 0
    if a.by_product:
        query = _query_module()
        products, n_products, n_parts = _by_product_sqlite(con, a)
        print(query.render_by_product(a, products, n_products, n_parts)); return 0

    where, args = build_filters(a)
    sql = "SELECT p.* FROM parts p WHERE " + where
    cands = rows(con, sql, args)

    if a.like:
        base = rows(con, "SELECT * FROM parts WHERE part_id LIKE ?", (a.like + "%",))
        if not base:
            print("no such part"); return 1
        bv = shape_vec(base[0])
        scored = []
        for r in cands:
            if r["part_id"] == base[0]["part_id"]:
                continue
            d = shape_dist(bv, shape_vec(r))
            if d is not None:
                scored.append((d, r, "shape distance"))
        scored.sort(key=lambda t: t[0])
        head = "shape-similar to %s (%s)" % (base[0]["label"], base[0]["part_id"])
    elif a.near:
        spec = {}
        for tok in a.near.split(","):
            k, _, v = tok.partition("=")
            spec[k.strip()] = float(v)
        scored = []
        for r in cands:
            s, why = near_score(con, r, spec)
            scored.append((s, r, why))
        scored.sort(key=lambda t: t[0])
        head = "nearest to %s" % a.near
    else:
        scored = [(0.0, r, "") for r in cands]
        head = "matches"

    print("%s  (%d candidate%s)" % (head, len(cands), "" if len(cands) == 1 else "s"))
    print("%-7s %-22s %-17s %9s %8s  %s" % ("SCORE", "LABEL", "FAMILY", "VOL mm3", "LEN mm", "WHY"))
    for s, r, why in scored[:a.limit]:
        print("%-7.4f %-22s %-17s %9.0f %8.1f  %s"
              % (s, (r["label"] or "")[:22], (r["family"] or "")[:17],
                 r["volume_mm3"] or 0, r["bbox_a_mm"] or 0, why))
        print("        %s" % r["part_id"])
    return 0


if __name__ == "__main__":         # importable without running
    sys.exit(main(sys.argv[1:]))
