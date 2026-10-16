"""Distinct geometric features (cylinders with bore/od role, planes, cones) -- the
catalogue's feature list (fixed rounding, key order and merge rule), plus the blend
rule: a cylinder
face is a bore/OD candidate only if its parametric arc span is >= 180 degrees, or the
caller passed no reference length, or its diameter fits within that reference length.
Without it, the gearbox reports an 828 mm "cylinder" and another test part a 39,981 mm one --
both are blend surfaces between features, not features themselves (they span more than
the part's own longest bounding-box edge, `bbox_a_mm`, so no real bore or OD can be that
big). The rule is purely geometric (arc span / radius vs. the part's own size); it never
mentions a part by name.
"""
import math

from framework.common.parity_match import canonical_cone  # noqa: F401  (re-exported)

from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.BRep import BRep_Tool
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepTools import BRepTools
from OCP.GProp import GProp_GProps
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.TopAbs import TopAbs_FACE, TopAbs_IN, TopAbs_ON, TopAbs_VERTEX
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Pnt, gp_Vec

PI = math.pi




def _r(x, n=6):
    try:
        return round(float(x), n)
    except Exception:
        return None


def _sign_normalize(vec):
    """Sign-normalise a 3-vector (for the Postgres catalogue): the first component with
    |c| > 1e-9 is made positive, flipping every component the same way. Returns
    (flipped_vec, flip) where flip is +1.0 or -1.0 -- the true axis/normal, as
    opposed to canonical_cone's abs-per-component convention above, which loses
    which of the two equally-valid representations a face's translator picked.
    A vector whose every component is within 1e-9 of zero is returned unchanged
    (flip=1.0); never occurs for a real face normal/axis, kept only as a
    defensive fallback."""
    flip = 1.0
    for c in vec:
        if abs(c) > 1e-9:
            flip = 1.0 if c > 0 else -1.0
            break
    return [flip * c for c in vec], flip


def _surface_point(face):
    """A point genuinely ON the face -- midpoint of its parameter range. For a full
    cylindrical face, its centre of mass sits on the axis by symmetry, which is why
    it is never used for a surface point."""
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    return 0.5 * (u0 + u1), 0.5 * (v0 + v1)


def _face_area(face):
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return props.Mass()


def _face_bbox(face):
    box = Bnd_Box()
    BRepBndLib.Add_s(face, box, False)
    box.SetGap(0.0)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return [_r(xmin, 3), _r(xmax, 3), _r(ymin, 3), _r(ymax, 3), _r(zmin, 3), _r(zmax, 3)]


def _merge_bb(a, b):
    if a is None:
        return list(b)
    return [min(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]),
            max(a[3], b[3]), min(a[4], b[4]), max(a[5], b[5])]


def _face_vertex_points(face):
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(face, TopAbs_VERTEX, m)
    out = []
    for i in range(1, m.Extent() + 1):
        p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(m.FindKey(i)))
        out.append((p.X(), p.Y(), p.Z()))
    return out


def _normal_at(face, u, v):
    """Outward unit normal at (u, v) -- BRepGProp_Face.Normal is orientation-aware
    (a REVERSED face already comes back pointing outward), same contract as
    FreeCAD's face.normalAt."""
    gf = BRepGProp_Face(face)
    p = gp_Pnt()
    vec = gp_Vec()
    gf.Normal(u, v, p, vec)
    length = vec.Magnitude()
    if length < 1e-12:
        return 0.0, 0.0, 0.0
    return vec.X() / length, vec.Y() / length, vec.Z() / length


def _cyl_role(classifier, adapt, face, loc, axis, radius):
    """bore or od. Step a hair RADIALLY INWARD from a surface point: material there
    means outside diameter, void means bore."""
    try:
        u, v = _surface_point(face)
        p = adapt.Value(u, v)
        dx, dy, dz = p.X() - loc.X(), p.Y() - loc.Y(), p.Z() - loc.Z()
        t = dx * axis[0] + dy * axis[1] + dz * axis[2]
        ax_pt = (loc.X() + axis[0] * t, loc.Y() + axis[1] * t, loc.Z() + axis[2] * t)
        rx, ry, rz = p.X() - ax_pt[0], p.Y() - ax_pt[1], p.Z() - ax_pt[2]
        length = math.sqrt(rx * rx + ry * ry + rz * rz)
        if length < 1e-9:
            return "unknown"
        step = max(1e-3, min(0.05, radius * 0.05))
        probe = gp_Pnt(p.X() - rx / length * step, p.Y() - ry / length * step,
                        p.Z() - rz / length * step)
        classifier.Perform(probe, 1e-7)
        state = classifier.State()
        return "od" if state in (TopAbs_IN, TopAbs_ON) else "bore"
    except Exception:
        return "unknown"


