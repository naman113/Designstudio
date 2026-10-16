"""A part's complete record, built from the facts the catalogue already stores.

    from framework.catalog.pg import partrecord
    recs = partrecord.build(con, part_ids)          # {part_id: record}; nothing is measured here
    partrecord.write_derived(con, part_id, rec)     # the record's typed facts (source 'derived')
    partrecord.write_extents(con, results)          # the kernel's exact extent and solids (geometry.tight_extent)

The record (one JSON object per part, numbers rounded as shown on the page) holds:
- `name`, `family`; `material` (name, density, `assumed` when the material is the default) and `mass_kg`
  (volume x density, framework/catalog/materials.json);
- `size_mm` (the exact size, sorted largest first, from `part.tight_bbox` once the kernel measured it, else the stored
  envelope, `size_exact` saying which), `volume_mm3`, `area_mm2`;
- `connections`: `bores` and `outside_diameters` (the part card's groups: 0.01 mm, a cylinder wider than the part x1.05 is
  a blend; count and lengths), `hole_patterns` (2+ equal bores on parallel, separate axes: on a circle with its pitch-circle
  diameter, in a grid, or in a row with its pitch), `keyways` (two parallel faces facing each other across a cylinder's
  axis, narrower than its diameter, inside its length; width, and depth from the floor face), `flats` (a face parallel to
  a cylinder's axis that cuts into it; distance across two opposite flats, else from the axis), `chamfers` (angle, count),
  `threads` (none: a thread is modelled as a plain cylinder; `threads_note` says so);
- `family_params`: the values of its family's parameters (dictionary class_property rows with required = true);
- `used_in`: each design that uses it (designs in the trash left out): its references and count, its fits (the partner
  part, both diameters, the clearance, the fit class) and contacts (the partner part, the contact area).
Every number is the catalogue's own (fact rows, feature facts, interfaces) or arithmetic on them (mass = volume x density,
an across-flats distance = the two flats' distances from the axis); the geometry rules are in this file.
"""
import hashlib
import json
import math
import os

from framework.catalog.pg import load as pgload

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
MATERIALS = os.path.join(ROOT, "framework", "catalog", "materials.json")
METHOD_VERSION = "cat4-v1"
SOURCE = pgload.DERIVED_SOURCE
RECORD_VERSION = 1

BLEND = 1.05            # read_models.build_part_card: a cylinder wider than the part itself (x1.05) is a blend
PARALLEL = 0.9995       # |cos| of two axes that are parallel
PERP = 0.005            # |cos| of a plane normal and an axis that are perpendicular
LINE_TOL = 0.02         # mm: two cylinder axes closer than this are one line (one hole); a hole centre off its circle
EDGE_TOL = 0.05         # mm: a face's position checks (inside a cylinder, symmetric about its axis)
MIN_OVERLAP = 0.5       # mm: a face must run at least this far along a cylinder's length to belong to it
MAX_FITS = 12           # fits / contacts listed per design in the record (the count is always given)
MAX_LIST = 12           # entries per connection list in the record; the rest is counted (`<list>_more`)
KEY_WIDTH = 0.5        # a keyway is at most half the diameter wide (DIN 6885: 0.2-0.35 d) ...
KEY_DEPTH = 0.3        # ... and at most 0.3 d deep (shaft seat about 0.12-0.2 d, hub about 0.1 d)
FLAT_MIN = 0.5         # a flat is at least half the radius from the axis (a D-flat or a hex: 0.7-0.95 r); a face
                       # through the axis is a jaw or a half cut, not a flat

FEAT_PROPS = ("feat.diameter", "feat.length", "feat.axis", "feat.axis_legacy", "feat.position", "feat.bbox",
              "feat.semi_angle_legacy", "feat.area")


# ---------------------------------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------------------------------

def r2(v):
    return None if v is None else round(float(v), 2) + 0.0


def r1(v):
    return None if v is None else round(float(v), 1) + 0.0


def sig3(v):
    """Mass to 3 significant figures (as shown)."""
    if v is None or v == 0:
        return v
    d = 2 - int(math.floor(math.log10(abs(v))))
    return round(v, d) + 0.0


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a):
    n = math.sqrt(_dot(a, a))
    return None if n < 1e-12 else (a[0] / n, a[1] / n, a[2] / n)


def _basis(a):
    """Two unit vectors perpendicular to `a` (and to each other); along x/y/z axes when `a` is."""
    k = max(range(3), key=lambda i: abs(a[i]))
    ref = (0.0, 1.0, 0.0) if k == 0 else (1.0, 0.0, 0.0)
    u = _unit(_sub(ref, tuple(_dot(ref, a) * x for x in a)))
    return u, _cross(a, u)


