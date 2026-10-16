"""The part record -- the catalogue's `measure()` record (fixed keys, meaning and
rounding).

Volume and area (and the centre of mass / inertia that come with them) use ONE
agreement rule, applied PER SOLID, for every part, no exceptions: OCCT's DEFAULT
BRepGProp integration (no eps -- FreeCAD's own Shape.Volume) and, independently,
adaptive integration (its own internal eps 1e-6, the algorithm's usual convergence
target) are compared on EACH of the part's solids separately (a shape with no distinct
solids -- the common single-solid case -- is treated as one). When the two agree
within _AGREE_REL_TOL relative, that solid's method is "formula" -- its volume/area
come from the DEFAULT props. When they disagree (a solid the default formula gets
wrong), that solid alone falls back to a two-level triangulation measurement, with an
error estimate. The part's volume/area is the SUM of every solid's own chosen value;
the part's method is "mesh" if ANY solid used mesh (uncertainty: the SUM of every
solid's own, 0.0 for a formula solid) -- observed on 4 of the 79 catalogue parts,
never listed here by name. See `mass_props()`/`_solid_mass_props()`.

Per-solid, not per-part, matters: a multi-solid part (e.g. a multi-body upload
catalogued as one part record) can have ONE solid whose formula is genuinely wrong
while its other solids' correct values dilute that solid's own gap below
_AGREE_REL_TOL at whole-part level -- exactly this was found, by direct measurement,
on a 5-solid catalogue part: solid-level gap 2.29e-4 (routed to mesh) vs the same
part's own whole-shape gap of only 8.54e-5 (would have stayed "formula", silently
keeping a ~1,090 mm3 error). Per-solid closes that.

_AGREE_REL_TOL is 1.5e-4, not 1e-6: measuring every
solid of every one of the 79 catalogue parts' raw default-vs-adaptive gap (before
wiring the 1e-6 rule in) found several solids -- all independently confirmed, against
golden, to have a CORRECT default value -- where adaptive integration alone carries
this much intrinsic numerical noise; 1e-6 would have wrongly routed those
noisy-but-correct solids to the mesh path too, replacing an exact, golden-matching
number with a mesh approximation for no reason. 1.5e-4 sits above every one of those
noise gaps and below every genuinely-wrong solid's own gap, verified against all 79
parts' solids (measured directly, per-solid) to give exactly the 4 mesh-path parts.
The largest confirmed-noise VOLUME gap on an otherwise-correct
(formula) solid is now 8.9e-5, on one solid of an 8-solid catalogue part -- NOT the
8.5e-5 figure an earlier version of this docstring cited, which was a DIFFERENT,
5-solid catalogue part's own diluted whole-PART gap, now resolved by per-solid routing:
that part's one genuinely-wrong solid (of 5) is caught individually at 2.29e-4. The
largest confirmed-noise AREA gap on an otherwise-correct (formula) solid is 9.9e-5, on
a single-solid catalogue part whose VOLUME (not area) fails agreement at 2.16e-4 and so
goes to mesh -- its area stays formula, correctly, at that 9.9e-5 gap; an earlier
version of this docstring mischaracterised that 9.9e-5 figure as a volume noise case.
"""
import gc
import math
from collections import Counter

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GProp import GProp_GProps
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID, TopAbs_VERTEX
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Pnt, gp_Vec

from framework.kernel import features as _features

_ADAPTIVE_EPS = 1e-6      # adaptive BRepGProp's OWN convergence target
_AGREE_REL_TOL = 1.5e-4   # default-vs-adaptive AGREEMENT comparison tolerance (see above)

_SURFACE_NAMES = {
    GeomAbs_SurfaceType.GeomAbs_Plane: "Plane",
    GeomAbs_SurfaceType.GeomAbs_Cylinder: "Cylinder",
    GeomAbs_SurfaceType.GeomAbs_Cone: "Cone",
    GeomAbs_SurfaceType.GeomAbs_Sphere: "Sphere",
    GeomAbs_SurfaceType.GeomAbs_Torus: "Toroid",
    GeomAbs_SurfaceType.GeomAbs_BezierSurface: "BezierSurface",
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface: "BSplineSurface",
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution: "SurfaceOfRevolution",
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion: "SurfaceOfExtrusion",
    GeomAbs_SurfaceType.GeomAbs_OffsetSurface: "OffsetSurface",
    GeomAbs_SurfaceType.GeomAbs_OtherSurface: "OtherSurface",
}
_CURVE_NAMES = {
    GeomAbs_CurveType.GeomAbs_Line: "Line",
    GeomAbs_CurveType.GeomAbs_Circle: "Circle",
    GeomAbs_CurveType.GeomAbs_Ellipse: "Ellipse",
    GeomAbs_CurveType.GeomAbs_Hyperbola: "Hyperbola",
    GeomAbs_CurveType.GeomAbs_Parabola: "Parabola",
    GeomAbs_CurveType.GeomAbs_BezierCurve: "BezierCurve",
    GeomAbs_CurveType.GeomAbs_BSplineCurve: "BSplineCurve",
    GeomAbs_CurveType.GeomAbs_OffsetCurve: "OffsetCurve",
    GeomAbs_CurveType.GeomAbs_OtherCurve: "Undefined",
}


def _r(x, n=6):
    try:
        return round(float(x), n)
    except Exception:
        return None


def _indexed(shape, kind):
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return m