def features(shape, bbox_a_mm=None):
    """Distinct geometric features that other parts touch or a drawing would
    dimension. Coaxial cylinder faces merge into one feature; coplanar faces with
    the same outward normal merge into one feature. `bbox_a_mm` is the part's own
    longest bounding-box edge (measure.py passes bbox_sorted_mm[0]); a cylinder face
    whose diameter exceeds it, and whose arc span is under 180 degrees, is a blend
    between features (not a bore or OD) and is skipped entirely -- no feature, no
    face_count, no area."""
    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, face_map)
    classifier = BRepClass3d_SolidClassifier(shape)

    acc = {}
    for i in range(1, face_map.Extent() + 1):
        face = TopoDS.Face_s(face_map.FindKey(i))
        adapt = BRepAdaptor_Surface(face)
        stype = adapt.GetType()

        if stype == GeomAbs_SurfaceType.GeomAbs_Cylinder:
            cyl = adapt.Cylinder()
            radius = cyl.Radius()
            u0, u1, _v0, _v1 = BRepTools.UVBounds_s(face)
            arc_deg = (u1 - u0) * 180.0 / PI
            if not (arc_deg >= 180 - 1e-6 or bbox_a_mm is None or 2 * radius <= bbox_a_mm + 1e-9):
                continue  # blend rule: not a bore/OD candidate
            axis_dir = cyl.Axis().Direction()
            axis = (axis_dir.X(), axis_dir.Y(), axis_dir.Z())
            loc = cyl.Location()
            role = _cyl_role(classifier, adapt, face, loc, axis, radius)
            tt = loc.X() * axis[0] + loc.Y() * axis[1] + loc.Z() * axis[2]
            foot = (_r(loc.X() - axis[0] * tt, 2), _r(loc.Y() - axis[1] * tt, 2),
                    _r(loc.Z() - axis[2] * tt, 2))
            key = ("cylinder", _r(radius, 3), _r(abs(axis[0]), 3), _r(abs(axis[1]), 3),
                   _r(abs(axis[2]), 3), role, foot)
            axis_signed, _flip = _sign_normalize(axis)
            e = acc.setdefault(key, {
                "kind": "cylinder", "role": role,
                "radius_mm": key[1], "dia_mm": _r(radius * 2, 3),
                "axis": [key[2], key[3], key[4]], "pos": list(foot),
                "face_count": 0, "area_mm2": 0.0, "bbox": None, "_tmin": None, "_tmax": None,
                # the Postgres catalogue: the true, sign-normalised axis of the FIRST face
                # merged into this feature -- "axis" above stays the legacy abs-per-
                # component key used for merging/parity, unchanged.
                "axis_signed": [_r(c, 3) for c in axis_signed]})
            for vx, vy, vz in _face_vertex_points(face):
                t = vx * axis[0] + vy * axis[1] + vz * axis[2]
                e["_tmin"] = t if e["_tmin"] is None else min(e["_tmin"], t)
                e["_tmax"] = t if e["_tmax"] is None else max(e["_tmax"], t)
        elif stype == GeomAbs_SurfaceType.GeomAbs_Plane:
            u, v = _surface_point(face)
            n = _normal_at(face, u, v)
            p = adapt.Value(u, v)
            off = p.X() * n[0] + p.Y() * n[1] + p.Z() * n[2]
            key = ("plane", _r(n[0], 3), _r(n[1], 3), _r(n[2], 3), _r(off, 2))
            # The plane's own "axis" (key[1:4]) is ALREADY the true outward
            # normal (never abs'd, unlike cylinder/cone) -- its true direction also
            # survives today in offset_mm's sign. axis_signed just sign-normalises
            # that same true normal for the catalogue's uniform convention.
            axis_signed, _flip = _sign_normalize(n)
            e = acc.setdefault(key, {
                "kind": "plane", "role": "face",
                "axis": [key[1], key[2], key[3]], "offset_mm": key[4],
                "pos": [_r(p.X(), 3), _r(p.Y(), 3), _r(p.Z(), 3)],
                "face_count": 0, "area_mm2": 0.0, "bbox": None,
                "axis_signed": [_r(c, 3) for c in axis_signed]})
        elif stype == GeomAbs_SurfaceType.GeomAbs_Cone:
            cone = adapt.Cone()
            axis_dir = cone.Axis().Direction()
            apex = cone.Apex()
            # canonical_cone: the raw, unadjusted axis
            # and semi-angle sign are a representation choice, not a geometric fact --
            # canonicalise so every physical cone gets the SAME (axis, angle) pair
            # regardless of which of the two equally-valid representations this face's
            # translator happened to construct.
            raw_axis = (axis_dir.X(), axis_dir.Y(), axis_dir.Z())
            raw_angle = cone.SemiAngle() * 180.0 / PI
            c_axis, c_angle = canonical_cone(raw_axis, raw_angle)
            key = ("cone", _r(c_angle, 2),
                   _r(c_axis[0], 3), _r(c_axis[1], 3), _r(c_axis[2], 3),
                   _r(apex.X(), 2), _r(apex.Y(), 2), _r(apex.Z(), 2))
            # The true (signed) axis/semi-angle PAIR of the first face merged
            # into this feature -- sign-normalise the raw axis, and negate the raw
            # angle exactly when the axis was flipped, so the two stay a physically
            # consistent pair (unlike canonical_cone's abs-per-field convention above,
            # which discards which of the two equally-valid representations a face's
            # translator picked).
            signed_axis, flip = _sign_normalize(raw_axis)
            signed_angle = raw_angle * flip
            e = acc.setdefault(key, {
                "kind": "cone", "role": "chamfer",
                "semi_angle_deg": key[1], "axis": [key[2], key[3], key[4]],
                "pos": [key[5], key[6], key[7]],
                "face_count": 0, "area_mm2": 0.0, "bbox": None,
                "axis_signed": [_r(c, 3) for c in signed_axis],
                "semi_angle_signed_deg": _r(signed_angle, 2)})
        else:
            continue

        e["face_count"] += 1
        e["area_mm2"] = _r(e["area_mm2"] + _face_area(face), 3)
        e["bbox"] = _merge_bb(e["bbox"], _face_bbox(face))

    out = []
    for e in acc.values():
        if e["kind"] == "cylinder":
            if e["_tmin"] is not None:
                e["extent_mm"] = _r(e["_tmax"] - e["_tmin"], 3)
            e.pop("_tmin", None)
            e.pop("_tmax", None)
        elif e["kind"] == "plane":
            e["extent_mm"] = _r(e["area_mm2"] ** 0.5, 3)
        out.append(e)
    out.sort(key=lambda d: (d["kind"], -(d.get("dia_mm") or 0), -(d.get("area_mm2") or 0)))
    return out