def _box_corners(b):
    return [(x, y, z) for x in (b[0], b[1]) for y in (b[2], b[3]) for z in (b[4], b[5])]


def _span(b, d):
    """The box's span along unit direction d (min, max of its corners' projections)."""
    ps = [_dot(c, d) for c in _box_corners(b)]
    return min(ps), max(ps)


def materials():
    with open(MATERIALS, encoding="utf-8") as fh:
        return json.load(fh)


def density_for(material, mats=None):
    """-> (density kg/m3, material word shown, assumed). Every material whose name is in the catalogue's material text is
    found; a material that is only a part of another's word ('steel' in 'stainless steel') gives way to it. Exactly one
    left: that material, not assumed. None, or two that disagree ('Aluminium bronze'): the default, marked assumed."""
    mats = mats or materials()
    name = (material or "").strip().lower()
    hits = []
    if name and name != "default":
        words = name.replace("-", " ").split()
        for m in mats["materials"]:
            if any(n in words or (" " in n and n in name) for n in m["names"]):
                hits.append(m)
    hits = [m for m in hits if not any(o is not m and m["material"] != o["material"] and m["material"] in o["material"]
                                       for o in hits)]
    if len(hits) == 1:
        return float(hits[0]["density_kg_m3"]), hits[0]["material"], False
    d = mats["default"]
    return float(d["density_kg_m3"]), d["material"], True


def record_hash(rec):
    return hashlib.sha256(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------------------------------
# reading the stored facts
# ---------------------------------------------------------------------------------------------------------------------

def _load_features(con, part_ids):
    """{part_id: [feature dict]} from the feature entities and their current facts."""
    out = {}
    rows = con.execute(
        "SELECT ft.part_id, ft.feature_id, ft.ordinal, ft.kind, ft.role, f.property_id, f.value_num, f.value_vec "
        "FROM feature ft LEFT JOIN fact f ON f.entity_kind = 'feature' AND f.entity_id = ft.feature_id "
        "AND f.valid_to IS NULL AND f.property_id = ANY(%s) WHERE ft.part_id = ANY(%s) ORDER BY ft.part_id, ft.ordinal",
        (list(FEAT_PROPS), list(part_ids))).fetchall()
    cur = {}
    for pid, fid, ordinal, kind, role, prop, num, vec in rows:
        f = cur.get(fid)
        if f is None:
            f = cur[fid] = {"id": fid, "ordinal": ordinal, "kind": kind, "role": role}
            out.setdefault(pid, []).append(f)
        if prop:
            f[prop] = vec if vec is not None else num
    return out


def _base(con, part_ids):
    rows = con.execute(
        "SELECT p.part_id, p.label, p.family, p.material, p.volume_mm3, p.area_mm2, p.bbox_a_mm, p.bbox_b_mm, p.bbox_c_mm "
        "FROM compat.parts p WHERE p.part_id = ANY(%s)", (list(part_ids),)).fetchall()
    out = {r[0]: {"label": r[1], "family": r[2], "material": r[3], "volume": r[4], "area": r[5],
                  "envelope": [r[6], r[7], r[8]]} for r in rows}
    for pid, prop, vec in con.execute(
            "SELECT entity_id, property_id, value_vec FROM fact WHERE entity_kind = 'part' AND valid_to IS NULL "
            "AND property_id = 'part.tight_bbox' AND entity_id = ANY(%s)", (list(part_ids),)).fetchall():
        if pid in out and vec:
            out[pid]["tight_bbox"] = vec
    return out


def true_axis(abs_axis, foot, box):
    """A cylinder's true axis direction from the stored one. The stored axis (`feat.axis_legacy`) is abs() per component,
    so an oblique axis (two or three non-zero components) has 2 or 4 candidate directions. The stored position is the foot
    of the perpendicular from the origin to the TRUE axis line (framework/kernel/features.py: loc - axis * (loc . axis)),
    so it is perpendicular to the true axis; and the face's box centre, less its part along the true axis, lands near that
    foot. The candidate that best meets both is the true axis (overall sign is irrelevant for a line)."""
    a = _unit(tuple(abs_axis))
    if a is None:
        return None
    nz = [i for i in range(3) if abs(a[i]) > 1e-6]
    if len(nz) < 2:
        return a
    cands = []
    for s1 in (1, -1):
        for s2 in ((1, -1) if len(nz) == 3 else (1,)):
            c = list(a)
            c[nz[1]] *= s1
            if len(nz) == 3:
                c[nz[2]] *= s2
            cands.append(tuple(c))
    p = tuple(foot)
    mid = tuple((box[2 * i] + box[2 * i + 1]) / 2 for i in range(3)) if box else None

    def err(c):
        e = abs(_dot(c, p))
        if mid is not None:
            t = _dot(mid, c)
            q = _sub(mid, tuple(t * x for x in c))
            d = _sub(q, p)
            e += math.sqrt(_dot(d, d))
        return e
    return min(cands, key=err)


def _cylinders(feats, size):
    """Cylinders that are not blends, with their axis line (true direction), radius and length span."""
    out = []
    for f in feats:
        if f["kind"] != "cylinder" or f["role"] not in ("bore", "od"):
            continue
        dia = f.get("feat.diameter")
        pos, box = f.get("feat.position"), f.get("feat.bbox")
        ax = f.get("feat.axis") or (true_axis(f["feat.axis_legacy"], pos, box)
                                    if f.get("feat.axis_legacy") and pos else None)
        if not dia or not ax or not pos or not box or (size and dia > size * BLEND):
            continue
        a = _unit(tuple(ax))
        if a is None:
            continue
        lo, hi = _span(box, a)
        # an axis along x, y or z: the box gives the exact span; else the stored length about the box's middle
        if max(abs(x) for x in a) < PARALLEL and f.get("feat.length"):
            mid = (lo + hi) / 2
            lo, hi = mid - f["feat.length"] / 2, mid + f["feat.length"] / 2
        out.append({"id": f["id"], "role": f["role"], "dia": float(dia), "r": float(dia) / 2, "a": a,
                    "p": tuple(pos), "lo": lo, "hi": hi, "length": f.get("feat.length")})
    return out


def _planes(feats):
    out = []
    for f in feats:
        if f["kind"] != "plane":
            continue
        n = f.get("feat.axis_legacy") or f.get("feat.axis")
        pos, box = f.get("feat.position"), f.get("feat.bbox")
        if not n or not pos or not box:
            continue
        n = _unit(tuple(n))
        if n is None:
            continue
        out.append({"id": f["id"], "n": n, "p": tuple(pos), "box": box, "area": f.get("feat.area")})
    return out


# ---------------------------------------------------------------------------------------------------------------------
# connection features
# ---------------------------------------------------------------------------------------------------------------------

def _groups(feats, role, size):
    """The part card's groups for one role: [{dia_mm, count, lengths_mm}] largest first (0.01 mm)."""
    g = {}
    for f in feats:
        if f["kind"] != "cylinder" or f["role"] != role:
            continue
        dia = f.get("feat.diameter")
        if not dia or (size and dia > size * BLEND):
            continue
        k = round(dia, 2)
        e = g.setdefault(k, {"dia_mm": k + 0.0, "count": 0, "lengths": set()})
        e["count"] += 1
        if f.get("feat.length") is not None:
            e["lengths"].add(r1(f["feat.length"]))
    return [{"dia_mm": e["dia_mm"], "count": e["count"], "lengths_mm": sorted(e["lengths"], reverse=True)}
            for _k, e in sorted(g.items(), reverse=True)]


def _lines(cyls):
    """Distinct axis lines of equal-diameter cylinders: [[cyl, ...], ...] (a bore split by a groove is one line)."""
    lines = []
    for c in cyls:
        for ln in lines:
            o = ln[0]
            if abs(_dot(o["a"], c["a"])) >= PARALLEL:
                d = _sub(c["p"], o["p"])
                perp = _sub(d, tuple(_dot(d, o["a"]) * x for x in o["a"]))
                if math.sqrt(_dot(perp, perp)) <= LINE_TOL:
                    ln.append(c)
                    break
        else:
            lines.append([c])
    return lines


def _fit_circle(pts):
    """Least-squares circle through 2D points -> (cx, cy, r, worst distance off the circle) or None."""
    n = len(pts)
    sx = sum(p[0] for p in pts) / n
    sy = sum(p[1] for p in pts) / n
    u = [(p[0] - sx, p[1] - sy) for p in pts]
    suu = sum(x * x for x, _ in u)
    svv = sum(y * y for _, y in u)
    suv = sum(x * y for x, y in u)
    suuu = sum(x ** 3 for x, _ in u)
    svvv = sum(y ** 3 for _, y in u)
    suvv = sum(x * y * y for x, y in u)
    svuu = sum(y * x * x for x, y in u)
    det = suu * svv - suv * suv
    if abs(det) < 1e-9:
        return None
    b1, b2 = 0.5 * (suuu + suvv), 0.5 * (svvv + svuu)
    uc = (b1 * svv - b2 * suv) / det
    vc = (suu * b2 - suv * b1) / det
    cx, cy = uc + sx, vc + sy
    r = math.sqrt(sum((p[0] - cx) ** 2 + (p[1] - cy) ** 2 for p in pts) / n)
    worst = max(abs(math.hypot(p[0] - cx, p[1] - cy) - r) for p in pts)
    return cx, cy, r, worst


def _collinear(pts):
    """-> (direction (dx, dy), sorted positions along it, worst distance off the line) for 2+ points."""
    (x0, y0), (x1, y1) = max(((p, q) for p in pts for q in pts), key=lambda pq: math.hypot(pq[0][0] - pq[1][0],
                                                                                         pq[0][1] - pq[1][1]))
    L = math.hypot(x1 - x0, y1 - y0)
    if L < 1e-9:
        return None
    dx, dy = (x1 - x0) / L, (y1 - y0) / L
    worst = max(abs((p[0] - x0) * dy - (p[1] - y0) * dx) for p in pts)
    ts = sorted((p[0] - x0) * dx + (p[1] - y0) * dy for p in pts)
    return (dx, dy), ts, worst


def _equal(vals, tol=LINE_TOL):
    return bool(vals) and max(vals) - min(vals) <= tol


def _hole_patterns(bores, cyls_all):
    """Hole patterns from bore lines of one diameter. -> [pattern dict]."""
    out = []
    by_dia = {}
    for c in bores:
        by_dia.setdefault(round(c["dia"], 2), []).append(c)
    for dia, cs in sorted(by_dia.items(), reverse=True):
        lines = _lines(cs)
        # lines of one direction only (a pattern's holes are parallel)
        dirs = []
        for ln in lines:
            for d in dirs:
                if abs(_dot(d[0][0]["a"], ln[0]["a"])) >= PARALLEL:
                    d.append(ln)
                    break
            else:
                dirs.append([ln])
        for group in dirs:
            if len(group) < 2:
                continue
            a = group[0][0]["a"]
            u, v = _basis(a)
            pts = [(_dot(ln[0]["p"], u), _dot(ln[0]["p"], v)) for ln in group]
            n = len(pts)
            pat = {"count": n, "hole_dia_mm": dia + 0.0}
            circle = None
            fit = _fit_circle(pts) if n >= 3 else None
            if fit and fit[3] <= LINE_TOL:
                cx, cy, r, _w = fit
                angs = sorted(math.degrees(math.atan2(p[1] - cy, p[0] - cx)) % 360 for p in pts)
                gaps = [angs[i + 1] - angs[i] for i in range(n - 1)] + [angs[0] + 360 - angs[-1]]
                circle = {"kind": "circle", "pcd_mm": r2(2 * r), "equal_spacing": max(gaps) - min(gaps) <= 0.5}
                # the circle's centre on another cylinder's axis: the pattern sits around that bore or diameter
                centre = tuple(cx * u[i] + cy * v[i] for i in range(3))
                for c in sorted(cyls_all, key=lambda c: -c["dia"]):
                    if abs(_dot(c["a"], a)) < PARALLEL or c["dia"] <= dia:
                        continue
                    d = _sub(c["p"], centre)
                    perp = _sub(d, tuple(_dot(d, a) * x for x in a))
                    if math.sqrt(_dot(perp, perp)) <= EDGE_TOL:
                        circle["around"] = {"kind": "bore" if c["role"] == "bore" else "outside diameter",
                                            "dia_mm": r2(c["dia"])}
                        break
            # equally spaced holes around a bore or diameter: a bolt circle, even when they also make a square
            if circle and circle.get("around") and circle["equal_spacing"]:
                pat.update(circle)
                out.append(pat)
                continue
            # a grid: every point on the product of its distinct u's and v's (along the part's own axes)
            us = _distinct([p[0] for p in pts])
            vs = _distinct([p[1] for p in pts])
            if n >= 4 and len(us) >= 2 and len(vs) >= 2 and len(us) * len(vs) == n and                     all(any(abs(p[0] - x) <= LINE_TOL and abs(p[1] - y) <= LINE_TOL for p in pts) for x in us for y in vs):
                pu = [us[i + 1] - us[i] for i in range(len(us) - 1)]
                pv = [vs[i + 1] - vs[i] for i in range(len(vs) - 1)]
                pat.update(kind="grid", rows=len(vs), columns=len(us),
                           spacing_mm=sorted([r2(sum(pu) / len(pu)), r2(sum(pv) / len(pv))], reverse=True),
                           equal_spacing=_equal(pu) and _equal(pv))
                out.append(pat)
                continue
            line = _collinear(pts)
            if line and line[2] <= LINE_TOL:
                ts = line[1]
                gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
                pat.update(kind="row", pitch_mm=r2(sum(gaps) / len(gaps)) if _equal(gaps) else None,
                           span_mm=r2(ts[-1] - ts[0]), equal_spacing=_equal(gaps))
                out.append(pat)
                continue
            if circle:
                pat.update(circle)
                out.append(pat)
                continue
            pat.update(kind="scattered")
            out.append(pat)
    return out


def _distinct(vals, tol=LINE_TOL):
    out = []
    for x in sorted(vals):
        if not out or x - out[-1] > tol:
            out.append(x)
    return out


def _rel_span(box, d, origin):
    lo, hi = _span(box, d)
    o = _dot(origin, d)
    return lo - o, hi - o


def _cuts(cyls, planes):
    """Keyways and flats on each cylinder (see the module doc). -> (keyways, flats).

    For a cylinder (axis a through point c, radius r) the faces parallel to its axis that run along its length are read
    with their signed offset h = (p - c) . n (the face's distance from the axis along its own outward normal n):
    - keyway: walls A, B with opposite normals facing each other (sA < sB along A's normal e), symmetric about the axis,
      width w = sB - sA < 2r, AND its floor face (normal along f = the third direction): on a shaft at 0 < h < r facing out
      (depth r - h), in a bore at |h| > r facing the axis (depth |h| - r); the walls run between the floor and the surface,
      and the floor between the walls;
    - flat: a face cutting into the cylinder with the material on the cylinder's side (a shaft: 0 <= h < r; a bore:
      -r < h < 0) that stays within its chord (+-sqrt(r^2 - h^2) across); two opposite flats give the distance across."""
    keyways, flats, used = [], [], set()
    for c in sorted(cyls, key=lambda c: -c["dia"]):
        if c["r"] < 1.0 or c["hi"] - c["lo"] < MIN_OVERLAP:
            continue
        a, r, o = c["a"], c["r"], c["p"]
        tol = EDGE_TOL + 0.02 * r
        near = []
        for pl in planes:
            if pl["id"] in used or abs(_dot(pl["n"], a)) > PERP:
                continue
            lo, hi = _span(pl["box"], a)
            if min(hi, c["hi"]) - max(lo, c["lo"]) < MIN_OVERLAP:
                continue
            h = _dot(_sub(pl["p"], o), pl["n"])
            if abs(h) > 1.6 * r + tol:
                continue
            g = _unit(_cross(pl["n"], a))
            near.append(dict(pl, h=h, g=g, gspan=_rel_span(pl["box"], g, o)))
        taken = set()
        # keyways
        walls = [p for p in near if p["h"] < 0 and -p["h"] < r]
        for i, A in enumerate(walls):
            if A["id"] in taken:
                continue
            for B in walls[i + 1:]:
                if B["id"] in taken or _dot(A["n"], B["n"]) > -PARALLEL:
                    continue
                e = A["n"]
                sA, sB = _dot(_sub(A["p"], o), e), _dot(_sub(B["p"], o), e)
                w = sB - sA
                if w <= 0 or w > KEY_WIDTH * 2 * r or abs(sA + sB) / 2 > tol:
                    continue
                f = _cross(a, e)
                surf = math.sqrt(max(0.0, r * r - (w / 2) ** 2))
                found = None
                for F in near:
                    if F["id"] in (A["id"], B["id"]) or F["id"] in taken or abs(abs(_dot(F["n"], f)) - 1) > 1 - PARALLEL:
                        continue
                    if c["role"] == "od" and 0 < F["h"] < r - tol / 5:
                        out_dir, depth, lo_f, hi_f = F["n"], r - F["h"], F["h"], surf
                    elif c["role"] == "bore" and F["h"] < 0 and -F["h"] > r + tol / 5:
                        out_dir, depth, lo_f, hi_f = tuple(-x for x in F["n"]), -F["h"] - r, surf, -F["h"]
                    else:
                        continue
                    ok = True
                    for W in (A, B):          # each wall runs between the floor and the surface
                        wl, wh = _rel_span(W["box"], out_dir, o)
                        if wl < lo_f - tol or wh > hi_f + tol:
                            ok = False
                    fl, fh = _rel_span(F["box"], e, o)    # the floor lies between the walls
                    if fl < sA - tol or fh > sB + tol:
                        ok = False
                    if ok:
                        found = (F, depth)
                        break
                if not found or found[1] > KEY_DEPTH * 2 * r:
                    continue
                lo = max(min(_span(A["box"], a)[0], _span(B["box"], a)[0]), c["lo"])
                hi = min(max(_span(A["box"], a)[1], _span(B["box"], a)[1]), c["hi"])
                keyways.append({"on": "shaft" if c["role"] == "od" else "bore", "dia_mm": r2(c["dia"]),
                                "width_mm": r2(w), "depth_mm": r2(found[1]), "length_mm": r2(hi - lo)})
                taken |= {A["id"], B["id"], found[0]["id"]}
                break
        used |= taken
        # flats
        fl = []
        for p in near:
            if p["id"] in taken:
                continue
            if not ((c["role"] == "od" and FLAT_MIN * r <= p["h"] < r - tol / 5) or
                    (c["role"] == "bore" and -r + tol / 5 < p["h"] <= -FLAT_MIN * r)):
                continue
            hw = math.sqrt(max(0.0, r * r - p["h"] ** 2))
            if p["gspan"][0] < -hw - tol or p["gspan"][1] > hw + tol:
                continue
            fl.append(p)
        done = set()
        for i, A in enumerate(fl):
            if A["id"] in done:
                continue
            pair = None
            for B in fl[i + 1:]:
                if B["id"] not in done and _dot(A["n"], B["n"]) <= -PARALLEL:
                    pair = B
                    break
            on = "shaft" if c["role"] == "od" else "bore"
            if pair:
                flats.append({"on": on, "dia_mm": r2(c["dia"]), "count": 2, "across_mm": r1(abs(A["h"]) + abs(pair["h"]))})
                done |= {A["id"], pair["id"]}
            else:
                flats.append({"on": on, "dia_mm": r2(c["dia"]), "count": 1, "from_axis_mm": r1(abs(A["h"]))})
                done.add(A["id"])
        used |= done
    merged = {}
    for f in flats:
        k = (f["on"], f["dia_mm"], f.get("across_mm"), f.get("from_axis_mm"))
        if k in merged:
            merged[k]["count"] += f["count"]
        else:
            merged[k] = dict(f)
    return keyways, list(merged.values())


def _chamfers(feats):
    g = {}
    for f in feats:
        if f["kind"] == "cone" and f["role"] == "chamfer":
            a = f.get("feat.semi_angle_legacy")
            k = None if a is None else r1(abs(a))
            g[k] = g.get(k, 0) + 1
    return [{"angle_deg": k, "count": n} for k, n in sorted(g.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))]