def _bbox_diagonal(shape):
    xmin, xmax, ymin, ymax, zmin, zmax = _bbox6(shape)
    return math.sqrt((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2)


_MESH_FACE_COUNT_CEILING = 500
_MESH_BSPLINE_FACE_CEILING = 4  # see criterion 4 below


def _mesh_deflection_levels(shape):
    """The two (linear, angular) deflection pairs _two_level_mesh meshes at, chosen
    from `shape`'s own geometry -- never from a part name or document id. Called per
    SOLID (mass_props is per-solid); every criterion below is evaluated on whatever shape is passed
    in, whether that is a whole part or one of its solids.

    The base schedule: linear = 1e-4 x bbox diagonal
    (same at both levels -- see below), angular 0.02 rad then 0.005 rad. Benchmarking
    every mesh-path candidate directly (BRepMesh_IncrementalMesh timed in isolation,
    before wiring this function in) found that schedule computationally intractable for
    some shapes -- not merely slow, but tens of GB of RAM and still not finished after
    minutes -- diagnosed to four geometric properties of the shape, never its identity:
      1. Any face whose BRepAdaptor_Surface().GetType() is GeomAbs_SurfaceOfExtrusion.
         This is the SAME surface type already known to be unstable for adaptive
         BRepGProp integration; the instability turns out
         to extend to BRepMesh_IncrementalMesh too, at any angular deflection finer
         than roughly 0.06 rad, independent of linear deflection, independent of
         relative vs. absolute mode, independent of parallel meshing, and independent
         of a BRepBuilderAPI_NurbsConvert pre-pass (all tried and all still hung).
      2. A face count over ~500 (a real, densely-faced imported part -- one
         mesh-path part alone has thousands) -- fine deflection times out from
         sheer face volume, not a hang.
      3. More than one solid in the shape (a multi-part assembly measured as one
         compound, e.g. a real bearing assembly of 34 solids, as measured before
         the per-solid routing existed to split it) -- kept as a defensive floor for any
         caller that ever passes a whole multi-solid compound in directly (mass_props
         itself never does: it calls this once per solid, each Extent()
         1); every mesh-path shape actually needing the fine schedule's precision is a
         single solid, so this floor costs nothing there.
      4. More than _MESH_BSPLINE_FACE_CEILING faces whose surface type is
         GeomAbs_BSplineSurface. Found by the per-solid routing itself:
         splitting a 34-solid real bearing assembly (criterion 3's own example) into
         its individual solids revealed ONE solid -- ordinary by every other measure
         (34 faces total, no extrusion surface, a single solid on its own) -- that
         still hung at the fine schedule in isolation (confirmed: level 0.1 rad 0.41 s,
         0.06 rad 2.04 s, 0.02 rad still unfinished after 90 s). That solid has 8
         BSplineSurface faces; the two mesh-path shapes that genuinely need (and
         tractably get) fine precision each have only 2. 4 sits between the two,
         verified not to move either of the fine-schedule shapes onto the coarse one.
    For a shape with any of these properties, both levels float the angular deflection
    to 0.1/0.06 rad (confirmed tractable, seconds not minutes, on every affected
    shape) -- coarser than the base schedule, but still comfortably inside the loose
    tolerance allowed for these large/complex/multi-solid shapes. Every
    other mesh-path shape (single solid, no such face, a modest face count) keeps a
    fine 0.02/0.01 schedule -- angular 0.01, not the base schedule's 0.005, which was
    independently confirmed intractable even on this smaller, simpler shape once
    combined with a refined linear deflection (see below); 0.02/0.01 is both tractable
    (seconds) and accurate enough for the mesh-path shapes that need it.
    Linear deflection does NOT refine between the two levels (kept at 1e-4 x bbox
    diagonal for both) -- refining linear and angular together was the specific
    combination that turned a tractable level 1 into an intractable level 2 during
    benchmarking; refining angular alone still gives a meaningful two-level
    uncertainty estimate without that blow-up.
    """
    diag = _bbox_diagonal(shape)
    lin_defl = 1e-4 * diag

    if _indexed(shape, TopAbs_SOLID).Extent() > 1:
        return [(lin_defl, 0.1), (lin_defl, 0.06)]

    face_map = _indexed(shape, TopAbs_FACE)
    has_extrusion_surface = False
    n_bspline_faces = 0
    for i in range(1, face_map.Extent() + 1):
        face = TopoDS.Face_s(face_map.FindKey(i))
        t = BRepAdaptor_Surface(face).GetType()
        if t == GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion:
            has_extrusion_surface = True
        elif t == GeomAbs_SurfaceType.GeomAbs_BSplineSurface:
            n_bspline_faces += 1

    if (has_extrusion_surface or face_map.Extent() > _MESH_FACE_COUNT_CEILING
            or n_bspline_faces > _MESH_BSPLINE_FACE_CEILING):
        return [(lin_defl, 0.1), (lin_defl, 0.06)]
    return [(lin_defl, 0.02), (lin_defl, 0.01)]


def _two_level_mesh(shape):
    """Two independent BRepBuilderAPI_Copy meshes of `shape`, at the two (linear,
    angular) deflections _mesh_deflection_levels chooses -- each meshed with
    BRepMesh_IncrementalMesh(relative=False, parallel=True), then measured with
    triangulation-based BRepGProp (VolumeProperties_s/SurfaceProperties_s,
    OnlyClosed/SkipShared=True, UseTriangulation=True). Returns
    ((vol_props_1, area_props_1), (vol_props_2, area_props_2)); the caller reads
    .Mass()/.CentreOfMass()/.PrincipalProperties() off whichever level it needs.
    """
    levels = []
    for lin_defl, ang_defl in _mesh_deflection_levels(shape):
        copy = BRepBuilderAPI_Copy(shape, True, False).Shape()
        BRepMesh_IncrementalMesh(copy, lin_defl, False, ang_defl, True)
        vprops = GProp_GProps()
        BRepGProp.VolumeProperties_s(copy, vprops, True, True, True)
        aprops = GProp_GProps()
        BRepGProp.SurfaceProperties_s(copy, aprops, True, True)
        levels.append((vprops, aprops))
        # The triangulated copy (every face's mesh, potentially large -- across a whole test session measuring many parts, OCP's C++-backed
        # TopoDS_Shape/triangulation objects were confirmed, by direct observation, not
        # to be reclaimed promptly by Python's own refcounting, accumulating several GB
        # over a session) is dropped and explicitly collected the moment its two
        # GProp_GProps values are extracted -- applied after every mesh-path
        # measurement, not held for the caller.
        del copy
        gc.collect()
    return levels


def _agree(a, b, tol=_AGREE_REL_TOL):
    return abs(a - b) <= tol * max(abs(a), abs(b), 1e-12)


def _solid_mass_props(shape) -> dict:
    """Default-vs-adaptive BRepGProp agreement for ONE solid (or, when the caller has
    no distinct solids to split, the whole shape treated as one) -- the per-solid unit
    `mass_props()` sums. Returns volume_val/volume_method/volume_unc/volume_props (a
    GProp_GProps -- either the default-formula one or the mesh level-2 one, whichever
    this solid's own method chose, for the caller to `.Add()` into a combined system),
    area_val/area_method/area_unc, and the raw default/adaptive numbers (v_default/
    v_adaptive/a_default/a_adaptive) for diagnostics/docstring evidence.
    """
    v_default_props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, v_default_props)
    v_default = v_default_props.Mass()
    v_adaptive_props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, v_adaptive_props, _ADAPTIVE_EPS)
    v_adaptive = v_adaptive_props.Mass()
    v_agree = _agree(v_default, v_adaptive)

    a_default_props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, a_default_props)
    a_default = a_default_props.Mass()
    a_adaptive_props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, a_adaptive_props, _ADAPTIVE_EPS)
    a_adaptive = a_adaptive_props.Mass()
    a_agree = _agree(a_default, a_adaptive)

    mesh_levels = mesh_deflections = None
    if not (v_agree and a_agree):
        mesh_deflections = _mesh_deflection_levels(shape)
        mesh_levels = _two_level_mesh(shape)

    volume_level1 = area_level1 = None
    if v_agree:
        volume_method, volume_val, volume_unc = "formula", v_default, 0.0
        volume_props = v_default_props
    else:
        (vp1, _), (vp2, _) = mesh_levels
        vol1, vol2 = vp1.Mass(), vp2.Mass()
        volume_method, volume_val, volume_unc = "mesh", vol2, abs(vol2 - vol1)
        volume_props = vp2
        volume_level1 = vol1
        print("volume formulas disagree (default %.3f, adaptive %.3f) -> mesh %.3f +- %.3f"
              % (v_default, v_adaptive, vol2, volume_unc))

    if a_agree:
        area_method, area_val, area_unc = "formula", a_default, 0.0
    else:
        (_, ap1), (_, ap2) = mesh_levels
        ar1, ar2 = ap1.Mass(), ap2.Mass()
        area_method, area_val, area_unc = "mesh", ar2, abs(ar2 - ar1)
        area_level1 = ar1
        print("area formulas disagree (default %.3f, adaptive %.3f) -> mesh %.3f +- %.3f"
              % (a_default, a_adaptive, ar2, area_unc))

    return {
        "volume_val": volume_val, "volume_method": volume_method, "volume_unc": volume_unc,
        "volume_props": volume_props,
        # level1 (the coarser of the two mesh passes), None for a formula solid -- kept
        # only for test-time validation: lets a test recompute the SAME level2-vs-level1 gap
        # this function already used to set volume_unc, as a self-consistency check, and
        # extrapolate a third estimate from the two levels, at no extra meshing cost.
        "volume_level1": volume_level1,
        "area_val": area_val, "area_method": area_method, "area_unc": area_unc,
        "area_level1": area_level1,
        # (linear, angular) deflection pairs used for the two mesh levels, when either
        # went to mesh -- same test-validation purpose as volume_level1/area_level1.
        "mesh_deflections": mesh_deflections,
        "v_default": v_default, "v_adaptive": v_adaptive,
        "a_default": a_default, "a_adaptive": a_adaptive,
    }


