"""Family rules (framework/catalog/families.json, owned by the catalogue; read here)
and the classifier, shared with framework/catalog/ingest.py."""
import json
import os
import re

FAMILIES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "catalog", "families.json")

SURF_COLS = {"Plane": "n_plane", "Cylinder": "n_cylinder", "Cone": "n_cone",
             "Sphere": "n_sphere", "Toroid": "n_torus", "BSplineSurface": "n_bspline"}


def load_rules():
    with open(FAMILIES, encoding="utf-8") as fh:
        rules = json.load(fh)["rules"]
    for r in rules:
        if r.get("label_matches"):
            re.compile(r["label_matches"])   # fail loudly here, not mid-ingest
    return rules


def classify(rec, rules):
    """First matching rule wins. Returns (family, why)."""
    flat = dict(rec)
    flat.update(rec.get("shape_ratios") or {})
    surf = rec.get("surfaces") or {}
    for name, col in SURF_COLS.items():
        flat[col] = surf.get(name, 0)
    flat["n_other_surf"] = sum(v for k, v in surf.items() if k not in SURF_COLS)
    dims = (rec.get("bbox_sorted_mm") or []) + [0, 0, 0]
    flat["bbox_a_mm"], flat["bbox_b_mm"], flat["bbox_c_mm"] = dims[:3]
    for rule in rules:
        lm = rule.get("label_matches")
        if lm and not re.search(lm, rec.get("label") or ""):
            continue
        ok = True
        for p in rule.get("all", []):
            v = flat.get(p["field"])
            if v is None:
                ok = False
                break
            for op, target in p.items():
                if op == "field":
                    continue
                if (op == "lt" and not v < target) or (op == "lte" and not v <= target) \
                        or (op == "gt" and not v > target) or (op == "gte" and not v >= target) \
                        or (op == "eq" and not v == target) \
                        or (op == "between" and not (target[0] <= v <= target[1])):
                    ok = False
            if not ok:
                break
        if ok:
            return rule["family"], rule.get("why", "")
    return "unclassified", "no rule matched"
