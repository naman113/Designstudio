"""Automatic dimensions for a part drawing: the rules that turn the kernel's own feature list into a
dimensioned first-angle sheet. The rules below each carry the measurement behind them (a gearbox and a larger
54-part assembly).

Every number on a sheet is a kernel value: tight extents (`extents.extent`), `features.features` (never changed here: the
golden numbers) and `arc_coverage` (computed beside it, below). A placed number is recorded in `expected` as
(text, kernel value, what); `check.check_part` reads the finished document back and refuses the sheet when a printed
number is unexplained, a placed number is missing, or a text is not the engine's format of its kernel value.

Rules
  turned parts lie along X before drawing (`orient`): a first version drew 48 of the larger assembly's 54 parts with the prismatic rules
      because their axis was Y or Z. Sizes and diameters do not change.
  a hole is called out only when it is a FULL circle (`arc_coverage`): its faces cover 360 degrees (two half faces count), each
      face at least 170. The gearbox casing's big cavities are 286 / 235 degree arcs: a first version called them out as "THRU
      holes" (Ø204, Ø138.5). A probe: CASING Ø100/75/70/62 and 4x Ø19 and Ø15.19 are 360; Ø20 is 180 (a fillet).
  a "bore" is verified before it is used (`verify_roles`): features() decides bore or outside diameter from ONE probe at the
      middle of a face's parameter range, and a face cut by a keyway can put that probe inside the cut. The gearbox
      OUTPUT_SHAFT's Ø48 (the outside diameter of its right end, solid on the axis from x=6 to 84) was
      read as "Ø48 ↧82 bore". Probes at eight angles and three axial places decide by majority; features.py is unchanged.
  a bore is THRU only where it exits into air at both ends (`_thru`): the points just past both ends are outside the solid
      and no coaxial bore of the same group stands there (a step). A stepped bore shows each step's own depth.
  a 180 degree (+-1) cylinder square to a turned part's axis, on a radial line, is a slot end (a keyway): the callout reads
      `SLOT <width> WIDE ↧<depth>` on the view along its axis (a first version printed "Ø14 ↧5.5").
  turned parts: diameters across their own steps (shaft) or on the end view (disc), shoulders as baseline lengths from the
      left end face, the overall length outermost, up to 6 coaxial bores, parallel holes grouped, `ON Ø<pitch>` for 3 or more
      on one circle.
  prismatic parts (castings included): the three envelope sizes between kernel points (never measured off projected edges:
      the finding in extents.py), up to 8 hole groups on the view along their axis, no hole positions, and the note
      "HOLE POSITIONS ARE NOT DIMENSIONED.".
  first angle: the end view stands to the LEFT of the profile, the view from above BELOW the front view.
"""
import math

from framework.kernel import features as kf
from framework.kernel.drawings import check as dcheck
from framework.kernel.drawings import extents
from framework.kernel.drawings import sheet as S

TOL = 1e-3
DIM_GAP_AIR_MM = 1.5          # sheet mm of air between two diameter texts standing side by side
MAX_BORES, MAX_HOLE_GROUPS, MAX_SHOULDERS = 6, 8, 8
SLOT_DEG = 1.0                                   # a slot end covers 180 +- this
# (sheet, scale) tried in order: the first that fits and has no overlapping annotation wins
TRIES = (("A3", 2.0), ("A3", 1.0), ("A3", 0.5), ("A3", 1 / 2.5), ("A2", 0.5), ("A2", 1 / 2.5), ("A3", 0.2), ("A2", 0.2),
         ("A1", 0.2), ("A1", 0.1),
         # long structural members (a 9 m column, 7.5 m purlins) failed "does not fit on the largest
         # sheet"; steelwork is drawn at 1:20 / 1:50 (ISO 5455). Tried last, so every part that fitted before is unchanged.
         ("A1", 1 / 20), ("A0", 1 / 20), ("A1", 1 / 50), ("A0", 1 / 50))
WHY_FIT = "it does not fit on the largest sheet"
fmt = S._fmt                                     # the engine's own number format: every value handed over prints this way


class DrawFail(Exception):
    """A part that cannot be drawn: `why` is one of the plain reasons the page shows."""

    def __init__(self, why):
        super().__init__(why)
        self.why = why