def mass_props(shape) -> dict:
    """Volume, area, centre of mass and inertia for `shape`, by the one rule every
    part uses, applied PER SOLID (module docstring): a part with several solids (e.g. a multi-body
    upload catalogued as one part record) gets `_solid_mass_props()` run independently
    on EACH of its solids (TopAbs_SOLID; a shape with none -- e.g. a single-solid part,
    which is the common case -- is treated as one solid, itself). volume_mm3/area_mm2
    are the SUM of every solid's own chosen value; volume_method/area_method is "mesh"
    if ANY solid used mesh, else "formula"; the uncertainty is the SUM of every solid's
    own (0.0 for a formula solid). This catches a formula error confined to one solid
    of a multi-solid part that would otherwise be diluted below _AGREE_REL_TOL at
    whole-part level. Centre of mass / inertia are combined
    with `GProp_GProps.Add()` on each solid's own chosen volume-properties object
    (the default-formula one, or the mesh level-2 one) -- OCCT's GProp_GProps stores
    the underlying mass/moment integrals, not a derived centre/tensor, so summing them
    across solids is exact (no manual parallel-axis step needed): hence
    GProp_GProps.Add on the per-solid props.
    Returns a dict with volume_mm3/volume_method/volume_uncertainty_mm3, area_mm2/
    area_method/area_uncertainty_mm2, com/moments/rgyr (raw, unrounded; None when
    PrincipalProperties() is unavailable, same as the pre-existing behaviour), plus
    "solids" (each solid's own _solid_mass_props() dict, for a caller -- e.g. a test --
    that wants the per-solid breakdown; not part of measure()'s own record).
    `volume()`/`area()` and `measure()` all go through this one function; the only
    other place in the kernel that computes a volume is `default_volume()` (below),
    deliberately NOT routed through this agreement rule -- see its own docstring.
    """
    solid_map = _indexed(shape, TopAbs_SOLID)
    n = solid_map.Extent()
    solids = [solid_map.FindKey(i) for i in range(1, n + 1)] if n else [shape]

    per_solid = [_solid_mass_props(s) for s in solids]

    volume_val = sum(p["volume_val"] for p in per_solid)
    volume_unc = sum(p["volume_unc"] for p in per_solid)
    volume_method = "mesh" if any(p["volume_method"] == "mesh" for p in per_solid) else "formula"

    area_val = sum(p["area_val"] for p in per_solid)
    area_unc = sum(p["area_unc"] for p in per_solid)
    area_method = "mesh" if any(p["area_method"] == "mesh" for p in per_solid) else "formula"

    combined_props = GProp_GProps()
    for p in per_solid:
        combined_props.Add(p["volume_props"])

    com = moments = rgyr = None
    try:
        c = combined_props.CentreOfMass()
        com = [c.X(), c.Y(), c.Z()]
        pp = combined_props.PrincipalProperties()
        moments = list(pp.Moments())
        rg = pp.RadiusOfGyration()
        rgyr = list(rg) if rg else None
    except Exception:
        pass

    return {
        "volume_mm3": volume_val, "volume_method": volume_method, "volume_uncertainty_mm3": volume_unc,
        "area_mm2": area_val, "area_method": area_method, "area_uncertainty_mm2": area_unc,
        "com": com, "moments": moments, "rgyr": rgyr,
        "solids": per_solid,
    }


