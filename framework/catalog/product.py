"""Products: a designed thing spanning any number of files.

    python product.py load  <manifest.json>     register/refresh a product, detect interfaces
    python product.py list
    python product.py show  <product_id>        BOM + interfaces

Manifest format (JSON):
{
  "product_id": "bracket-assembly", "name": "...", "revision": "A", "description": "...",
  "parts": [
    {"ref": "BLOCK", "file": "path/to.FCStd", "object": "Common", "qty": 1, "parent": null}
  ]
}
Paths may be absolute or relative to the manifest. A file not yet in the
catalogue is ingested on the spot.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest  # noqa: E402
from framework.common import paths  # noqa: E402  (stored host paths open where they live)

# --- interface detection tolerances -----------------------------------------
FIT_MAX_CLEARANCE_MM = 2.0   # beyond this a shaft in a bore is a gap, not a fit
FIT_AXIS_TOL = 0.02          # direction cosines
FIT_POS_TOL_MM = 0.5         # how far apart two axis lines may sit and still be coaxial
CONTACT_GAP_MM = 0.25        # planar faces closer than this are touching
CONTACT_MIN_OVERLAP_MM2 = 4.0


def fit_class(clearance):
    if clearance < 0:
        return "interference"
    if clearance <= 0.02:
        return "transition"
    return "clearance"


def _overlap_area(a, b):
    """Overlap of two feature bboxes projected onto the plane they share.
    Good enough to reject faces that are coplanar but nowhere near each other."""
    dx = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    dy = max(0.0, min(a[3], b[3]) - max(a[2], b[2]))
    dz = max(0.0, min(a[5], b[5]) - max(a[4], b[4]))
    dims = sorted([dx, dy, dz], reverse=True)
    return dims[0] * dims[1]


def detect_interfaces(con, product_id):
    con.execute("DELETE FROM interfaces WHERE product_id=?", (product_id,))
    con.row_factory = sqlite3.Row
    feats = con.execute("""
        SELECT pp.ref_des, f.* FROM product_parts pp
        JOIN features f ON f.part_id = pp.part_id
        WHERE pp.product_id=?""", (product_id,)).fetchall()
    cyl_od = [f for f in feats if f["kind"] == "cylinder" and f["role"] == "od"]
    cyl_bore = [f for f in feats if f["kind"] == "cylinder" and f["role"] == "bore"]
    planes = [f for f in feats if f["kind"] == "plane"]
    n = 0

    # cylindrical fits: OD in a bore, coaxial, same product, different parts
    for o in cyl_od:
        for b in cyl_bore:
            if o["ref_des"] == b["ref_des"]:
                continue
            gap = (b["dia_mm"] or 0) - (o["dia_mm"] or 0)
            if gap < -0.5 or gap > FIT_MAX_CLEARANCE_MM:
                continue
            if any(abs((o[k] or 0) - (b[k] or 0)) > FIT_AXIS_TOL for k in ("axis_x", "axis_y", "axis_z")):
                continue
            if any(abs((o[k] or 0) - (b[k] or 0)) > FIT_POS_TOL_MM for k in ("pos_x", "pos_y", "pos_z")):
                continue
            # they must actually overlap along the axis
            ob = [o["bb_xmin"], o["bb_xmax"], o["bb_ymin"], o["bb_ymax"], o["bb_zmin"], o["bb_zmax"]]
            bb = [b["bb_xmin"], b["bb_xmax"], b["bb_ymin"], b["bb_ymax"], b["bb_zmin"], b["bb_zmax"]]
            if None in ob or None in bb or _overlap_area(ob, bb) <= 0:
                continue
            con.execute("""INSERT INTO interfaces (product_id, kind, ref_a, feature_a, ref_b, feature_b,
                nominal_a_mm, nominal_b_mm, clearance_mm, fit_class, contact_area_mm2,
                axis_x, axis_y, axis_z, pos_x, pos_y, pos_z)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (product_id, "cylindrical_fit", o["ref_des"], o["feature_id"], b["ref_des"], b["feature_id"],
                 o["dia_mm"], b["dia_mm"], round(gap, 4), fit_class(gap), None,
                 o["axis_x"], o["axis_y"], o["axis_z"], o["pos_x"], o["pos_y"], o["pos_z"]))
            n += 1

    # planar contacts: antiparallel outward normals, same plane, overlapping
    for i, a in enumerate(planes):
        for b in planes[i + 1:]:
            if a["ref_des"] == b["ref_des"]:
                continue
            dot = (a["axis_x"] or 0) * (b["axis_x"] or 0) + (a["axis_y"] or 0) * (b["axis_y"] or 0) \
                + (a["axis_z"] or 0) * (b["axis_z"] or 0)
            if dot > -0.999:
                continue
            gap = abs((a["offset_mm"] or 0) + (b["offset_mm"] or 0))
            if gap > CONTACT_GAP_MM:
                continue
            ab = [a["bb_xmin"], a["bb_xmax"], a["bb_ymin"], a["bb_ymax"], a["bb_zmin"], a["bb_zmax"]]
            bb = [b["bb_xmin"], b["bb_xmax"], b["bb_ymin"], b["bb_ymax"], b["bb_zmin"], b["bb_zmax"]]
            if None in ab or None in bb:
                continue
            ov = _overlap_area(ab, bb)
            if ov < CONTACT_MIN_OVERLAP_MM2:
                continue
            con.execute("""INSERT INTO interfaces (product_id, kind, ref_a, feature_a, ref_b, feature_b,
                nominal_a_mm, nominal_b_mm, clearance_mm, fit_class, contact_area_mm2,
                axis_x, axis_y, axis_z, pos_x, pos_y, pos_z)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (product_id, "planar_contact", a["ref_des"], a["feature_id"], b["ref_des"], b["feature_id"],
                 None, None, round(gap, 4), "contact", round(ov, 2),
                 a["axis_x"], a["axis_y"], a["axis_z"], a["pos_x"], a["pos_y"], a["pos_z"]))
            n += 1
    con.commit()
    return n


def load(manifest_path, db=None, engine=None, measure=None, d2_provider=None):
    """Register/refresh a product from its manifest and detect its interfaces.
    Returns (n_parts, n_interfaces, missing) -- missing is the list of manifest
    entries whose object was not found as a part (ref, object, path).

    This module does not import the engines layer. One engine's measurements fill the
    catalogue: `engine` "freecad" is the first engine's (its documents, `fc_file`), anything else the kernel's.
    `measure(path, side)` (the catalogue service passes one that submits measuring jobs) does the measuring; without
    it a document is measured in-process with the kernel only (tests, offline), and the first engine needs `measure`.
    The `product.load` recipe (framework/orchestrator/recipes.py) runs the two engines' loads and their comparison.
    `d2_provider` says where the shape signatures of the new parts come from (pg/signatures.backfill).

    Postgres: the writer connection mirrors the change when load_side closes it
    (ingest.CatalogueConnection), so this function, load_side and every other writer are covered."""
    return load_side(manifest_path, db, "freecad" if engine == "freecad" else "kernel", measure, d2_provider)


def _entry_source(e, base, side):
    """(path, object) of a manifest entry for one engine: FreeCAD reads fc_file/fc_object
    when present (the .FCStd a part was designed in), the kernel reads file/object.
    The path is in ingest.canon_path form."""
    if side == "freecad":
        f, obj = e.get("fc_file") or e["file"], e.get("fc_object") or e["object"]
    else:
        f, obj = e["file"], e["object"]
    path = f if os.path.isabs(f) else os.path.join(base, f)
    return ingest.canon_path(path), obj


def load_side(manifest_path, db=None, side="kernel", measure=None, d2_provider=None, progress=None):
    """product.load with ONE engine's measurements (side = "kernel" | "freecad"); `measure` / `d2_provider` as in `load`.
    `progress(step)` (the catalogue service's job progress) is told "fits" and "mirror" as the load reaches them."""
    with open(manifest_path, encoding="utf-8") as fh:
        m = json.load(fh)
    base = os.path.dirname(os.path.abspath(manifest_path))
    con = ingest.connect(db=db)
    try:
        return _load_side(con, m, base, side, manifest_path, measure, d2_provider, progress)
    except BaseException:
        # never leave the write transaction open: an open failed connection keeps the whole
        # catalogue locked until garbage collection
        try:
            con.rollback()
            con.close()
        except Exception:
            pass
        raise


def _load_side(con, m, base, side, manifest_path, measure=None, d2_provider=None, progress=None):
    rules = ingest.load_rules()
    pid = m["product_id"]
    try:
        con.mirror_label = ("after-product", pid)      # closing con mirrors this product
        con.mirror_extra = {"d2_provider": d2_provider}
    except AttributeError:
        pass
    con.execute("INSERT OR REPLACE INTO products VALUES (?,?,?,?,?,?)",
                (pid, m.get("name", pid), m.get("revision"), m.get("description"),
                 os.path.abspath(manifest_path), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    con.execute("DELETE FROM product_parts WHERE product_id=?", (pid,))
    missing = []
    docs_of = {}                                       # file -> doc_id, once per file in this load
    for e in m["parts"]:
        path, obj = _entry_source(e, base, side)
        doc_id = docs_of.get(path)
        if doc_id is None:
            # the document of this file under ANY spelling of its path: reused when its
            # checksum is the file's, else the file is re-ingested (store() replaces it)
            fresh = ingest.sha256(path)
            same = [d for d, sha, _p in ingest.find_documents(con, path) if sha == fresh]
            if not same:
                # a document is the file's content (doc_id = its checksum): the same file stored under another
                # path (a design uploaded again, as a second design) is the document already there, with its measured numbers
                same = [r[0] for r in con.execute("SELECT doc_id FROM documents WHERE doc_id=? AND checksum_sha256=?",
                                                  (ingest.doc_id_for(path, fresh), fresh))]
            if not same:
                print("  ingesting %s" % path)
                doc_id, _ = ingest.ingest_file(path, con, rules, side=side, measure=measure)
            else:
                doc_id = same[0]
                ingest.drop_stale_documents(con, path, keep=doc_id)   # older copies nothing uses
            docs_of[path] = doc_id
        part_id = "%s:%s" % (doc_id, obj)
        if con.execute("SELECT 1 FROM parts WHERE part_id=?", (part_id,)).fetchone() is None:
            # a STEP read by FreeCAD names objects after the STEP product names (Label)
            hit = con.execute("SELECT part_id FROM parts WHERE doc_id=? AND label=?", (doc_id, obj)).fetchone()
            if hit:
                part_id = hit[0]
        if con.execute("SELECT 1 FROM parts WHERE part_id=?", (part_id,)).fetchone() is None:
            print("  WARNING: object %r not found as a part in %s" % (obj, path))
            missing.append((e["ref"], obj, path))
            continue
        con.execute("INSERT INTO product_parts VALUES (?,?,?,?,?,?,?)",
                    (pid, e["ref"], part_id, e.get("qty", 1), e.get("parent"),
                     json.dumps(e["placement"]) if e.get("placement") else None, e.get("notes")))
    con.commit()
    if progress:
        progress("fits")
    n = detect_interfaces(con, pid)
    n_parts = len(m["parts"]) - len(missing)
    print("product %s: %d parts, %d interfaces" % (pid, len(m["parts"]), n))
    if progress:
        progress("mirror")
    con.close()
    return n_parts, n, missing





def remove(product_id, own_folder, db=None):
    """Erase a product from the catalogue: its BOM, interfaces and product row, plus the
    documents (and, by cascade, parts/features/specs) whose file lives in `own_folder` and
    that no other product uses. Documents elsewhere are never touched.
    -> {"products": n, "documents": n, "parts": n}."""
    con = ingest.connect(db=db)
    con.mirror_label = ("after-remove", product_id)       # closing con mirrors the erase
    try:
        folder = ingest.canon_path(own_folder).rstrip("\\/") + os.sep
        docs = [r[0] for r in con.execute("""SELECT DISTINCT pa.doc_id FROM product_parts pp
                    JOIN parts pa ON pa.part_id=pp.part_id WHERE pp.product_id=?""", (product_id,))]
        docs += [r[0] for r in con.execute("SELECT doc_id, path FROM documents").fetchall()
                 if r[0] not in docs and ingest.canon_path(r[1]).startswith(folder)]
        con.execute("DELETE FROM interfaces WHERE product_id=?", (product_id,))
        con.execute("DELETE FROM product_parts WHERE product_id=?", (product_id,))
        # (there is no drawings table: drawings are files made from the model)
        n_prod = con.execute("DELETE FROM products WHERE product_id=?", (product_id,)).rowcount
        n_docs = n_parts = 0
        for d in docs:
            row = con.execute("SELECT path FROM documents WHERE doc_id=?", (d,)).fetchone()
            if not row or (not ingest.canon_path(row[0]).startswith(folder) and os.path.exists(paths.local(row[0]))):
                continue             # a file that lives elsewhere (and still exists there) is never touched
            if con.execute("""SELECT 1 FROM product_parts pp JOIN parts pa ON pa.part_id=pp.part_id
                              WHERE pa.doc_id=? LIMIT 1""", (d,)).fetchone():
                continue                                   # still used by another product
            n_parts += con.execute("SELECT COUNT(*) FROM parts WHERE doc_id=?", (d,)).fetchone()[0]
            con.execute("DELETE FROM documents WHERE doc_id=?", (d,))   # cascades to parts/features/specs
            n_docs += 1
        con.commit()
        result = {"products": n_prod, "documents": n_docs, "parts": n_parts}
    finally:
        con.close()          # mirrors the erase into Postgres (ingest.CatalogueConnection)
    return result


def show(product_id):
    con = ingest.connect(readonly=True)      # a reader: no writer connection, no schema re-applied, nothing mirrored
    con.row_factory = sqlite3.Row
    p = con.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()
    if not p:
        print("no such product"); return 1
    print("PRODUCT %s  rev %s\n  %s" % (p["name"], p["revision"], p["description"] or ""))
    print("\nBILL OF MATERIALS")
    print("  %-10s %-4s %-22s %-16s %10s  %s" % ("REF", "QTY", "LABEL", "FAMILY", "VOL mm3", "SOURCE"))
    for r in con.execute("""SELECT pp.ref_des, pp.qty, pp.parent_ref, pa.label, pa.family, pa.volume_mm3, d.path
                            FROM product_parts pp JOIN parts pa ON pa.part_id=pp.part_id
                            JOIN documents d ON d.doc_id=pa.doc_id
                            WHERE pp.product_id=? ORDER BY pp.parent_ref, pp.ref_des""", (product_id,)):
        ind = "  " if r["parent_ref"] else ""
        print("  %s%-10s %-4d %-22s %-16s %10.0f  %s" % (ind, r["ref_des"], r["qty"], (r["label"] or "")[:22],
              (r["family"] or "")[:16], r["volume_mm3"] or 0, os.path.basename(r["path"])))
    print("\nINTERFACES")
    for r in con.execute("""SELECT * FROM interfaces WHERE product_id=? ORDER BY kind, nominal_a_mm DESC""",
                         (product_id,)):
        if r["kind"] == "cylindrical_fit":
            print("  fit      %-10s OD %-7.3f in %-10s bore %-7.3f  clearance %+.3f  %s  at (%.1f, %.1f, %.1f)"
                  % (r["ref_a"], r["nominal_a_mm"], r["ref_b"], r["nominal_b_mm"], r["clearance_mm"],
                     r["fit_class"], r["pos_x"] or 0, r["pos_y"] or 0, r["pos_z"] or 0))
        else:
            print("  contact  %-10s face <-> %-10s  gap %.3f  overlap ~%.0f mm2  normal (%.2f, %.2f, %.2f)"
                  % (r["ref_a"], r["ref_b"], r["clearance_mm"], r["contact_area_mm2"] or 0,
                     r["axis_x"] or 0, r["axis_y"] or 0, r["axis_z"] or 0))
    con.close()
    return 0


def _cliclient():
    """framework.common.cliclient (stdlib only at import), or None."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from framework.common import cliclient
        return cliclient
    except Exception:
        return None


def _load_through_services(manifest_arg):
    """`product.py load` as a client (BC_SERVICES=1): submit the `product.load` recipe, wait, print the line `load`
    prints. The manifest must be the one in framework/products (the services read nothing else)."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        with open(manifest_arg, encoding="utf-8") as fh:
            pid = json.load(fh)["product_id"]
    except (OSError, ValueError, KeyError):
        print("That design file could not be read.")
        return 2
    wanted = os.path.join(root, "framework", "products", pid + ".json")
    if os.path.normcase(os.path.abspath(manifest_arg)) != os.path.normcase(wanted):
        print("The design file must be in the products folder.")
        return 2
    if root not in sys.path:
        sys.path.insert(0, root)
    from framework.common import jobcall
    from framework.common import policy as _policy                  # the shared job client (not the studio's)
    try:
        res = jobcall.call("product.load", {"manifest": "framework/products/%s.json" % pid, "product_id": pid},
                           sender="cli", timeout_s=_policy.wait_s("product.load"))
    except jobcall.JobFailed as exc:
        print("The design could not be loaded: %s" % (exc.user_text or exc.code))
        return 1
    except (jobcall.JobError, TimeoutError):
        print("The design could not be loaded right now.")
        return 3
    print("product %s: %d parts, %d interfaces" % (pid, res.get("parts", 0) + len(res.get("missing") or []), res.get("interfaces", 0)))
    return 0


def main(argv):
    cc = _cliclient()
    if cc is not None:
        cc.watch_direct("product.py")
    args, db = [], None
    i = 0
    while i < len(argv):
        if argv[i] == "--db":
            i += 1
            db = argv[i]
        else:
            args.append(argv[i])
        i += 1
    if not args or args[0] in ("-h", "--help"):
        print(__doc__); return 0
    cmd = args[0]
    if cmd in ("list", "show") and db is None and cc is not None and cc.wanted():
        # with BC_SERVICES=1 the question goes to the catalogue service; so does `load`, below
        return cc.relay("catalogue.list" if cmd == "list" else "catalogue.show", args,
                        script=os.path.abspath(__file__))
    if cmd == "load":
        if db is None and cc is not None and cc.wanted():
            return _load_through_services(args[1])      # the load is the `product.load` recipe
        load(args[1], db=db)
        return 0
    if cmd == "list":
        con = ingest.connect(readonly=True, db=db)      # a reader (no writer, no schema re-applied)
        for r in con.execute("""SELECT p.product_id, p.name, p.revision,
                                (SELECT COUNT(*) FROM product_parts pp WHERE pp.product_id=p.product_id),
                                (SELECT COUNT(*) FROM interfaces i WHERE i.product_id=p.product_id)
                                FROM products p"""):
            print("  %-16s %-40s rev %-3s %2d parts %2d interfaces" % r)
        con.close()
        return 0
    if cmd == "show":
        return show(args[1])
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