# ---- facts beside features() ---------------------------------------------------------------------------------------------
def _key(f):
    """The key arc_coverage uses for a cylinder feature."""
    return (f["dia_mm"], tuple(f["axis"]), tuple(f["pos"]))


def arc_coverage(shape):
    """{(dia_mm, axis abs, foot): {"sum_deg", "min_face_deg", "faces"}}: the angle each cylinder feature's faces cover, from
    the faces' own parameter bounds (`BRepTools.UVBounds_s`), keyed exactly as `features()` keys its cylinders (dia rounded 3,
    axis abs rounded 3, foot rounded 2). A probe gave the gearbox CASING bores 286 / 235 / 360 degrees."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_SurfaceType
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape
    fm = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fm)
    acc = {}
    for i in range(1, fm.Extent() + 1):
        face = TopoDS.Face_s(fm.FindKey(i))
        surf = BRepAdaptor_Surface(face)
        if surf.GetType() != GeomAbs_SurfaceType.GeomAbs_Cylinder:
            continue
        cyl = surf.Cylinder()
        d = cyl.Axis().Direction()
        ax = (d.X(), d.Y(), d.Z())
        loc = cyl.Location()
        t = loc.X() * ax[0] + loc.Y() * ax[1] + loc.Z() * ax[2]
        foot = (kf._r(loc.X() - ax[0] * t, 2), kf._r(loc.Y() - ax[1] * t, 2), kf._r(loc.Z() - ax[2] * t, 2))
        key = (kf._r(cyl.Radius() * 2, 3), tuple(kf._r(abs(v), 3) for v in ax), foot)
        u0, u1, _v0, _v1 = BRepTools.UVBounds_s(face)
        deg = (u1 - u0) * 180.0 / math.pi
        e = acc.setdefault(key, {"sum_deg": 0.0, "min_face_deg": 1e9, "faces": 0})
        e["sum_deg"] += deg
        e["min_face_deg"] = min(e["min_face_deg"], deg)
        e["faces"] += 1
    return acc


def full_circle(c):
    """A feature whose faces cover 360 degrees, each face at least 170 (two half faces count; two separate fillet arcs
    do not)."""
    return bool(c) and c["sum_deg"] >= 359.0 and c["min_face_deg"] >= 170.0


def _slot_end(c):
    return bool(c) and abs(c["sum_deg"] - 180.0) <= SLOT_DEG and c["faces"] == 1


def _classifier(shape):
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    return BRepClass3d_SolidClassifier(shape)


def _outside(cl, p):
    from OCP.TopAbs import TopAbs_OUT
    from OCP.gp import gp_Pnt
    cl.Perform(gp_Pnt(*p), 1e-6)
    return cl.State() == TopAbs_OUT


def _axial(f, ai):
    return f["bbox"][2 * ai], f["bbox"][2 * ai + 1]


def _thru(cl, f, siblings):
    """THRU when, just past each end of the hole, the air is as wide as the hole: the points 0.5 mm past the end on the axis
    AND at 0.97 of the radius (four directions) are outside the solid, and no coaxial bore of the same group (`siblings`)
    stands there (a step). The ADAPTOR's Ø230 recess, 6 deep, ends on a floor with a smaller opening: its axis point is in
    air (a first version read it as a through hole) but the points at 0.97 r are in the floor, so it reads "↧6"."""
    ai = max(range(3), key=lambda k: abs(f["axis"][k]))
    a0, a1 = _axial(f, ai)
    r = f["dia_mm"] / 2.0
    ring = [(0.0, 0.0)] + [(s * 0.97 * r, 0.0) for s in (1, -1)] + [(0.0, s * 0.97 * r) for s in (1, -1)]
    others = [k for k in range(3) if k != ai]
    for end, sign in ((a0, -1.0), (a1, 1.0)):
        for du, dv in ring:
            p = list(f["pos"])
            p[ai] = end + sign * 0.5
            p[others[0]] += du
            p[others[1]] += dv
            if not _outside(cl, p):
                return False
        for g in siblings:
            if g is f:
                continue
            b0, b1 = _axial(g, ai)
            if b0 - 0.02 <= end <= b1 + 0.02:
                return False
    return True


def _group_cyl(feats):
    groups = {}
    for f in feats:
        if f["kind"] == "cylinder":
            groups.setdefault((tuple(f["axis"]), tuple(f["pos"])), []).append(f)
    return groups


def _main_axis(feats, size):
    """(group key, axis index, distinct outside diameters largest first, turned?): the coaxial cylinder group with the
    largest area; turned when its largest outside diameter equals both cross extents (within 1 %)."""
    groups = _group_cyl(feats)
    if not groups:
        return None, None, [], False
    main = max(groups.items(), key=lambda kv: sum(f["area_mm2"] for f in kv[1]))[0]
    axis, _foot = main
    ai = max(range(3), key=lambda i: abs(axis[i]))
    if abs(axis[ai]) < 0.999:
        return main, None, [], False
    ods = sorted({f["dia_mm"] for f in groups[main] if f["role"] == "od"}, reverse=True)
    others = [i for i in range(3) if i != ai]
    turned = bool(ods) and all(abs(size[i] - ods[0]) <= max(0.01 * ods[0], 0.05) for i in others)
    return main, ai, ods, turned


def _role_votes(cl, f):
    """Votes for what a full-circle "bore" feature is: at eight angles and three places along it, a point a hair inside and a
    hair outside the surface. Inside solid and outside air is an outside diameter; inside air and outside solid is a bore;
    anything else (a point off the face) does not vote."""
    ai = max(range(3), key=lambda k: abs(f["axis"][k]))
    o0, o1 = [k for k in range(3) if k != ai]
    r = f["dia_mm"] / 2.0
    d = max(1e-3, min(0.05, r * 0.05))
    a0, a1 = _axial(f, ai)
    votes = {"od": 0, "bore": 0}
    for k in range(8):
        for frac in (0.2, 0.5, 0.8):
            if abs(votes["od"] - votes["bore"]) >= 4:             # decided by a lead of four (24 probes at most)
                return votes
            c, s = math.cos(2 * math.pi * k / 8), math.sin(2 * math.pi * k / 8)
            pin, pout = list(f["pos"]), list(f["pos"])
            pin[ai] = pout[ai] = a0 + frac * (a1 - a0)
            pin[o0] += (r - d) * c
            pin[o1] += (r - d) * s
            pout[o0] += (r + d) * c
            pout[o1] += (r + d) * s
            inside, outside = not _outside(cl, pin), not _outside(cl, pout)
            if inside and not outside:
                votes["od"] += 1
            elif outside and not inside:
                votes["bore"] += 1
    return votes


def verify_roles(shape, feats, cov):
    """-> (feats with the roles corrected, [the corrections]). Only full-circle cylinders whose kernel role is "bore" are
    checked (a wrong "od" cannot happen: the probe steps inward, so a cut can only turn an outside diameter into a bore)."""
    cl = _classifier(shape)
    out, fixed = [], []
    for f in feats:
        if (f["kind"] == "cylinder" and f["role"] == "bore" and full_circle(cov.get(_key(f)))
                and max(abs(c) for c in f["axis"]) >= 0.999):
            v = _role_votes(cl, f)
            if v["od"] > v["bore"]:
                fixed.append({"dia": f["dia_mm"], "votes": v})
                f = dict(f, role="od")
        out.append(f)
    return out, fixed


def orient(shape, feats, ext):
    """Turned parts lie along +X: a part whose main axis is Y or Z is rotated about the origin (a rigid turn: sizes and
    diameters do not change). -> (shape, rotation or None); the caller takes extents and features of the new shape."""
    size = extents.size(ext)
    main, ai, _ods, turned = _main_axis(feats, size)
    if not turned or ai == 0:
        return shape, None
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf
    t = gp_Trsf()
    if ai == 1:           # Y -> X: a quarter turn about Z
        t.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), -math.pi / 2)
        rot = "Y to X"
    else:                 # Z -> X: a quarter turn about Y
        t.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 0)), math.pi / 2)
        rot = "Z to X"
    return BRepBuilderAPI_Transform(shape, t, True).Shape(), rot


def prepare(shape):
    """The kernel facts a drawing is built from: -> (shape lying the right way, features with verified roles, tight extent,
    arc coverage, rotation or None, role corrections)."""
    ext = extents.extent(shape)
    feats = kf.features(shape, bbox_a_mm=max(extents.size(ext)))
    cov = arc_coverage(shape)
    feats, fixed = verify_roles(shape, feats, cov)
    shape2, rot = orient(shape, feats, ext)
    if rot:
        ext = extents.extent(shape2)
        feats = kf.features(shape2, bbox_a_mm=max(extents.size(ext)))
        cov = arc_coverage(shape2)
        feats, fixed = verify_roles(shape2, feats, cov)
    return shape2, feats, ext, cov, rot, fixed


# ---- the analysis --------------------------------------------------------------------------------------------------------
def analyse(shape, feats, ext, cov):
    """-> a dict describing the part for the rules: kind ("shaft", "disc" or "prismatic"), the main axis, outside steps,
    bores, hole groups, slots and shoulders."""
    size = extents.size(ext)
    groups = _group_cyl(feats)
    cl = _classifier(shape)
    info = {"size": size, "ext": ext, "kind": "prismatic", "axis": None, "slots": [], "holes_any": {}, "holes_par": {},
            "holes_perp": {}, "bores": [], "ods": [], "shoulders": []}

    def full(f):
        return full_circle(cov.get(_key(f)))

    def siblings(f):
        return [g for g in groups.get((tuple(f["axis"]), tuple(f["pos"])), []) if g["role"] == "bore"]

    def hole_key(f):
        thru = _thru(cl, f, siblings(f))
        depth = None if thru else round(f.get("extent_mm") or 0.0, 2)
        return (f["dia_mm"], depth, tuple(f["axis"]), thru)

    main, ai, ods, turned = _main_axis(feats, size)
    if not turned or ai != 0:
        # prismatic: every full-circle bore whose axis is square to a coordinate plane
        for f in feats:
            if f["kind"] != "cylinder" or f["role"] != "bore" or not full(f):
                continue
            if max(abs(c) for c in f["axis"]) < 0.999:
                continue                              # oblique holes: not called out
            info["holes_any"].setdefault(hole_key(f), []).append(f)
        return info
    axis, foot = main
    lo, hi = ext[0], ext[1]
    length = hi - lo
    # outside steps: one dimension per distinct diameter, on its longest occurrence
    best = {}
    for f in groups[main]:
        if f["role"] != "od":
            continue
        a0, a1 = _axial(f, 0)
        s = {"dia": f["dia_mm"], "a0": a0, "a1": a1}
        if s["dia"] not in best or (a1 - a0) > (best[s["dia"]]["a1"] - best[s["dia"]]["a0"]):
            best[s["dia"]] = s
    bores = []
    for f in groups[main]:
        if f["role"] != "bore" or not full(f):
            continue
        a0, a1 = _axial(f, 0)
        thru = _thru(cl, f, siblings(f))
        bores.append({"dia": f["dia_mm"], "depth": f.get("extent_mm"), "a0": a0, "a1": a1, "thru": thru})
    holes_par, holes_perp, slots = {}, {}, {}
    for (ax, ft), fs in groups.items():
        if (ax, ft) == main:
            continue
        for f in fs:
            if f["role"] != "bore":
                continue
            if abs(ax[0]) >= 0.999:                    # parallel to the main axis
                if full(f):
                    holes_par.setdefault(hole_key(f), []).append(f)
            elif abs(ax[0]) <= 0.001 and max(ax[1], ax[2]) >= 0.999:      # square to it, along Y or Z
                if full(f):
                    holes_perp.setdefault(hole_key(f), []).append(f)
                elif _slot_end(cov.get(_key(f))):
                    k = 1 if ax[1] >= 0.999 else 2
                    m = 3 - k                           # the other transverse axis: a radial slot crosses the main axis
                    if abs(f["pos"][m] - foot[m]) <= 0.05 * ods[0] / 2 + 0.01:
                        slots.setdefault((f["dia_mm"], round(f.get("extent_mm") or 0.0, 2), k), []).append(f)
    # shoulders: planes square to the axis, strictly inside the ends
    area = {}
    for f in feats:
        if f["kind"] == "plane" and abs(abs(f["axis"][0]) - 1) < TOL:
            x = round(f["pos"][0], 3)
            area[x] = area.get(x, 0.0) + f["area_mm2"]
    kind = "shaft" if length > ods[0] else "disc"
    inner = [x for x in area if lo + 0.05 < x < hi - 0.05]
    shoulders = sorted(sorted(inner, key=lambda x: -area[x])[:MAX_SHOULDERS if kind == "shaft" else 3])
    info.update(kind=kind, axis=0, foot=foot, lo=lo, hi=hi, length=length, ods=sorted(best.values(), key=lambda s: -s["dia"]),
                bores=sorted(bores, key=lambda b: -b["dia"])[:MAX_BORES], holes_par=holes_par, holes_perp=holes_perp,
                slots=slots, shoulders=shoulders)
    return info


# ---- layout --------------------------------------------------------------------------------------------------------------
def _notes(info):
    notes = ["ALL SIZES MEASURED FROM THE MODEL.", "NO TOLERANCES ARE HELD IN THE MODEL."]
    if info["kind"] == "prismatic" and info["holes_any"]:
        notes.append("HOLE POSITIONS ARE NOT DIMENSIONED.")
    return notes


def _two_views(sheet, b, info, s, rows_above):
    """First angle: the end view (seen from the right) stands to the LEFT of the profile, a three-quarter view top right.
    Raises ValueError (as the engine does) when they do not fit at scale s."""
    m, th, ml = S._MARGIN, sheet.text_height, S._MARGIN_LEFT
    L, D = info["length"] * s, info["ods"][0]["dia"] * s
    reach_top = S._DIM_ROW * (rows_above + 1) + 2 * th
    reach_side = 60.0                              # callouts beside the end view
    notes_h = len(sheet.notes) * 5.0 + 8.0
    bottom = m + S._TITLE_H + notes_h + 12.0 + 10.0
    gap = 40.0 + reach_side
    block_w = reach_side + D + gap + L
    if bottom + D + reach_top > sheet.height - m - 5 or ml + 20 + block_w > sheet.width - m - 5:
        raise ValueError("two views do not fit at this scale")
    x0 = ml + 20 + max(0.0, (sheet.width - ml - m - 20 - block_w - 120) / 2)
    yc = bottom + D / 2 + max(0.0, (sheet.height - m - bottom - D - reach_top) / 3)
    right = sheet.view(b, "right", at=(x0 + reach_side + D / 2, yc))
    front = sheet.view(b, "front", at=(x0 + reach_side + D + gap + L / 2, yc))
    iso_s = min(s, 90.0 / max(info["size"]))
    iso = sheet.view(b, "iso", at=(sheet.width - m - 55, sheet.height - m - 55), hidden=False, centre_marks=False,
                     scale=iso_s)
    return None, front, right, iso


def build_sheet(shape_b3d, info, row, design, size, scale, expected, fault=None):
    """Lay out and dimension one sheet. `expected` collects (text the sheet must print, kernel value, what).
    `fault == "number"` (CLI proof of the check) prints the first placed size 0.02 mm too large."""
    pending = [fault == "number"]

    def shifted(prefix, value):
        """The text for a dimension: the engine measures it ("<>") unless the proof asks for a wrong one."""
        if pending[0]:
            pending[0] = False
            return prefix + fmt(value + 0.02)
        return prefix + "<>"

    rows_above = 1 + len(info.get("shoulders") or [])
    sheet = S.Sheet(size, scale=scale, title=row["name"], part_number=row["ref"],
                    material=("" if row["material"] == "-" else row["material"]), revision=design["revision"],
                    author=design["name"] + " · MADE FROM THE MODEL", notes=_notes(info), projection="FIRST ANGLE",
                    hlr="exact", iso_hlr="triangulation", doc_type="PART DRAWING")
    th = sheet.text_height
    kind = info["kind"]
    # first angle: the front view is the UPPER row, and the length dimensions stand in rows above it
    sheet.reach_top_mm = max(0.0, S._DIM_ROW * (rows_above + 1) + 2 * th - (12.0 + 2 * th + 8.0))
    sheet.reach_side_mm = 75.0       # callout text beside the views
    gap = 24.0 + 2 * th + 16.0
    turned = kind in ("shaft", "disc")
    cross = bool(info["holes_perp"] or info["slots"])
    if turned and not cross:
        top, front, right, iso = _two_views(sheet, shape_b3d, info, scale, rows_above)
    else:
        top, front, right, iso = sheet.three_views(shape_b3d, iso=True, gap=gap)
    iso.label = "NOT TO SCALE"
    # the label is centred under the view and wider than a small three-quarter view: keep it inside the frame
    iso.at = (min(iso.at[0], sheet.width - S._MARGIN - 28.0), iso.at[1])
    sx, e = info["size"], info["ext"]
    if kind == "prismatic":
        front.dim((e[0], e[2], e[5]), (e[1], e[2], e[5]), orientation="h", text=shifted("", sx[0]))
        expected.append((fmt(sx[0]), sx[0], "overall X"))
        front.dim((e[0], e[2], e[4]), (e[0], e[2], e[5]), orientation="v", offset=-12.0)
        expected.append((fmt(sx[2]), sx[2], "overall Z"))
        top.dim((e[0], e[2], e[5]), (e[0], e[3], e[5]), orientation="v", offset=-12.0)
        expected.append((fmt(sx[1]), sx[1], "overall Y"))
        on = {0: right, 1: front, 2: top}
        for (d, depth, ax, thru), fs in sorted(info["holes_any"].items(), key=lambda kv: -kv[0][0])[:MAX_HOLE_GROUPS]:
            i = max(range(3), key=lambda k: abs(ax[k]))
            f = fs[0]
            c = list(f["pos"])
            c[i] = f["bbox"][2 * i + 1]
            on[i].hole(tuple(c), d, thru=thru, depth=None if thru else (depth or None),
                       count=len(fs) if len(fs) > 1 else None)
            expected.append((fmt(d), d, "hole"))
            if len(fs) > 1:
                expected.append((str(len(fs)), len(fs), "hole count"))
            if depth and not thru:
                expected.append((fmt(depth), depth, "hole depth"))
        return sheet
    y0, z0 = info["foot"][1:]
    lo, hi = info["lo"], info["hi"]
    rmax = info["ods"][0]["dia"] / 2
    # diameters: a shaft across its own steps on the front view; a disc on its end view (thin steps cannot hold text)
    last_x = None
    for k, st in enumerate(sorted(info["ods"], key=lambda s: s["a0"] + s["a1"])):
        xm = (st["a0"] + st["a1"]) / 2
        r = st["dia"] / 2
        if kind == "shaft":
            # a dimension line across its own step; where the steps are narrower than the texts are wide, the lines are
            # spread to the right (the engine reads two texts closer than their width as printed over each other)
            x_sheet, off = (xm - lo) * scale, 0.0
            w = S._text_width("%%c" + fmt(st["dia"]), th)
            if last_x is not None and x_sheet - last_x[0] < (last_x[1] + w) / 2 + DIM_GAP_AIR_MM:
                off = last_x[0] + (last_x[1] + w) / 2 + DIM_GAP_AIR_MM - x_sheet
            last_x = (x_sheet + off, w)
            front.dim((xm, y0, z0 - r), (xm, y0, z0 + r), orientation="v", offset=off, text=shifted("%%c", st["dia"]))
        else:
            right.diameter((hi, y0, z0), r, angle=125 + 18 * k, text=("%%c" + fmt(st["dia"] + 0.02)) if pending[0] else None)
            pending[0] = False
        expected.append((fmt(st["dia"]), st["dia"], "diameter"))
    # shoulders: baseline from the left end face, rows above the view (nearest first), the overall length outermost
    for x in info["shoulders"]:
        front.dim((lo, y0, z0 + rmax), (x, y0, z0 + rmax), orientation="h", text=shifted("", x - lo))
        expected.append((fmt(x - lo), x - lo, "shoulder from the left end"))
    front.dim((lo, y0, z0 + rmax), (hi, y0, z0 + rmax), orientation="h", text=shifted("", hi - lo))
    expected.append((fmt(hi - lo), hi - lo, "length"))
    # coaxial bores: hole callouts on the end view
    for k, bo in enumerate(info["bores"]):
        right.hole((hi, y0, z0), bo["dia"], thru=bo["thru"], depth=None if bo["thru"] else bo["depth"], angle=60 - 25 * k)
        expected.append((fmt(bo["dia"]), bo["dia"], "bore"))
        if not bo["thru"]:
            expected.append((fmt(bo["depth"]), bo["depth"], "bore depth"))
    # holes parallel to the axis, grouped; with the circle they sit on when there are 3 or more
    for (d, depth, _ax, thru), fs in sorted(info["holes_par"].items(), key=lambda kv: -kv[0][0]):
        f = min(fs, key=lambda g: g["pos"][1])           # the leftmost on the end view: its callout exits left
        rads = sorted({round(math.hypot(g["pos"][1] - y0, g["pos"][2] - z0), 3) for g in fs})
        label = None
        if len(fs) >= 3 and len(rads) == 1:
            label = "ON %%c" + fmt(2 * rads[0])
            expected.append((fmt(2 * rads[0]), 2 * rads[0], "hole circle"))
        right.hole((hi, f["pos"][1], f["pos"][2]), d, thru=thru, depth=None if thru else depth,
                   count=len(fs) if len(fs) > 1 else None, label=label)
        if len(fs) > 1:
            expected.append((str(len(fs)), len(fs), "hole count"))
        expected.append((fmt(d), d, "hole"))
        if not thru and depth:
            expected.append((fmt(depth), depth, "hole depth"))
    # holes square to the axis: on the view along their axis (Z: top, Y: front)
    for (d, depth, ax, thru), fs in sorted(info["holes_perp"].items(), key=lambda kv: -kv[0][0]):
        f = fs[0]
        v, c = (top, (f["pos"][0], f["pos"][1], z0 + rmax)) if ax[2] >= 0.999 else (front, (f["pos"][0], y0 - rmax, f["pos"][2]))
        v.hole(c, d, thru=thru, depth=None if thru else (depth or None), count=len(fs) if len(fs) > 1 else None)
        expected.append((fmt(d), d, "cross hole"))
        if len(fs) > 1:
            expected.append((str(len(fs)), len(fs), "cross hole count"))
        if depth and not thru:
            expected.append((fmt(depth), depth, "cross hole depth"))
    # slots (keyways): "SLOT <width> WIDE ↧<depth>" on the view along the slot end's axis
    for (d, depth, k), fs in sorted(info["slots"].items(), key=lambda kv: -kv[0][0]):
        f = fs[0]
        text = "SLOT %s WIDE ↧%s" % (fmt(d), fmt(depth))
        if k == 2:
            top.diameter((f["pos"][0], f["pos"][1], z0 + rmax), d / 2, text=text)
        else:
            front.diameter((f["pos"][0], y0 - rmax, f["pos"][2]), d / 2, text=text)
        expected.append((fmt(d), d, "slot width"))
        expected.append((fmt(depth), depth, "slot depth"))
    return sheet


def _furniture(sheet, row, design):
    """Numbers allowed in the title block and the note lines (sheet furniture), and the names the user chose."""
    nums = set()
    for text in (design["name"], design["product_id"], row["ref"], row["name"], design["revision"], sheet.size,
                 S._scale_label(sheet.scale), "SHEET 1 OF 1"):
        nums.update(dcheck.NUM.findall(str(text)))
    nums.update(str(i + 1) for i in range(len(sheet.notes)))
    return {"numbers": nums, "names": [row["name"], design["name"], row["ref"]]}


def view_boxes(sheet):
    """[(name, (x0, x1, y0, y1))]: where each view stands on the sheet, as the engine's layout computes it."""
    boxes = []
    for v in sheet.views:
        proj = S._projection(v.shape, v.name, sheet.hlr_for(v.name), sheet.deflection_mm)
        b = S._bounds_of(proj, v.hidden)
        if not b:
            continue
        sv = v.scale or sheet.scale
        hw, hh = (b[1] - b[0]) * sv / 2, (b[3] - b[2]) * sv / 2
        boxes.append((v.name, (v.at[0] - hw, v.at[0] + hw, v.at[1] - hh, v.at[1] + hh)))
    return boxes