def volume(shape) -> float:
    """Volume, mm3, by the one agreement rule (mass_props) -- the default formula for the 76/79
    parts whose default and adaptive formulas agree; mesh for the other 3."""
    return mass_props(shape)["volume_mm3"]


def area(shape) -> float:
    """Area, mm2, by the one agreement rule (mass_props) -- the default formula for the 76/79 parts
    whose default and adaptive formulas agree; mesh for the other 3."""
    return mass_props(shape)["area_mm2"]


def default_volume(shape) -> float:
    """Default (non-adaptive) BRepGProp volume, mm3 -- no agreement check, no
    mesh fallback. For ad-hoc, non-catalogued shapes the part-record contract (golden
    parity) does not apply to, such as per-pose
    interference volumes during a sweep (those shapes can inherit a
    slice of a mesh-path part's own problematic face at any one of dozens of poses;
    routing every one of them through mass_props's default-vs-adaptive-vs-mesh dance
    was confirmed, by direct benchmarking, to take minutes and tens of GB instead of
    the sub-second, bounded cost this sweep has always had). `volume()`/`measure()`
    are unaffected -- every catalogued part still goes through the one agreement rule."""
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


def _bbox6(shape):
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    box.SetGap(0.0)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return xmin, xmax, ymin, ymax, zmin, zmax