def connections(feats, size):
    cyls = _cylinders(feats, size)
    bores = [c for c in cyls if c["role"] == "bore"]
    keyways, flats = _cuts(cyls, _planes(feats))
    out = {"bores": _groups(feats, "bore", size), "outside_diameters": _groups(feats, "od", size),
           "hole_patterns": _hole_patterns(bores, cyls), "keyways": keyways,
           "flats": sorted(flats, key=lambda f: (-f["count"], -(f["dia_mm"] or 0))),
           "chamfers": _chamfers(feats), "threads": [],
           "threads_note": "threads are not modelled in the shape (a threaded hole or stud shows as a plain diameter)"}
    totals = {"bore_count": sum(b["count"] for b in out["bores"]), "flat_count": sum(f["count"] for f in out["flats"]),
              "hole_patterns": len([p for p in out["hole_patterns"] if p["kind"] != "scattered"]),
              "keyways": len(out["keyways"]), "od_steps": len(out["outside_diameters"]),
              "od_max": max([o["dia_mm"] for o in out["outside_diameters"]], default=None),
              "od_min": min([o["dia_mm"] for o in out["outside_diameters"]], default=None),
              "bore_max": max([b["dia_mm"] for b in out["bores"]], default=None),
              "bore_min": min([b["dia_mm"] for b in out["bores"]], default=None)}
    for k in ("bores", "outside_diameters", "hole_patterns", "keyways", "flats", "chamfers"):
        if len(out[k]) > MAX_LIST:
            out[k + "_more"] = len(out[k]) - MAX_LIST
            out[k] = out[k][:MAX_LIST]
    out["totals"] = totals
    return out


# ---------------------------------------------------------------------------------------------------------------------
# where used
# ---------------------------------------------------------------------------------------------------------------------

def _used_in(con, part_ids, hidden):
    """{part_id: [{design, refs, count, fits, contacts}]} from occurrences and interfaces."""
    occ = con.execute(
        "SELECT o.part_id, o.product_id, p.name, o.ref_des, o.occ_id FROM occurrence o JOIN product p USING (product_id) "
        "WHERE o.part_id = ANY(%s) ORDER BY o.product_id, o.ref_des, o.instance", (list(part_ids),)).fetchall()
    out = {}
    designs = {}
    for pid, prod, name, ref, occ_id in occ:
        if prod in hidden:
            continue
        d = designs.get((pid, prod))
        if d is None:
            d = designs[(pid, prod)] = {"design": name, "product_id": prod, "refs": [], "count": 0, "fits": [],
                                        "contacts": [], "fits_total": 0, "contacts_total": 0}
            out.setdefault(pid, []).append(d)
        d["count"] += 1
        if ref not in d["refs"]:
            d["refs"].append(ref)
    if not designs:
        return out
    prods = sorted({p for _pid, p in designs})
    # partner labels by (product, ref)
    labels = {(prod, ref): lab for prod, ref, lab in con.execute(
        "SELECT o.product_id, o.ref_des, pr.label FROM occurrence o JOIN part_revision pr ON pr.part_id = o.part_id "
        "WHERE o.product_id = ANY(%s)", (prods,)).fetchall()}
    rows = con.execute(
        "SELECT i.product_id, i.kind, i.ref_a, i.ref_b, "
        "(SELECT value_num FROM fact WHERE entity_kind='interface' AND entity_id=i.interface_id AND property_id='iface.nominal_a' AND valid_to IS NULL), "
        "(SELECT value_num FROM fact WHERE entity_kind='interface' AND entity_id=i.interface_id AND property_id='iface.nominal_b' AND valid_to IS NULL), "
        "(SELECT value_num FROM fact WHERE entity_kind='interface' AND entity_id=i.interface_id AND property_id='iface.clearance' AND valid_to IS NULL), "
        "(SELECT value_text FROM fact WHERE entity_kind='interface' AND entity_id=i.interface_id AND property_id='iface.fit_class' AND valid_to IS NULL), "
        "(SELECT value_num FROM fact WHERE entity_kind='interface' AND entity_id=i.interface_id AND property_id='iface.contact_area' AND valid_to IS NULL) "
        "FROM interface i WHERE i.product_id = ANY(%s) ORDER BY i.product_id, i.ordinal", (prods,)).fetchall()
    for (pid, prod), d in designs.items():
        refs = set(d["refs"])
        seen_fit, seen_contact = {}, {}
        for p2, kind, ra, rb, na, nb, cl, fc, area in rows:
            if p2 != prod or (ra not in refs and rb not in refs) or ra == rb:
                continue
            mine_a = ra in refs
            other = rb if mine_a else ra
            olabel = labels.get((prod, other)) or other
            if kind == "cylindrical_fit":
                key = (olabel, r2(na), r2(nb), fc)
                if key in seen_fit:
                    seen_fit[key]["count"] += 1
                    continue
                d["fits_total"] += 1
                fit = {"with": olabel, "shaft_dia_mm": r2(na), "bore_dia_mm": r2(nb),
                       "clearance_mm": None if cl is None else round(cl, 3) + 0.0, "fit": fc,
                       "this_part_is": "shaft" if mine_a else "bore", "count": 1}
                seen_fit[key] = fit
                if len(d["fits"]) < MAX_FITS:
                    d["fits"].append(fit)
            else:
                if olabel in seen_contact:
                    seen_contact[olabel]["faces"] += 1
                    if area:
                        seen_contact[olabel]["area_mm2"] = round(seen_contact[olabel]["area_mm2"] + area)
                    continue
                d["contacts_total"] += 1
                ct = {"with": olabel, "faces": 1, "area_mm2": round(area or 0)}
                seen_contact[olabel] = ct
                if len(d["contacts"]) < MAX_FITS:
                    d["contacts"].append(ct)
        d["fits_total"] = len(seen_fit)
        d["contacts_total"] = len(seen_contact)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------------------------------------------------

