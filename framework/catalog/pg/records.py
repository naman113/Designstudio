"""Read the Postgres catalogue fact tables back into the SAME shapes the kernel/SQLite
catalogue produces, so a parity check written against the kernel's records can be
pointed at Postgres facts without knowing anything about the schema.

part_record(con, part_id) rebuilds a `framework.kernel.measure.measure()`-shaped dict
from CURRENT facts only (`valid_to IS NULL`) -- the exact inverse of
`framework.catalog.pg.load.write_part`'s per-key mapping (`record_map()["part"]` /
`["feature"]`, both bijective). product_rows(con, product_id) reads a product's current
BOM and interfaces back in the SQLite column shapes, so they compare directly with the
SQLite catalogue's rows.
"""
from framework.catalog.pg import load as _load


def _fact_value(datatype, value_num, value_text, value_vec, value_json):
    if datatype == "int":
        return int(value_num) if value_num is not None else None
    if datatype == "real":
        return value_num
    if datatype == "vector":
        return list(value_vec) if value_vec is not None else None
    if datatype == "map":
        return dict(value_json) if isinstance(value_json, dict) else value_json
    if datatype == "bool":
        return value_text == "true" if value_text is not None else None
    return value_text  # text / enum


def _facts_by_prop(con, kind, entity_id):
    rows = con.execute(
        """SELECT property_id, value_num, value_text, value_vec, value_json
           FROM fact WHERE entity_kind=%s AND entity_id=%s AND valid_to IS NULL""",
        (kind, entity_id),
    ).fetchall()
    props = _load._properties()
    return {prop_id: _fact_value(props[prop_id]["datatype"], vn, vt, vv, vj)
            for prop_id, vn, vt, vv, vj in rows}


def part_record(con, part_id):
    """Rebuild the kernel record for `part_id` from current facts -- None when the
    part does not exist. `features` comes back in ordinal order, each with the LEGACY
    (abs) `axis`/`semi_angle_deg` keys the parity suite's diff_part already knows how
    to read (record_map()["feature"] maps `axis` -> `feat.axis_legacy`, so the reverse
    lookup below naturally produces the legacy name; the signed keys come back
    too, under `axis_signed`/`semi_angle_signed_deg`, unused by the legacy readers but harmless)."""
    row = con.execute(
        "SELECT object_ref, label, type_id, units FROM part_revision WHERE part_id=%s",
        (part_id,),
    ).fetchone()
    if row is None:
        return None
    object_ref, label, type_id, units = row
    rec = {"object": object_ref, "label": label, "type_id": type_id, "units": units}

    reverse = {prop_id: key for key, prop_id in _load.record_map()["part"].items()}
    for prop_id, value in _facts_by_prop(con, "part", part_id).items():
        key = reverse.get(prop_id)
        if key is None:
            continue  # a derived-only scalar (range-index only), not a record key
        rec[key] = value

    freverse = {prop_id: key for key, prop_id in _load.record_map()["feature"].items()}
    features = []
    for feature_id, kind, role in con.execute(
            "SELECT feature_id, kind, role FROM feature WHERE part_id=%s ORDER BY ordinal",
            (part_id,)).fetchall():
        feat = {"kind": kind, "role": role}
        for prop_id, value in _facts_by_prop(con, "feature", feature_id).items():
            key = freverse.get(prop_id)
            if key is not None:
                feat[key] = value
        features.append(feat)
    rec["features"] = features
    return rec


def product_rows(con, product_id):
    """-> (bom, interfaces). bom: [{"ref_des", "part_id", "parent_ref", "qty"}] (qty =
    count of occurrence instances for that ref_des). interfaces: SQLite-column-
    shaped dicts (kind, ref_a, ref_b, nominal_a_mm, nominal_b_mm, clearance_mm,
    fit_class, contact_area_mm2, axis_x/y/z, pos_x/y/z) in ordinal order."""
    bom = [
        {"ref_des": ref_des, "part_id": part_id, "parent_ref": parent_ref, "qty": qty}
        for ref_des, part_id, parent_ref, qty in con.execute(
            """SELECT ref_des, part_id, parent_ref, COUNT(*) FROM occurrence
               WHERE product_id=%s GROUP BY ref_des, part_id, parent_ref ORDER BY ref_des""",
            (product_id,)).fetchall()
    ]

    interfaces = []
    for ordinal, kind, ref_a, ref_b in con.execute(
            "SELECT ordinal, kind, ref_a, ref_b FROM interface WHERE product_id=%s ORDER BY ordinal",
            (product_id,)).fetchall():
        interface_id = "%s/i%d" % (product_id, ordinal)
        f = _facts_by_prop(con, "interface", interface_id)
        axis = f.get("iface.axis") or [None, None, None]
        pos = f.get("iface.position") or [None, None, None]
        interfaces.append({
            "kind": kind, "ref_a": ref_a, "ref_b": ref_b,
            "nominal_a_mm": f.get("iface.nominal_a"), "nominal_b_mm": f.get("iface.nominal_b"),
            "clearance_mm": f.get("iface.clearance"), "fit_class": f.get("iface.fit_class"),
            "contact_area_mm2": f.get("iface.contact_area"),
            "axis_x": axis[0], "axis_y": axis[1], "axis_z": axis[2],
            "pos_x": pos[0], "pos_y": pos[1], "pos_z": pos[2],
        })
    return bom, interfaces
