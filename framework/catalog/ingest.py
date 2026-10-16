"""Ingest STEP documents into the part catalogue, in-process.

Calls framework.kernel (io.load + measure.measure) directly -- no child process,
no env-var argv. A STEP metadata sidecar (labels.json beside the STEP file) supplies
each object's Label/TypeId/material, which STEP itself does not carry.

    python ingest.py <file.step | folder> [more...]
    python ingest.py --rebuild <folder>      # drop and re-ingest everything
    python ingest.py --db <path> <file.step | folder> [more...]   # ingest into a
                                                                    # different DB file

Importable: ingest_file(path) returns doc_id, used by product.py.

Measuring: this module imports neither the kernel nor the engines layer at module level.
`ingest_file(path, con, rules, side, measure)` stores whatever `measure(path, side)` returns; the catalogue
service passes a function that submits `engines.measure` / `geometry.measure` jobs (framework/catalog/
svc_handlers.py, `catalogue.load`), so the first engine and the second engine run in their own services
and are compared by the `product.load` recipe. Without `measure`, a document is measured IN-PROCESS with
the kernel only (the parity tests, offline use); the first engine is reachable only through the services.
A native design file (.FCStd) is measured in-process only from an existing BREP dump.
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


from framework.common.families import SURF_COLS, classify, load_rules  # noqa: E402,F401  (R2)
from framework.common.files import sha256  # noqa: E402,F401  (R2)


def extract(path):
    """framework.kernel.extract.extract (R3), imported on use: the in-process kernel path (tests, offline)."""
    from framework.kernel.extract import extract as _extract
    return _extract(path)


def _load_sidecar(path):
    from framework.kernel.extract import _load_sidecar as _ls
    return _ls(path)


HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.abspath(os.environ.get("BC_CATALOG_DB") or os.path.join(HERE, "catalog.db"))      # BC_CATALOG_DB (container), default the file beside this module
SCHEMA = os.path.join(HERE, "schema.sql")
FAMILIES = os.path.join(HERE, "families.json")





def doc_id_for(path, checksum):
    """A document's id: its checksum[:12]. A catalogue part whose labels.json sidecar gives the file a chosen material
    (add_step.catalogue_material) is measured with that material, so the same file catalogued in two materials is two
    documents: the id is then sha256(checksum + material)[:12]. Plain JSON read here (no kernel import)."""
    material = None
    sidecar = os.path.join(os.path.dirname(os.path.abspath(path)), "labels.json")
    if os.path.exists(sidecar):
        try:
            with open(sidecar, encoding="utf-8") as fh:
                material = (json.load(fh).get(os.path.basename(path)) or {}).get("material")
        except (OSError, ValueError, AttributeError):
            material = None
    if not material or not isinstance(material, str):
        return checksum[:12]
    return hashlib.sha256(("%s\nmaterial=%s" % (checksum, material)).encode("utf-8")).hexdigest()[:12]


def canon_path(path):
    """The one form a document path is stored and compared in: absolute, links and
    8.3 short names resolved, and on Windows lower-case with backslashes (os.path.normcase),
    so `C:\\data\\a.STEP`, `c:/data/a.step` and `..\\data\\a.step` (from C:\\work) are the same file."""
    p = os.path.abspath(path)
    try:
        p = os.path.realpath(p)
    except (OSError, ValueError):
        pass
    return os.path.normcase(p)


def find_documents(con, path):
    """Documents catalogued for the file `path` under any spelling of its path (letter case,
    slashes, `..`, links). -> [(doc_id, checksum_sha256, stored_path)]."""
    key = canon_path(path)
    base = os.path.basename(key)
    return [(doc_id, sha, stored) for doc_id, stored, sha in
            con.execute("SELECT doc_id, path, checksum_sha256 FROM documents").fetchall()
            if stored and os.path.basename(os.path.normcase(stored)) == base and canon_path(stored) == key]


def drop_stale_documents(con, path, keep):
    """Delete the other documents of the same file (any spelling of `path`) than `keep`, with
    their parts/features/specs (cascade). A document something still uses (a product's bill of
    materials or interfaces) fails its foreign key and is kept: SQLite backs out just
    that statement. -> doc_ids deleted. The caller commits."""
    gone = []
    for doc_id, _sha, _stored in find_documents(con, path):
        if doc_id == keep:
            continue
        try:
            con.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
            gone.append(doc_id)
        except sqlite3.IntegrityError:
            pass
    return gone


def document_in_use(con, doc_id, checksum):
    """True when document `doc_id` is stored with this very checksum and something stands on it: a design's bill of
    materials or an interface (the rows whose foreign keys forbid deleting it). Such a document is the
    same content under any path, so a second design of the same file reuses it and a re-ingest leaves it alone. (There is no
    drawings table: drawings are files made from the model.)"""
    row = con.execute("SELECT checksum_sha256 FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    if row is None or row[0] != checksum:
        return False
    return any(con.execute(q, (doc_id,)).fetchone() for q in (
        "SELECT 1 FROM product_parts pp JOIN parts pa ON pa.part_id=pp.part_id WHERE pa.doc_id=? LIMIT 1",
        "SELECT 1 FROM interfaces i JOIN features f ON f.feature_id IN (i.feature_a, i.feature_b) "
        "JOIN parts pa ON pa.part_id=f.part_id WHERE pa.doc_id=? LIMIT 1"))


class CatalogueConnection(sqlite3.Connection):
    """A writer connection to a catalogue file: the ONE choke point of the Postgres mirror.
    close() mirrors the change when this connection changed rows (total_changes > 0);
    framework/catalog/pg/sync.py on_write() decides whether the file is the default catalogue
    (test and kernel-shadow catalogues are skipped there). `mirror_label`, set by the caller,
    names the change for the log: ("after-product", pid) | ("after-remove", pid) |
    ("after-ingest", [doc_ids]); None = a generic after-write. The mirror never raises here."""
    db_path = None
    mirror_label = None
    mirror_extra = None      # {"d2_provider": fn} -- where the shape signatures of what this connection wrote come from

    def close(self):
        try:
            changed = self.total_changes > 0
        except sqlite3.Error:            # already closed
            changed = False
        super().close()
        if changed:
            try:
                from framework.catalog.pg import sync
                sync.on_write(self.db_path, self.mirror_label,
                              d2_provider=(self.mirror_extra or {}).get("d2_provider"))
            except Exception:
                pass


def connect(readonly=False, db=None):
    """Writers get the schema applied and WAL mode. Readers (the web app) open
    read-only and never run DDL, so an ingest in progress cannot make
    a page request wait on the write lock. `db` defaults to the module global DB,
    read at call time (so --db / a test fixture can repoint it before calling).
    A writer is a CatalogueConnection: closing it mirrors what it wrote into Postgres."""
    db = db or DB
    if readonly:
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True, timeout=2)
        con.execute("PRAGMA query_only=ON")
        return con
    con = sqlite3.connect(db, timeout=30, factory=CatalogueConnection)
    con.db_path = os.path.abspath(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")      # readers never block on a writer
    with open(SCHEMA, encoding="utf-8") as fh:
        con.executescript(fh.read())
    retire_tables(con)
    return con


RETIRED_TABLES = ("dimensions", "tolerances", "drawings", "part_cards")      # never filled, no code uses them


def retire_tables(con):
    """Drop the retired tables, each only when it exists and holds no rows; a table with rows is left alone (and said so
    once on stderr), never dropped."""
    for t in RETIRED_TABLES:
        try:
            if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (t,)).fetchone() is None:
                continue
            if con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] == 0:
                con.execute("DROP TABLE %s" % t)
            else:
                sys.stderr.write("catalogue: retired table %s still holds rows; not dropped\n" % t)
        except sqlite3.Error:
            pass










def store(con, data, rules):
    """Stores `data["parts"]` (framework.kernel.measure.measure()'s own records) into
    the `parts`/`features`/`specs` tables. `volume_mm3`/`area_mm2` land in their
    existing dedicated columns unchanged -- whichever the measurement rule's method
    decided ("formula" or "mesh"), same as any other measured number. The method flag and its
    uncertainty (`volume_method`/`volume_uncertainty_mm3`/`area_method`/
    `area_uncertainty_mm2`) have no dedicated column; they are kept in `raw_json`
    (`json.dumps(rec)` below already carries every key `measure()` produces, these
    included); the catalogue's fact tables are the place to promote them to typed,
    queryable provenance columns.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    src = canon_path(data["source_file"])
    doc_id = doc_id_for(data["source_file"], data["checksum_sha256"])
    # the writer lock first, then the look at what the file already is. A document is the file's CONTENT
    # (doc_id = its checksum): the same file stored under a second path (a design uploaded again) or by a second writer at the
    # same time is the same document. Two writers wait for each other here (the connection's 30 s) instead of racing between
    # the look and the delete; inside a load's own transaction the lock is already held.
    began = not con.in_transaction
    if began:
        con.execute("BEGIN IMMEDIATE")
    if document_in_use(con, doc_id, data["checksum_sha256"]):
        if began:
            con.commit()                        # ends only a transaction this call began, never the caller's own
        return doc_id, data["part_count"]       # identical content that a design stands on: kept as it is, nothing to replace
    # a re-ingest of the same file replaces its document, whatever the spelling of the stored path;
    # one statement, so a foreign-key failure backs all of it out
    old = sorted({doc_id} | {d for d, _sha, _p in find_documents(con, src)})
    con.execute("DELETE FROM documents WHERE doc_id IN (%s)" % ",".join("?" * len(old)), old)   # cascades
    con.execute("""INSERT INTO documents
        (doc_id, path, doc_name, format, checksum_sha256, object_count, part_count, tool_version, ingested_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (doc_id, src, data.get("source_doc"), os.path.splitext(src)[1].lstrip(".").lower(),
         data.get("checksum_sha256"), data.get("object_count"), data.get("part_count"),
         data.get("kernel_version") or "FreeCAD %s" % data.get("freecad_version"), now))

    for rec in data["parts"]:
        family, why = classify(rec, rules)
        pid = "%s:%s" % (doc_id, rec["object"])
        dims = (rec.get("bbox_sorted_mm") or []) + [None] * 3
        bb = (rec.get("bbox") or []) + [None] * 6
        com = (rec.get("com") or []) + [None] * 3
        mom = (rec.get("moments") or []) + [None] * 3
        rg = (rec.get("radius_of_gyration") or []) + [None] * 3
        surf = rec.get("surfaces") or {}
        ratios = rec.get("shape_ratios") or {}
        con.execute("""INSERT INTO parts (
            part_id, doc_id, object_name, label, type_id, family, family_why, material, units,
            volume_mm3, area_mm2, bbox_a_mm, bbox_b_mm, bbox_c_mm,
            bbox_xmin, bbox_xmax, bbox_ymin, bbox_ymax, bbox_zmin, bbox_zmax,
            com_x_mm, com_y_mm, com_z_mm, moment_1, moment_2, moment_3, rgyr_1, rgyr_2, rgyr_3,
            n_solids, n_shells, n_faces, n_edges, n_vertexes,
            n_plane, n_cylinder, n_cone, n_sphere, n_torus, n_bspline, n_other_surf,
            aspect_1, aspect_2, fill, compactness, added, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?,?,?, ?,?,?,?,?,?)""",
            (pid, doc_id, rec["object"], rec.get("label"), rec.get("type_id"), family, why,
             rec.get("material"), rec.get("units"),
             rec.get("volume_mm3"), rec.get("area_mm2"), dims[0], dims[1], dims[2],
             bb[0], bb[1], bb[2], bb[3], bb[4], bb[5],
             com[0], com[1], com[2], mom[0], mom[1], mom[2], rg[0], rg[1], rg[2],
             rec.get("n_solids"), rec.get("n_shells"), rec.get("n_faces"), rec.get("n_edges"), rec.get("n_vertexes"),
             surf.get("Plane", 0), surf.get("Cylinder", 0), surf.get("Cone", 0),
             surf.get("Sphere", 0), surf.get("Toroid", 0), surf.get("BSplineSurface", 0),
             sum(v for k, v in surf.items() if k not in SURF_COLS),
             ratios.get("aspect_1"), ratios.get("aspect_2"), ratios.get("fill"), ratios.get("compactness"),
             now, json.dumps(rec)))
        for f in rec.get("features") or []:
            ax = (f.get("axis") or []) + [None] * 3
            pos = (f.get("pos") or []) + [None] * 3
            fb = (f.get("bbox") or []) + [None] * 6
            con.execute("""INSERT INTO features (part_id, kind, role, dia_mm, radius_mm, semi_angle_deg,
                axis_x, axis_y, axis_z, pos_x, pos_y, pos_z, offset_mm, extent_mm, area_mm2, face_count,
                bb_xmin, bb_xmax, bb_ymin, bb_ymax, bb_zmin, bb_zmax)
                VALUES (?,?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?, ?,?,?,?,?,?)""",
                (pid, f["kind"], f.get("role"), f.get("dia_mm"), f.get("radius_mm"), f.get("semi_angle_deg"),
                 ax[0], ax[1], ax[2], pos[0], pos[1], pos[2], f.get("offset_mm"), f.get("extent_mm"),
                 f.get("area_mm2"), f.get("face_count"), fb[0], fb[1], fb[2], fb[3], fb[4], fb[5]))
        for k, v in (rec.get("params") or {}).items():
            con.execute("INSERT OR REPLACE INTO specs VALUES (?,?,?,?,?,?)",
                        (pid, "param_" + k.lower(), v, None, "mm" if k != "Angle" else "deg", "measured"))
    con.commit()
    return doc_id, data["part_count"]


def ingest_file(path, con=None, rules=None, side=None, measure=None):
    """Measure one document and store it. `measure(path, side)` -> the extract payload (the catalogue service passes
    a function that runs the measuring as `engines.measure` / `geometry.measure` jobs; `side` None counts as "kernel").
    Without it, in-process and kernel-only: a STEP file through framework.kernel.extract, a native design file from
    its BREP dump when one exists; the first engine ("freecad") is reachable only through `measure`."""
    own = con is None
    con = con or connect()
    rules = rules or load_rules()
    if measure is not None:
        data = measure(path, side or "kernel")
    elif side in (None, "kernel"):
        if path.lower().endswith((".step", ".stp")):
            data = extract(path)
        else:                              # .FCStd: measured from the exact BREP dump made by the first engine
            from framework.kernel import brep_dump
            fresh = sha256(path)
            d = brep_dump.brep_dir_for(path, fresh)
            if not os.path.exists(os.path.join(d, "_labels.json")):
                raise RuntimeError("This design file is measured through the services (no dump yet).")
            data = brep_dump.extract_dump(path, d, fresh)
    else:
        raise RuntimeError("The first engine measures through the services.")
    doc_id, n = store(con, data, rules)
    if own:
        con.close()
    return doc_id, n


def _fcstd_ok():
    """In-process measuring is the kernel's, which reads a native design file only from a BREP dump: the walk
    leaves native files to the services."""
    return False


def walk(targets):
    fc_ok = _fcstd_ok()
    for t in targets:
        if os.path.isdir(t):
            for root, _, files in os.walk(t):
                for f in files:
                    lower = f.lower()
                    path = os.path.join(root, f)
                    if lower.endswith(".step") or lower.endswith(".stp"):
                        yield path
                    elif lower.endswith(".fcstd") and fc_ok:
                        yield path
                    elif lower.endswith(".fcstd"):
                        print("skipped %s: native design files are not read here; export the "
                              "design as STEP from its CAD application and ingest that file" % path)
        elif t.lower().endswith((".step", ".stp")):
            yield t
        elif t.lower().endswith(".fcstd") and fc_ok:
            yield t
        elif t.lower().endswith(".fcstd"):
            print("skipped %s: native design files are not read here; export the "
                  "design as STEP from its CAD application and ingest that file" % t)


def main(argv):
    global DB
    args, rebuild = [], False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--rebuild":
            rebuild = True
        elif a == "--db":
            i += 1
            DB = argv[i]
        else:
            args.append(a)
        i += 1
    if rebuild and os.path.exists(DB):
        os.remove(DB)
    if not args:
        print(__doc__)
        return 2
    rules = load_rules()
    con = connect()
    total = 0
    doc_ids = []
    con.mirror_label = ("after-ingest", doc_ids)     # closing con mirrors these into Postgres
    for path in walk(args):
        try:
            doc_id, n = ingest_file(path, con, rules)
        except Exception as e:
            print("  FAIL %s\n       %s" % (path, e))
            continue
        doc_ids.append(doc_id)
        total += n
        print("  %-3d parts  %s" % (n, path))
    print("catalogued %d parts into %s" % (total, DB))
    con.close()            # mirrors the documents written into Postgres (CatalogueConnection)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