def _family_params(con):
    """{class_id: [(property_id, name, unit)]} from class_property rows with required = true."""
    out = {}
    for cid, pid, name, unit in con.execute(
            "SELECT cp.class_id, cp.property_id, p.name, p.unit FROM class_property cp JOIN property p USING (property_id) "
            "WHERE cp.required ORDER BY cp.class_id, p.property_id").fetchall():
        out.setdefault(cid, []).append((pid, name, unit))
    return out


def derived_values(rec):
    """The record's typed facts: {property_id: value} (None = no value; the current fact is closed)."""
    c = rec["connections"]
    t = c["totals"]
    size = rec["size_mm"] if rec.get("size_exact") else None
    return {
        "part.mass": rec["mass_kg"], "part.density": rec["material"]["density_kg_m3"],
        "part.tight_envelope": size,
        "part.tight_envelope_a": size[0] if size else None, "part.tight_envelope_b": size[1] if size else None,
        "part.tight_envelope_c": size[2] if size else None,
        "part.bore_count": t["bore_count"], "part.bore_max": t["bore_max"], "part.bore_min": t["bore_min"],
        "part.od_max": t["od_max"], "part.od_min": t["od_min"], "part.od_steps": t["od_steps"],
        "part.hole_patterns": t["hole_patterns"], "part.keyways": t["keyways"], "part.flats": t["flat_count"],
        "part.connections": c,
    }


def build(con, part_ids, hidden=None):
    """{part_id: record} for the parts that exist. `hidden`: designs in the trash (default: read the trash)."""
    part_ids = list(part_ids)
    if hidden is None:
        try:
            from framework.catalog import trash
            hidden = set(trash.trashed("mechanical"))
        except Exception:
            hidden = set()
    mats = materials()
    base = _base(con, part_ids)
    feats = _load_features(con, list(base))
    used = _used_in(con, list(base), hidden)
    fparams = _family_params(con)
    out = {}
    for pid, b in base.items():
        env = [x or 0.0 for x in b["envelope"]]
        size = max(env) if env else 0.0
        tb = b.get("tight_bbox")
        if tb and len(tb) == 6:
            exact = sorted([tb[1] - tb[0], tb[3] - tb[2], tb[5] - tb[4]], reverse=True)
            size_mm, size_exact = [r1(x) for x in exact], True
        else:
            size_mm, size_exact = [r1(x) for x in env], False
        dens, word, assumed = density_for(b["material"], mats)
        mass = None if b["volume"] is None else sig3(b["volume"] * dens * 1e-9)
        rec = {"v": RECORD_VERSION, "name": b["label"], "family": b["family"],
               "material": {"name": word, "density_kg_m3": dens, "assumed": assumed},
               "mass_kg": mass, "size_mm": size_mm, "size_exact": size_exact,
               "volume_mm3": None if b["volume"] is None else round(b["volume"]),
               "area_mm2": None if b["area"] is None else round(b["area"]),
               "connections": connections(feats.get(pid, []), size),
               "used_in": used.get(pid, [])}
        dv = derived_values(rec)
        params = []
        for prop, name, unit in fparams.get(b["family"] or "unclassified", []):
            if prop in dv and not isinstance(dv[prop], (dict, list)):
                params.append({"name": name, "value": dv[prop], "unit": unit})
        rec["family_params"] = params
        out[pid] = rec
    return out