def fits(sheet, doc, clearance=2.0):
    """None when the sheet is clean, else why it is not: something (a dimension row, a callout's text) lies outside the
    frame, or two views stand on each other. The engine's own guard looks at the views only: at 1:1 the OUTPUT_SHAFT's rows
    of length dimensions ran off the top and its three-quarter view stood on the profile (checked on a preview)."""
    from ezdxf import bbox as zbbox
    ext = zbbox.extents(doc.modelspace())
    m, ml = S._MARGIN, S._MARGIN_LEFT
    tol = 0.05
    if (ext.extmin.x < ml - tol or ext.extmax.x > sheet.width - m + tol or ext.extmin.y < m - tol
            or ext.extmax.y > sheet.height - m + tol):
        return "something runs off the frame"
    boxes = view_boxes(sheet)
    for i, (na, a) in enumerate(boxes):
        for nb, b in boxes[i + 1:]:
            if a[0] < b[1] + clearance and a[1] > b[0] - clearance and a[2] < b[3] + clearance and a[3] > b[2] - clearance:
                return "the %s view stands on the %s view" % (na, nb)
    return None


def _overlaps(warnings):
    return [w for w in warnings if w.startswith("annotation overlaps")]


def draw_part(shape, row, design, check_fault=None):
    """One part drawing. `shape` is a kernel (TopoDS) shape, `row` a catalogue.drawing_bom row, `design` {"product_id",
    "name", "revision"}. Tries the sheets and scales of TRIES in order: the first that fits AND renders with no overlapping
    annotation wins; none without overlaps -> the fitting one with the fewest (its count is reported, never hidden); none
    fits -> DrawFail("it does not fit on the largest sheet").
    -> (sheet dict, pdf bytes). The sheet dict carries the read-back result of `check.check_part` (the caller decides)."""
    import time
    from build123d import Shape
    t0 = time.perf_counter()
    shape2, feats2, ext2, cov, rot, fixed = prepare(shape)
    info = analyse(shape2, feats2, ext2, cov)
    t_rules = time.perf_counter() - t0
    b = Shape.cast(shape2)
    best, last = None, None
    for size, scale in TRIES:
        expected = []
        try:
            sheet = build_sheet(b, info, row, design, size, scale, expected, fault=check_fault)
            docs, warns, lay_s = S.layout([sheet])
        except ValueError as exc:
            last = str(exc)
            continue
        why_not = fits(sheet, docs[0])
        if why_not:
            last = why_not
            continue
        ov = _overlaps(warns)
        cand = {"sheet": sheet, "docs": docs, "warns": warns, "expected": expected, "overlaps": len(ov),
                "size": size, "scale": scale, "layout_s": lay_s}
        if best is None or cand["overlaps"] < best["overlaps"]:
            best = cand
        if not ov:
            break
    if best is None:
        raise DrawFail(WHY_FIT)
    sheet, docs = best["sheet"], best["docs"]
    t1 = time.perf_counter()
    pdf = S.pdf_bytes(docs, [sheet])
    pdf_s = time.perf_counter() - t1
    res = dcheck.check_part(docs, best["expected"], _furniture(sheet, row, design))
    other = [w for w in best["warns"] if not w.startswith("annotation overlaps") and not w.startswith("check: [info]")]
    out = {"size": best["size"], "scale": S._scale_label(best["scale"]), "part_kind": info["kind"], "rotation": rot, "role_fixed": fixed,
           "numbers": res["numbers"], "expected": res["expected"], "unexplained": res["unexplained"],
           "missing": res["missing"], "wrong": res["wrong"], "words": res["words"], "name_words": res["name_words"],
           "overlaps": best["overlaps"], "overlap_texts": [w[:160] for w in _overlaps(best["warns"])[:3]], "warnings": other[:5], "details": res["details"][:12], "texts": res["texts"],
           "faces": len(feats2), "rules_s": round(t_rules, 3), "layout_s": round(best["layout_s"], 3),
           "pdf_s": round(pdf_s, 3),
           "views": {v.name: [round(v.at[0], 1), round(v.at[1], 1)] for v in sheet.views},
           "title_text": row["name"], "_doc": docs[0], "_sheet": sheet}      # _doc and _sheet: for a reviewer's preview; callers pop them
    S.clear_cache()
    return out, pdf
