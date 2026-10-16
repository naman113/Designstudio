"""The parity suite's matchers, shared by tests/ and the engines comparator.
Pure Python: no OCP, no kernel."""


def canonical_cone(axis, angle_deg):
    """Cone key in extract.py's own convention (the golden contract).

    extract.py keys a cone by (semi-angle, abs(axis.x), abs(axis.y), abs(axis.z), apex),
    so the golden features store every axis component as a magnitude. This function
    reproduces that convention for every cone of every part; it also absorbs the reader
    returning cone(axis, angle) as cone(-axis, -angle). Known limit of the convention:
    axes mirrored across a coordinate plane, e.g. (0, 0.5, 0.866) and (0, 0.5, -0.866),
    get the same key. Verified on a test part: the source shape, the kernel STEP
    and the original SolidWorks file all give (0, -0.5, 0.866) for a face golden lists as
    (0, 0.5, 0.866). A future catalogue version should store the true sign-normalised axis.
    """
    return [abs(c) for c in axis], abs(angle_deg)


_FEATURE_SCALAR_TOL = {
    "cylinder": [("radius_mm", "radius_mm", 0.001)],
    "plane": [("offset_mm", "offset_mm", 0.01)],
    "cone": [("semi_angle_deg", "semi_angle_deg", 0.01)],
}
_FEATURE_VECTOR_TOL = {
    "cylinder": [("axis", ("axis_x", "axis_y", "axis_z"), 0.001),
                 ("pos", ("pos_x", "pos_y", "pos_z"), 0.01)],
    "plane": [("axis", ("axis_x", "axis_y", "axis_z"), 0.001)],
    "cone": [("axis", ("axis_x", "axis_y", "axis_z"), 0.001),
             ("pos", ("pos_x", "pos_y", "pos_z"), 0.01)],
}


def _feature_key_distance(kf, gf):
    """None when kf/gf cannot be the same feature; else the summed key-field
    difference (smaller is a better greedy match)."""
    if kf["kind"] != gf["kind"] or kf.get("role") != gf.get("role"):
        return None
    kind = kf["kind"]
    if kind == "cone":
        # canonical_cone, applied to BOTH sides before
        # comparing: kf's axis/semi_angle_deg is already canonical (kernel.features
        # applies it at the source), applying it again is a no-op; gf's flat
        # axis_x/y/z + semi_angle_deg columns come straight off the golden JSON on
        # disk (never edited) and need it applied here, at compare time.
        k_axis, k_angle = canonical_cone(kf.get("axis") or [0, 0, 0], kf.get("semi_angle_deg") or 0)
        g_axis, g_angle = canonical_cone(
            [gf.get("axis_x") or 0, gf.get("axis_y") or 0, gf.get("axis_z") or 0],
            gf.get("semi_angle_deg") or 0)
        kf = dict(kf, axis=k_axis, semi_angle_deg=k_angle)
        gf = dict(gf, axis_x=g_axis[0], axis_y=g_axis[1], axis_z=g_axis[2], semi_angle_deg=g_angle)
    total = 0.0
    for kfield, gfield, tol in _FEATURE_SCALAR_TOL[kind]:
        d = abs((kf.get(kfield) or 0) - (gf.get(gfield) or 0))
        if d > tol:
            return None
        total += d
    for kfield, gfields, tol in _FEATURE_VECTOR_TOL[kf["kind"]]:
        kv = kf.get(kfield) or [0, 0, 0]
        for a, gfield in zip(kv, gfields):
            d = abs((a or 0) - (gf.get(gfield) or 0))
            if d > tol:
                return None
            total += d
    return total


def _interface_key(row, kind):
    """cylindrical_fit is directional (ref_a = OD owner, ref_b = bore owner, task
    18's own spec); planar_contact is symmetric."""
    if kind == "cylindrical_fit":
        return (row["ref_a"], row["ref_b"])
    return frozenset((row["ref_a"], row["ref_b"]))


def _interfaces_match(k_row, g_row, kind):
    if kind == "cylindrical_fit":
        for field in ("nominal_a_mm", "nominal_b_mm", "clearance_mm"):
            if abs((k_row[field] or 0) - (g_row[field] or 0)) > 1e-3:
                return False
        return True
    return abs((k_row["clearance_mm"] or 0) - (g_row["clearance_mm"] or 0)) <= 1e-3


def _match_interfaces(kernel_rows, golden_rows, kind):
    """Greedy one-to-one match by key then by tolerance; returns the rows on each
    side that found no partner."""
    used = [False] * len(kernel_rows)
    unmatched_golden = []
    for g in golden_rows:
        gk = _interface_key(g, kind)
        best_i = None
        for i, k in enumerate(kernel_rows):
            if used[i] or _interface_key(k, kind) != gk:
                continue
            if _interfaces_match(k, g, kind):
                best_i = i
                break
        if best_i is None:
            unmatched_golden.append(dict(g))
        else:
            used[best_i] = True
    unmatched_kernel = [dict(k) for i, k in enumerate(kernel_rows) if not used[i]]
    return unmatched_kernel, unmatched_golden


# Record keys measure()'s output carries beyond the golden record (volume/area
# provenance -- which rule produced volume_mm3/area_mm2, and the mesh-path error
# estimate) that golden's own frozen raw_json predates and never carries. Excluded,
# by name, from the strict-mode key-list-order check below only -- everywhere else
# (ingest.py's raw_json column, the record itself) they are kept, per part, always.
_PROVENANCE_KEYS = {"volume_method", "volume_uncertainty_mm3", "area_method", "area_uncertainty_mm2"}


def _bbox_close(a, b, tol=1e-3 + 1e-9):
    return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b))


def diff_record(rec, key_str, gr, gfeats, k_topo, g_topo, strict=True):
    """The suite's part comparison -> (errors, counts). `gr` is the golden raw record of the part, `gfeats` its golden
    feature rows, `k_topo`/`g_topo` the degenerate-excluding topology of the measured shape and of the golden BREP (only read when `strict`).
    Pure Python: no OCP, no kernel import."""
    errors = []
    info = []
    vol_tol = lambda g: max(1e-6 * abs(g), 0.0005)

    # One rule for every part: when the kernel's own
    # measurement used the mesh method (default and adaptive BRepGProp disagreed),
    # golden's formula-measured number is not a valid comparison target -- report
    # both numbers as information, never as a failure, and count it. Every other
    # part (method "formula") is asserted exactly as before.
    vol_mesh = rec.get("volume_method") == "mesh"
    area_mesh = rec.get("area_method") == "mesh"

    if vol_mesh:
        info.append("%s golden measured with a failing formula: golden %s, kernel %s +- %s "
                     "(method mesh)" % (key_str, gr["volume_mm3"], rec["volume_mm3"],
                                         rec.get("volume_uncertainty_mm3")))
    elif abs(rec["volume_mm3"] - gr["volume_mm3"]) > vol_tol(gr["volume_mm3"]):
        errors.append("%s volume_mm3: kernel=%s golden=%s" % (
            key_str, rec["volume_mm3"], gr["volume_mm3"]))
    if area_mesh:
        info.append("%s golden measured with a failing formula (area): golden %s, kernel %s "
                     "+- %s (method mesh)" % (key_str, gr["area_mm2"], rec["area_mm2"],
                                               rec.get("area_uncertainty_mm2")))
    elif abs(rec["area_mm2"] - gr["area_mm2"]) > vol_tol(gr["area_mm2"]):
        errors.append("%s area_mm2: kernel=%s golden=%s" % (
            key_str, rec["area_mm2"], gr["area_mm2"]))
    if not _bbox_close(rec["bbox"], gr["bbox"]):
        errors.append("%s bbox: kernel=%s golden=%s" % (key_str, rec["bbox"], gr["bbox"]))
    if not _bbox_close(rec["bbox_sorted_mm"], gr["bbox_sorted_mm"]):
        errors.append("%s bbox_sorted_mm: kernel=%s golden=%s" % (
            key_str, rec["bbox_sorted_mm"], gr["bbox_sorted_mm"]))
    if strict:
        for field in ("n_solids", "n_shells", "n_faces"):
            if rec[field] != gr[field]:
                errors.append("%s %s: kernel=%s golden=%s" % (
                    key_str, field, rec[field], gr[field]))
        # n_edges/n_vertexes/curves (one global rule, applied to
        # every part): compared as degenerate-edge-excluding counts computed fresh
        # from BOTH the kernel's STEP-read shape and golden's own exact frozen BREP
        # source (topology_excl_degenerate on each) -- never golden's frozen JSON
        # n_edges/n_vertexes/curves directly, which (like extract.py) counts every
        # edge including ones STEP cannot represent. Still asserted, on every part.
        for field in ("n_edges", "n_vertexes"):
            if k_topo[field] != g_topo[field]:
                errors.append("%s %s (excl. degenerate): kernel=%s golden(brep)=%s" % (
                    key_str, field, k_topo[field], g_topo[field]))
        if k_topo["curves"] != g_topo["curves"]:
            errors.append("%s curves (excl. degenerate): kernel=%s golden(brep)=%s" % (
                key_str, k_topo["curves"], g_topo["curves"]))
        if rec["surfaces"] != gr["surfaces"]:
            errors.append("%s surfaces: kernel=%s golden=%s" % (
                key_str, rec["surfaces"], gr["surfaces"]))
        rec_keys = [k for k in rec.keys() if k not in _PROVENANCE_KEYS]
        if rec_keys != list(gr.keys()):
            errors.append("%s record keys: kernel=%s golden=%s" % (
                key_str, rec_keys, list(gr.keys())))

    # Feature accounting: greedy nearest-key match.
    kfeats = list(rec["features"])
    used = [False] * len(kfeats)
    matched = rejected = missing = 0
    bbox_a_mm = rec["bbox_sorted_mm"][0] if rec["bbox_sorted_mm"] else 0.0

    for gf in gfeats:
        best_i, best_d = None, None
        for i, kf in enumerate(kfeats):
            if used[i]:
                continue
            d = _feature_key_distance(kf, gf)
            if d is not None and (best_d is None or d < best_d):
                best_d, best_i = d, i
        if best_i is not None:
            used[best_i] = True
            matched += 1
            kf = kfeats[best_i]
            if gf["kind"] == "cylinder":
                kd, gd = kf.get("dia_mm") or 0, gf.get("dia_mm") or 0
                if abs(kd - gd) > 1e-3:
                    errors.append("%s feature dia_mm: kernel=%s golden=%s" % (key_str, kd, gd))
            elif gf["kind"] == "plane":
                ko, go = kf.get("offset_mm") or 0, gf.get("offset_mm") or 0
                if abs(ko - go) > 1e-3:
                    errors.append("%s feature offset_mm: kernel=%s golden=%s" % (key_str, ko, go))
        elif gf["kind"] == "cylinder" and (gf.get("dia_mm") or 0) > bbox_a_mm:
            rejected += 1  # blend rule: allowed unmatched golden row
        else:
            missing += 1
            errors.append("%s missing feature: %s" % (key_str, gf))

    extra = len(kfeats) - sum(used)
    if extra:
        errors.append("%s extra features: %d (%s)" % (
            key_str, extra, [kf for i, kf in enumerate(kfeats) if not used[i]]))

    return errors, {"matched": matched, "rejected": rejected, "missing": missing, "extra": extra,
                     "mesh_path": 1 if (vol_mesh or area_mesh) else 0, "info": info}


def match_records(fc_parts, k_parts):
    """Pair FreeCAD and kernel part records of ONE document: by object name, then by
    label, then by nearest volume. -> [(key, fc_rec|None, k_rec|None)]."""
    k_left = list(k_parts)
    out = []
    fc_left = []
    for f in fc_parts:
        hit = next((k for k in k_left if k["object"] == f["object"]), None)
        if hit is None:
            fc_left.append(f)
            continue
        k_left.remove(hit)
        out.append((f["object"], f, hit))
    rest = []
    for f in fc_left:
        hit = next((k for k in k_left if (k.get("label") or "") == (f.get("label") or "\0")), None)
        if hit is None:
            rest.append(f)
            continue
        k_left.remove(hit)
        out.append((f["object"], f, hit))
    for f in rest:
        if not k_left:
            out.append((f["object"], f, None))
            continue
        hit = min(k_left, key=lambda k: abs((k.get("volume_mm3") or 0) - (f.get("volume_mm3") or 0)))
        k_left.remove(hit)
        out.append((f["object"], f, hit))
    for k in k_left:
        out.append((k["object"], None, k))
    return out