def _is_circular_curve(curve):
    """True when `curve` (an Adaptor3d_Curve) has constant, non-zero curvature across
    several sampled points -- i.e. is a circular arc, whether or not OCCT's own
    GetType() reports it as GeomAbs_Circle. A spindle torus's meridian (minor radius
    >= major radius) is written to STEP as a rational BSpline circle to satisfy
    AP214's surface_of_revolution schema; the reader's GetType() then comes back
    GeomAbs_BSplineCurve even though the curve is still geometrically a circle. Pure
    curvature (kappa = |r' x r''| / |r'|^3) is representation-independent, so this
    test classifies correctly either way. Applied to every basis curve of every part.
    """
    u0, u1 = curve.FirstParameter(), curve.LastParameter()
    if not (math.isfinite(u0) and math.isfinite(u1)) or u1 <= u0:
        return False
    kappas = []
    for frac in (0.15, 0.35, 0.5, 0.65, 0.85):
        u = u0 + (u1 - u0) * frac
        p, d1, d2 = gp_Pnt(), gp_Vec(), gp_Vec()
        curve.D2(u, p, d1, d2)
        speed = d1.Magnitude()
        if speed < 1e-9:
            return False
        kappas.append(d1.Crossed(d2).Magnitude() / speed ** 3)
    if any(k < 1e-9 for k in kappas):
        return False
    lo, hi = min(kappas), max(kappas)
    return (hi - lo) <= max(1e-6, 1e-4 * hi)


def topology_excl_degenerate(shape):
    """n_edges, n_vertexes and the curve-type histogram, EXCLUDING degenerated edges
    and the vertices that appear only on degenerated ones.
    STEP cannot represent a degenerated edge (a zero-length edge OCCT keeps only to
    close out a face's wire, e.g. at a cone apex or a sphere pole) -- the reader
    recreates each face's own degenerated edges from its surface at read time rather
    than sharing a single BREP edge across faces, so a part whose exact BREP has any
    degenerated edges gets a smaller RAW n_edges/n_vertexes/curves once written to
    STEP and read back. Representation, not geometry (confirmed: 79/79 kernel-written
    STEP parts pass volume/area/bbox/CoM parity). This is the translator-independent,
    comparable count -- used by the parity test on BOTH the kernel's STEP-read shape
    and the exact frozen BREP (golden's own source), instead of `measure()`'s own
    n_edges/n_vertexes/curves (which stay the raw counts and are not part of this
    function's output).
    """
    edge_map = _indexed(shape, TopAbs_EDGE)
    edges = [TopoDS.Edge_s(edge_map.FindKey(i)) for i in range(1, edge_map.Extent() + 1)]
    real_edges = [e for e in edges if not BRep_Tool.Degenerated_s(e)]

    vert_edges = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_VERTEX, TopAbs_EDGE, vert_edges)
    n_vertexes = 0
    for i in range(1, vert_edges.Extent() + 1):
        ancestors = vert_edges.FindFromIndex(i)
        if any(not BRep_Tool.Degenerated_s(TopoDS.Edge_s(e)) for e in ancestors):
            n_vertexes += 1

    curves = Counter(_curve_kind(e) for e in real_edges)
    return {"n_edges": len(real_edges), "n_vertexes": n_vertexes, "curves": dict(curves)}


def _surface_kind(face):
    adapt = BRepAdaptor_Surface(face)
    t = adapt.GetType()
    if t == GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution:
        # A torus whose minor radius >= major radius (a "spindle" torus) is written to
        # STEP as a bare SURFACE_OF_REVOLUTION, not a toroidal_surface (representation,
        # not geometry). Classify geometrically instead: any
        # surface of revolution whose meridian (basis curve) is a circle IS a torus.
        # Applied to every face of every part, no exceptions.
        try:
            circular = _is_circular_curve(adapt.BasisCurve())
        except Exception:
            circular = False
        if circular:
            return "Toroid"
    return _SURFACE_NAMES.get(t, "OtherSurface")


def _curve_kind(edge):
    try:
        t = BRepAdaptor_Curve(edge).GetType()
        return _CURVE_NAMES.get(t, "Undefined")
    except Exception:
        return "Undefined"


