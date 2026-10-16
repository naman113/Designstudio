"""The two pure shape signatures, shared by the catalogue and the geometry service.

`geom_hash(record)` (an exact-duplicate hash) and `moments_vec(record)` (a 64-float coarse shape descriptor) work on a
`framework.kernel.measure.measure()`-shaped record (or its Postgres-fact reconstruction): hashlib, json and math only,
no kernel, no database. `framework/kernel/signature.py` re-imports them by name; the third
descriptor, `d2_histogram`, needs a shape and stays in the kernel. See that file's docstring for what each one means.
"""
import hashlib
import json
import math

_SIG_FIGS = 6  # volume/area/moments/radii of gyration: 1e-6 relative
_DIA_DECIMALS = 3  # feature diameters: 1e-3 mm absolute

# Fixed, ordered surface-kind buckets (mirrors framework/catalog/pg/load.py's own
# _SURF_DERIVED list) -- everything else in a part's `surfaces` histogram folds into
# "Other". Order matters: it fixes which dimension of moments_vec each fraction lands in.
_SURFACE_KINDS = ("Plane", "Cylinder", "Cone", "Sphere", "Toroid", "BSplineSurface")

MOMENTS_VEC_LEN = 64


def _round_sig(x, sig=_SIG_FIGS):
    """Round `x` to `sig` significant digits (1e-6 relative for sig=6). None-safe."""
    if x is None:
        return None
    x = float(x)
    if x == 0.0:
        return 0.0
    d = sig - int(math.floor(math.log10(abs(x)))) - 1
    return round(x, d)


def _feature_key(feat):
    kind = feat.get("kind")
    role = feat.get("role")
    dia = feat.get("dia_mm")
    dia_r = round(float(dia), _DIA_DECIMALS) if dia is not None else None
    return (kind or "", role or "", dia_r if dia_r is not None else -1.0)


def geom_hash(record):
    """-> "inv_v1:<sha256 hex>" over a canonical, rotation/translation-invariant subset of
    `record` (a `measure()`-shaped dict, or its Postgres-fact reconstruction). Two parts
    with the identical shape (to the rounding above) get the identical hash regardless of
    placement, object name, doc, or which product occurrence references them."""
    moments = sorted(_round_sig(v) for v in (record.get("moments") or []))
    rgyr = sorted(_round_sig(v) for v in (record.get("radius_of_gyration") or []))
    surfaces = record.get("surfaces") or {}
    features = [list(_feature_key(f)) for f in (record.get("features") or [])]
    features.sort()

    canonical = {
        "volume": _round_sig(record.get("volume_mm3")),
        "area": _round_sig(record.get("area_mm2")),
        "moments": moments,
        "rgyr": rgyr,
        "n_solids": record.get("n_solids"),
        "n_shells": record.get("n_shells"),
        "n_faces": record.get("n_faces"),
        "n_edges": record.get("n_edges"),
        "n_vertexes": record.get("n_vertexes"),
        "surfaces": sorted((k, v) for k, v in surfaces.items() if v),
        "features": features,
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return "inv_v1:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def moments_vec(record, length=MOMENTS_VEC_LEN):
    """-> `length` floats (default 64, matching `signature.vec vector(64)`): scale-free
    shape descriptors (moment ratios, radius-of-gyration ratios, the four shape_ratios,
    per-surface-kind face fractions), zero-padded, L2-normalised. Coarser than
    `d2_histogram` but cheap and DB-free (no shape needed -- computable from facts alone),
    used for a fast first-pass "same general proportions" search."""
    moments = record.get("moments") or []
    rgyr = record.get("radius_of_gyration") or []
    vals = []
    if len(moments) >= 3 and moments[0]:
        vals += [moments[1] / moments[0], moments[2] / moments[0]]
    else:
        vals += [0.0, 0.0]
    if len(rgyr) >= 3 and rgyr[0]:
        vals += [rgyr[1] / rgyr[0], rgyr[2] / rgyr[0]]
    else:
        vals += [0.0, 0.0]

    sr = record.get("shape_ratios") or {}
    vals += [
        sr.get("aspect_1") or 0.0, sr.get("aspect_2") or 0.0,
        sr.get("fill") or 0.0, sr.get("compactness") or 0.0,
    ]

    surfaces = record.get("surfaces") or {}
    n_faces = record.get("n_faces") or sum(surfaces.values()) or 0
    if n_faces:
        vals += [surfaces.get(k, 0) / n_faces for k in _SURFACE_KINDS]
        vals.append(sum(v for k, v in surfaces.items() if k not in _SURFACE_KINDS) / n_faces)
    else:
        vals += [0.0] * (len(_SURFACE_KINDS) + 1)

    vec = [float(x) for x in vals[:length]]
    vec += [0.0] * (length - len(vec))
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec
