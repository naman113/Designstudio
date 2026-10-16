"""Shape signatures: no DB access, no `framework.catalog`
import -- pure functions over a kernel record (`framework.kernel.measure.measure()`'s own
dict, or the equivalent reconstructed from Postgres facts) and, for `d2_histogram`, a
TopoDS shape. `framework.catalog.pg.signatures` is the DB-facing caller that resolves a
part's shape/record and writes these into the `signature` table + `part_revision.geom_hash`.

Three signatures:
- `geom_hash(record)`: an exact-duplicate hash ("inv_v1:<sha256>") over rotation/translation
  -invariant numbers already in the record (volume, area, principal moments, radii of
  gyration, topology counts, surface histogram, sorted feature multiset) -- two parts with
  the SAME geom_hash are the same shape (to the rounding below), placement irrelevant.
- `moments_vec(record)`: a 64-float, L2-normalised, scale-free descriptor (moment/rgyr
  ratios, the four shape_ratios, per-surface-kind face fractions) for a coarse, fast
  "same general shape family" nearest-neighbour search.
- `d2_histogram(shape, ...)`: the Osada et al. 2002 D2 shape distribution -- the histogram
  of pairwise distances between random points sampled on the tessellated surface (area-
  weighted), normalised by their own mean and by bin count -- a MUCH stronger shape
  descriptor (order-independent, robust to remeshing) for "parts shaped like X".

Determinism: `d2_histogram` uses a fixed-seed `numpy.random.default_rng` and a
deflection-controlled tessellation, so calling it twice on the same shape object gives
byte-identical output; two independently-loaded occurrences of the SAME physical geometry
(e.g. this catalogue's two identical connecting rods) give histograms within noise of each
other (their d2_v1 vectors' L2 distance is small, not necessarily exactly 0 -- their own
mesh/random-point realisations differ slightly whenever the underlying tessellation isn't
byte-identical across two separately-loaded TopoDS shapes).
"""
import math

from framework.common.shape_signature import (  # noqa: F401  (kept in framework/common, re-imported by name)
    MOMENTS_VEC_LEN, _DIA_DECIMALS, _SIG_FIGS, _SURFACE_KINDS, _feature_key, _round_sig, geom_hash, moments_vec)

D2_BINS = 64


def d2_histogram(shape, bins=D2_BINS, n_points=8192, n_pairs=400000, seed=0):
    """Osada et al. 2002 D2 shape distribution: tessellate `shape` (BRepMesh_
    IncrementalMesh at a deflection scaled to the shape's own bounding-box diagonal, so
    sampling density is comparable across very different part sizes), sample `n_points`
    random points on its surface weighted by triangle area, draw `n_pairs` random pairs
    (with replacement, fixed RNG `seed`) and histogram their pairwise distances -- each
    normalised by the pairs' own mean distance -- over [0, 3) into `bins` bins, normalised
    to sum to 1.0. Deterministic for a given TopoDS shape (fixed tessellation deflections
    + fixed-seed `numpy.random.default_rng`). Returns `bins` floats; an unmeshable or
    zero-area shape returns all-zero (never raises).

    `n_points`/`n_pairs` (8192 / 400,000) were sized
    empirically against this catalogue's own known-identical pair (two
    identical rods of one design, exact geom_hash duplicates): the smaller Osada-paper-typical 4096/
    200,000 gave a rod1-vs-rod2 L2 distance of ~1.2e-3 -- noise from the two shapes'
    independently-realised random point clouds, not a real shape difference -- above the
    1e-3 "near-duplicate" acceptance band; 8192/400,000 brings the SAME pair to ~6.6e-4,
    ~15x below the ~9.7e-2 distance to an unrelated part at the same sample
    size, while still running in ~30ms/part (~5s for this dev catalogue's 172 parts)."""
    import numpy as np
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    zeros = [0.0] * bins

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    box.SetGap(0.0)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    diag = math.sqrt((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2)
    if diag <= 0:
        return zeros
    linear_defl = max(diag * 0.004, 1e-4)
    angular_defl = 0.3

    BRepMesh_IncrementalMesh(shape, linear_defl, False, angular_defl, True)

    p0s, p1s, p2s = [], [], []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            reversed_face = face.Orientation() == TopAbs_REVERSED
            nodes = [tri.Node(i).Transformed(trsf) for i in range(1, tri.NbNodes() + 1)]
            pts = [(p.X(), p.Y(), p.Z()) for p in nodes]
            for i in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(i).Get()
                if reversed_face:
                    b, c = c, b
                p0s.append(pts[a - 1])
                p1s.append(pts[b - 1])
                p2s.append(pts[c - 1])
        exp.Next()

    if not p0s:
        return zeros

    p0 = np.array(p0s, dtype=float)
    p1 = np.array(p1s, dtype=float)
    p2 = np.array(p2s, dtype=float)
    cross = np.cross(p1 - p0, p2 - p0)
    areas = 0.5 * np.linalg.norm(cross, axis=1)
    total_area = float(areas.sum())
    if total_area <= 0:
        return zeros
    probs = areas / total_area

    rng = np.random.default_rng(seed)
    tri_idx = rng.choice(len(p0), size=n_points, p=probs)
    u = rng.random(n_points)
    v = rng.random(n_points)
    swap = (u + v) > 1.0
    u[swap] = 1.0 - u[swap]
    v[swap] = 1.0 - v[swap]
    w = 1.0 - u - v
    pts = (p0[tri_idx] * u[:, None] + p1[tri_idx] * v[:, None] + p2[tri_idx] * w[:, None])

    i_idx = rng.integers(0, n_points, size=n_pairs)
    j_idx = rng.integers(0, n_points, size=n_pairs)
    keep = i_idx != j_idx
    i_idx, j_idx = i_idx[keep], j_idx[keep]
    if i_idx.size == 0:
        return zeros
    dists = np.linalg.norm(pts[i_idx] - pts[j_idx], axis=1)
    mean_d = float(dists.mean())
    if mean_d <= 0:
        return zeros
    normalized = np.clip(dists / mean_d, 0.0, 3.0 - 1e-9)

    hist, _edges = np.histogram(normalized, bins=bins, range=(0.0, 3.0))
    hist = hist.astype(float)
    total = float(hist.sum())
    if total <= 0:
        return zeros
    return (hist / total).tolist()