def measure(shape, name: str, label: str = None, type_id: str = "Part::Feature",
            material: str = "Default", params: dict = None):
    """Volume, area, bbox (raw and sorted), centre of mass, principal moments, radii
    of gyration, topology counts, surface-type histogram, scale-free ratios and
    features -- the catalogue's part record, plus volume_method/
    volume_uncertainty_mm3/area_method/area_uncertainty_mm2 (mass_props' agreement
    rule). Returns None when the shape has no solid. No placement
    handling: kernel shapes are already in model coordinates."""
    solid_map = _indexed(shape, TopAbs_SOLID)
    n_solids = solid_map.Extent()
    if n_solids == 0:
        return None

    xmin, xmax, ymin, ymax, zmin, zmax = _bbox6(shape)
    dims = sorted([xmax - xmin, ymax - ymin, zmax - zmin], reverse=True)

    # One mass_props() call for the whole shape gives volume_mm3/area_mm2 (the record's
    # own contract: measured on `shape`, not per-solid) AND, when there is exactly one
    # solid, the same numbers a second per-solid call would give (a compound holding a
    # single solid has identical faces to that solid) -- reused for com/moments/rgyr
    # instead of a second GProp pass, so the agreement rule runs once per part, not twice.
    mp = mass_props(shape)
    vol, ar = mp["volume_mm3"], mp["area_mm2"]

    com = moments = rgyr = None
    if n_solids == 1:
        com = [_r(v, 3) for v in mp["com"]] if mp["com"] else None
        moments = [_r(v, 3) for v in mp["moments"]] if mp["moments"] else None
        rgyr = [_r(v, 4) for v in mp["rgyr"]] if mp["rgyr"] else None

    face_map = _indexed(shape, TopAbs_FACE)
    edge_map = _indexed(shape, TopAbs_EDGE)
    faces = [TopoDS.Face_s(face_map.FindKey(i)) for i in range(1, face_map.Extent() + 1)]
    edges = [TopoDS.Edge_s(edge_map.FindKey(i)) for i in range(1, edge_map.Extent() + 1)]
    surf = Counter(_surface_kind(f) for f in faces)
    curv = Counter(_curve_kind(e) for e in edges)

    bbox_a_mm = _r(dims[0], 3) if dims else None

    rec = {
        "object": name, "label": label or name, "type_id": type_id, "units": "mm",
        "volume_mm3": _r(vol, 3), "area_mm2": _r(ar, 3),
        "bbox_sorted_mm": [_r(d, 3) for d in dims],
        "bbox": [_r(xmin, 3), _r(xmax, 3), _r(ymin, 3), _r(ymax, 3), _r(zmin, 3), _r(zmax, 3)],
        "com": com, "moments": moments, "radius_of_gyration": rgyr,
        "n_solids": n_solids, "n_shells": _indexed(shape, TopAbs_SHELL).Extent(),
        "n_faces": len(faces), "n_edges": len(edges),
        "n_vertexes": _indexed(shape, TopAbs_VERTEX).Extent(),
        "surfaces": dict(surf), "curves": dict(curv),
        "features": _features.features(shape, bbox_a_mm=bbox_a_mm),
        "shape_ratios": {
            "aspect_1": _r(dims[1] / dims[0], 5) if dims[0] else None,
            "aspect_2": _r(dims[2] / dims[0], 5) if dims[0] else None,
            "fill": _r(vol / (dims[0] * dims[1] * dims[2]), 5) if all(dims) else None,
            "compactness": _r(vol ** (2.0 / 3.0) / ar, 5) if ar else None,
        },
        "params": params or {},
        "material": material,
        # Provenance (catalogue columns unchanged, kept here in
        # the record/raw_json): which rule produced volume_mm3/area_mm2 above, and the
        # mesh-path error estimate (0.0 for "formula"). Never keyed to a part name.
        "volume_method": mp["volume_method"],
        "volume_uncertainty_mm3": _r(mp["volume_uncertainty_mm3"], 3) or 0.0,
        "area_method": mp["area_method"],
        "area_uncertainty_mm2": _r(mp["area_uncertainty_mm2"], 3) or 0.0,
    }
    # Every face/edge above is an OCP-wrapped C++ topology object; freeing `faces`/
    # `edges` (and mass_props' own default/adaptive GProp_GProps) is left to Python's
    # refcounting everywhere else in this package, but measure() is the one function
    # called once per catalogued part across a whole session (up to 79 times, plus
    # every builder/upload/probe test's own parts) -- confirmed, by direct
    # observation, to accumulate several GB over such a session if never prompted.
    # One collection per part is cheap next to this function's own BRepGProp cost.
    del faces, edges
    gc.collect()
    return rec