def write_derived(con, part_id, rec, now=None):
    """The record's typed facts (source 'derived', method 'cat4-v1'); a fact whose value is gone is closed. -> changed."""
    changed = 0
    dv = derived_values(rec)
    dv["part.family_params"] = {p["name"]: p["value"] for p in rec["family_params"]}
    assumed = rec["material"]["assumed"]
    for prop, value in dv.items():
        if value is None or value == []:
            n = con.execute("UPDATE fact SET valid_to = now() WHERE entity_kind = 'part' AND entity_id = %s AND "
                            "property_id = %s AND valid_to IS NULL AND source = %s", (part_id, prop, SOURCE)).rowcount
            changed += n
            continue
        extra = {}
        if prop == "part.mass":
            extra = {"confidence": 0.5 if assumed else 1, "measure_method": "default density" if assumed else "volume x density"}
        if pgload._put_fact(con, "part", part_id, prop, value, METHOD_VERSION, source=SOURCE, now=now, **extra):
            changed += 1
    return changed


def write_extents(con, results, now=None):
    """The kernel's answers {part_id: {"extent": [6], "solids": [{volume_val, volume_method, volume_unc, area_val,
    area_method, area_unc}]}} -> part.tight_bbox facts and part_solid rows with their solid.* facts. -> counts."""
    n_ext = n_sol = 0
    for pid, r in results.items():
        if not r or not r.get("extent"):
            continue
        if pgload._put_fact(con, "part", pid, "part.tight_bbox", [float(x) for x in r["extent"]], METHOD_VERSION,
                            source=SOURCE, measure_method="exact extent", now=now):
            n_ext += 1
        solids = r.get("solids") or []
        for i, s in enumerate(solids):
            con.execute("INSERT INTO part_solid (part_id, solid_index) VALUES (%s, %s) ON CONFLICT DO NOTHING", (pid, i))
            eid = "%s#s%d" % (pid, i)
            for prop, key, extra in (("solid.volume", "volume_val", {"measure_method": s.get("volume_method"),
                                                                     "uncertainty": s.get("volume_unc")}),
                                     ("solid.volume_method", "volume_method", {}),
                                     ("solid.volume_uncertainty", "volume_unc", {}),
                                     ("solid.area", "area_val", {"measure_method": s.get("area_method"),
                                                                 "uncertainty": s.get("area_unc")}),
                                     ("solid.area_method", "area_method", {}),
                                     ("solid.area_uncertainty", "area_unc", {})):
                pgload._put_fact(con, "solid", eid, prop, s.get(key), METHOD_VERSION, source="measured", now=now, **extra)
            n_sol += 1
        for (idx,) in con.execute("SELECT solid_index FROM part_solid WHERE part_id = %s AND solid_index >= %s",
                                  (pid, len(solids))).fetchall():
            pgload._close_stale_facts(con, "solid", "%s#s%d" % (pid, idx), set(), now=now)
            con.execute("DELETE FROM part_solid WHERE part_id = %s AND solid_index = %s", (pid, idx))
    return {"extents": n_ext, "solids": n_sol}
