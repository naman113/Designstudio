"""Shape signatures, DB-facing half: resolves a part's shape/
record and writes `framework.kernel.signature`'s three descriptors into the `signature`
table (`d2_v1`, `moments_v1`) and `part_revision.geom_hash` (`inv_v1`).

`resolve_shape(con, part_id)` covers both catalogue shapes this project has:
- A STEP-backed document (`document.format` in step/stp, `document.path` readable): loads
  it through `framework.kernel.io.load` and returns the occurrence matching `object_ref`.
- A non-STEP document (e.g. FreeCAD `.fcstd` documents, whose own path may not exist on
  THIS machine when catalog.db was copied from another one): falls back to the product
  manifest that occurrence carries the part under (`framework/products/<product_id>.json`,
  this checkout's own copy, NOT the possibly-stale `product.manifest_path` column) -- its
  `parts[].file`/`.object` entries are the SAME kernel STEP path/object name
  `framework.kernel.io` can load (manifest entries carry file/object (kernel STEP) next to
  fc_file/fc_object).

- A native design document (`.fcstd`) -- the only source for an uploaded design or a part
  in no product -- has the exact shape of each part dumped by the first engine's measure pass:
  `work/engines/brep/<doc sha256[:12]>/<object_ref>.brep` (the dump the second engine measures).
  It is the LAST candidate, so a part that already had a STEP candidate keeps its signature.

A part with no candidate that loads has NO reachable geometry -- `backfill` counts it
(`without_geometry`) rather than silently skipping it. `moments_v1` needs only the
stored record, so every part gets it, with or without geometry.

    python -m framework.catalog.pg.signatures [--db NAME] [--all]

The command is idempotent (only missing rows, unless --all), holds the mirror's advisory lock
while it writes, and names every part still without a shape signature, with the reason.
"""
import argparse
import json
import os
import time

from psycopg.rows import dict_row

from framework.catalog.pg import db as pgdb
from framework.common import brep_paths, paths
from framework.common import shape_signature as ksig   # geom_hash, moments_vec; the kernel is imported inside, where a shape is needed

# framework/catalog/pg/signatures.py -> pg -> catalog -> framework -> root
_PG_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_PG_DIR)))
PRODUCTS_DIR = os.path.join(PROJECT_ROOT, "framework", "products")

_SURFACE_COLS = (
    ("Plane", "n_plane"), ("Cylinder", "n_cylinder"), ("Cone", "n_cone"),
    ("Sphere", "n_sphere"), ("Toroid", "n_torus"), ("BSplineSurface", "n_bspline"),
    ("Other", "n_other_surf"),
)


# ---------------------------------------------------------------------------
# shape resolution
# ---------------------------------------------------------------------------

def _manifest_entry(product_id, ref_des):
    path = os.path.join(PRODUCTS_DIR, "%s.json" % product_id)
    if not os.path.isfile(path):
        return None, None
    with open(path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    for part in manifest.get("parts") or []:
        if part.get("ref") == ref_des:
            return path, part
    return None, None


def _candidates(con, part_id):
    """{"document": (path, object) | None, "manifest": (path, object) | None, "dump": (path, object) | None}: the three
    places a part's exact shape can come from (see `d2_targets`, which lists them in its own order;
    `shape_sources` lists them fastest first). Only existing files are named."""
    found = {"document": None, "manifest": None, "dump": None}
    row = con.execute(
        "SELECT doc_id, object_ref FROM part_revision WHERE part_id=%s", (part_id,)
    ).fetchone()
    if row is None:
        return found
    doc_id, object_ref = row
    if doc_id is not None:
        doc = con.execute(
            "SELECT path, format, sha256 FROM document WHERE doc_id=%s", (doc_id,)
        ).fetchone()
        if doc is not None:
            path, fmt, sha = doc
            path = paths.local(path)          # the stored host form (drive letter, lower case) opens here
            if (fmt or "").lower() in ("step", "stp") and path and os.path.isfile(path):
                found["document"] = (path, object_ref)
            elif (fmt or "").lower() not in ("step", "stp"):
                bp = brep_paths.dump_file(sha or doc_id, object_ref)
                if bp is not None and os.path.isfile(bp):
                    found["dump"] = (bp, object_ref)

    # Manifest fallback: any occurrence of this part carries the
    # ref_des whose manifest entry names the kernel STEP file/object for THIS checkout.
    occ = con.execute(
        "SELECT product_id, ref_des FROM occurrence WHERE part_id=%s LIMIT 1", (part_id,)
    ).fetchone()
    if occ is not None:
        product_id, ref_des = occ
        manifest_path, entry = _manifest_entry(product_id, ref_des)
        if entry is not None:
            step_rel, obj_name = entry.get("file"), entry.get("object")
            # Not every manifest entry has a kernel STEP counterpart yet -- some parts (e.g. a
            # handful of uploaded/imported products) only carry `fc_file`/`fc_object` (FreeCAD),
            # and `file` is itself a `.FCStd` path there, unreadable by framework.kernel.io. That
            # part has no reachable geometry for THIS task, not a bug -- counted, not skipped.
            if step_rel and obj_name and os.path.splitext(step_rel)[1].lower() in (".step", ".stp"):
                found["manifest"] = (os.path.normpath(os.path.join(os.path.dirname(manifest_path), step_rel)), obj_name)
    return found


def d2_targets(con, part_ids):
    """-> [(part_id, [(path, object), ...]), ...]: the catalogue half of `resolve_shape` for each part, in the given
    order. Each part's candidates in the order `resolve_shape` tries them: first its document (only a
    STEP document whose file exists, with the part's `object_ref`), then the first occurrence's manifest entry
    (`file` / `object`, only when `file` is a STEP; the path is relative to this checkout's own manifest), then
    the part's exact-shape dump when its document is a native design file (`<brep cache>/<doc sha[:12]>/<object_ref>.brep`,
    only when that file exists). No kernel here: whoever has the kernel (this module's `resolve_shape`, or the geometry
    service's `geometry.signature`) loads the candidates in order and the first shape found wins. A part with no
    candidate has an empty list."""
    out = []
    for part_id in part_ids:
        c = _candidates(con, part_id)
        # the dump is last, so a part with a STEP candidate keeps its signature
        out.append((part_id, [x for x in (c["document"], c["manifest"], c["dump"]) if x is not None]))
    return out


def shape_sources(con, part_id):
    """The candidates for one part's own view and download, fastest first: its exact-shape dump (one
    small file per part), then its design's manifest entry, then its STEP document. The document goes last because its
    object names are the first engine's ("Part__Feature"), which the kernel's own reading of the file never carries
    (measured: 0 of the 78 parts of the 4 STEP documents found that way, each a whole-file load wasted, 9.4 s for
    the largest), while the manifest entry names the kernel's object. -> [(path, object), ...]; empty when none."""
    c = _candidates(con, part_id)
    return [x for x in (c["dump"], c["manifest"], c["document"]) if x is not None]


def resolve_shape(con, part_id, cache=None):
    """-> a TopoDS shape for `part_id`, or None when no geometry is reachable. `cache`
    (optional, caller-supplied dict) makes repeated calls across parts sharing one STEP
    file (or one product manifest) load that file's Model at most once -- pass the SAME
    dict across a whole `backfill()` run. Built on `d2_targets`: the candidates in
    order, the first shape found wins (`framework.kernel.brep_dump.candidate_shape`, the
    geometry service's own rule)."""
    from framework.kernel import brep_dump      # the offline path only (CLI, tests, migration)
    if cache is None:
        cache = {}
    for _pid, cands in d2_targets(con, [part_id]):
        for path, ref in cands:
            shape = brep_dump.candidate_shape(path, ref, cache)
            if shape is not None:
                return shape
    return None


# ---------------------------------------------------------------------------
# record reconstruction from compat.parts/compat.features (the exact-equal read
# models) -- the same source query.py's own ranking reads, so geom_hash/moments_vec see
# the SAME numbers already displayed/ranked elsewhere, not a re-derivation from raw facts.
# ---------------------------------------------------------------------------

def _part_record(con, part_id):
    cur = con.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM compat.parts WHERE part_id=%s", (part_id,))
    row = cur.fetchone()
    if row is None:
        return None
    moments = [row["moment_1"], row["moment_2"], row["moment_3"]]
    rgyr = [row["rgyr_1"], row["rgyr_2"], row["rgyr_3"]]
    # compat.features' own dia_mm expression, read straight from `feature`: the view numbers every feature of the
    # catalogue before it filters (~0.15 s per part on 48,860 features); geom_hash sorts the features anyway
    cur.execute("SELECT ft.kind, ft.role, (SELECT value_num FROM fact WHERE entity_kind='feature' AND entity_id=ft.feature_id "
                "AND property_id='feat.diameter' AND valid_to IS NULL) AS dia_mm FROM feature ft WHERE ft.part_id=%s "
                "ORDER BY ft.ordinal", (part_id,))
    features = [{"kind": f["kind"], "role": f["role"], "dia_mm": f["dia_mm"]} for f in cur.fetchall()]
    return {
        "volume_mm3": row["volume_mm3"], "area_mm2": row["area_mm2"],
        "moments": moments if all(v is not None for v in moments) else None,
        "radius_of_gyration": rgyr if all(v is not None for v in rgyr) else None,
        "n_solids": row["n_solids"], "n_shells": row["n_shells"], "n_faces": row["n_faces"],
        "n_edges": row["n_edges"], "n_vertexes": row["n_vertexes"],
        "surfaces": {name: (row[col] or 0) for name, col in _SURFACE_COLS},
        "shape_ratios": {
            "aspect_1": row["aspect_1"], "aspect_2": row["aspect_2"],
            "fill": row["fill"], "compactness": row["compactness"],
        },
        "features": features,
    }


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def _vector_literal(vec):
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _write_signature(con, part_id, method, vec):
    con.execute(
        "INSERT INTO signature (part_id, method, vec) VALUES (%s, %s, %s::vector) "
        "ON CONFLICT (part_id, method) DO UPDATE SET vec = EXCLUDED.vec",
        (part_id, method, _vector_literal(vec)),
    )


def _write_backfill(con, part_id, record, d2):
    gh = ksig.geom_hash(record)
    mv = ksig.moments_vec(record)
    con.execute("UPDATE part_revision SET geom_hash=%s WHERE part_id=%s", (gh, part_id))
    _write_signature(con, part_id, "moments_v1", mv)
    _write_signature(con, part_id, "d2_v1", d2)


def _write_moments(con, part_id, record):
    """geom_hash + moments_v1 from the stored record alone (no shape): every part gets them."""
    con.execute("UPDATE part_revision SET geom_hash=%s WHERE part_id=%s", (ksig.geom_hash(record), part_id))
    _write_signature(con, part_id, "moments_v1", ksig.moments_vec(record))


def _missing(con, method):
    return {r[0] for r in con.execute(
        "SELECT part_id FROM part_revision pr WHERE NOT EXISTS "
        "(SELECT 1 FROM signature s WHERE s.part_id = pr.part_id AND s.method = %s)", (method,)).fetchall()}


def backfill(con, only_missing=True, part_ids=None, d2_provider=None, commit=True):
    """For every part_revision (or, when `only_missing` -- the default -- only those with
    no `d2_v1` signature row yet: `framework.catalog.pg.load.write_part` already sets
    `geom_hash` at write time unconditionally -- so `geom_hash IS NULL`
    is the WRONG gate here; `d2_v1` is the one signature that needs a resolved shape, so its
    absence is what actually means "not backfilled yet"; `part_ids` limits the pass to
    those parts, e.g. the ones one write just added): resolve its shape; if reachable,
    (re)compute geom_hash + moments_v1 (from the record) + d2_v1 (from the shape) and write
    all three. A part with NO reachable geometry is counted in `without_geometry`, never
    silently skipped.

    `moments_v1` needs only the stored record, so a part without `moments_v1` (in scope) gets it
    and its record's geom_hash whether or not its shape is reachable (`moments_written`; `without_record` lists a part
    with no read-model record, which cannot get either).
    -> {"parts": int, "with_geometry": int, "without_geometry": [part_id, ...], "moments_written": int,
        "without_record": [part_id, ...]}.

    `d2_provider`: a function `provider(d2_targets(con, todo)) -> {part_id: vector or None}`. With it the
    shapes are never resolved here (no kernel): the provider is the services' measure results or one `geometry.signature`
    job; a part it returns None for has no reachable geometry. Without it, the in-process path (kernel, as before).

    `commit`: False leaves the commit to the caller (pg/sync.py writes a design's rows, their signatures without a
    shape and the read models in one transaction)."""
    every = [r[0] for r in con.execute("SELECT part_id FROM part_revision ORDER BY part_id").fetchall()]
    if only_missing:
        no_d2, no_moments = _missing(con, "d2_v1"), _missing(con, "moments_v1")
        todo = [p for p in every if p in no_d2]
        moments_todo = [p for p in every if p in no_moments]
    else:
        todo, moments_todo = list(every), list(every)
    if part_ids is not None:
        wanted = set(part_ids)
        todo = [p for p in todo if p in wanted]
        moments_todo = [p for p in moments_todo if p in wanted]
    refresh = []
    if part_ids is not None and only_missing:
        # a part a write just rewrote keeps its old signature rows (same part_id), but `write_part` stored a geom_hash
        # computed from the written record, not from the read-model record `backfill` uses; refresh geom_hash and moments_v1
        # for those parts (cheap, no shape) so a part always carries geom_hash(_part_record), however it was written.
        have = {r[0] for r in con.execute(
            "SELECT part_id FROM signature WHERE method = 'd2_v1' AND part_id = ANY(%s)", (list(part_ids),)).fetchall()}
        refresh = [p for p in part_ids if p in have and p not in set(todo)]
    part_ids = todo
    done = set()
    for part_id in refresh:
        record = _part_record(con, part_id)
        if record is not None:
            _write_moments(con, part_id, record)
            done.add(part_id)

    with_geometry = 0
    without_geometry = []
    if d2_provider is not None:
        d2s = d2_provider(d2_targets(con, part_ids)) if part_ids else {}
        for part_id in part_ids:
            d2 = d2s.get(part_id)
            record = _part_record(con, part_id) if d2 is not None else None
            if d2 is None or record is None:
                without_geometry.append(part_id)
                continue
            _write_backfill(con, part_id, record, d2)
            done.add(part_id)
            with_geometry += 1
    else:
        cache = {}
        for part_id in part_ids:
            shape = resolve_shape(con, part_id, cache=cache)
            record = _part_record(con, part_id) if shape is not None else None
            if shape is None or record is None:
                without_geometry.append(part_id)
                continue
            from framework.kernel.signature import d2_histogram     # the offline path only (CLI, tests, migration)
            _write_backfill(con, part_id, record, d2_histogram(shape))
            done.add(part_id)
            with_geometry += 1

    moments_written, without_record = 0, []
    for part_id in moments_todo:
        if part_id in done:
            continue
        record = _part_record(con, part_id)
        if record is None:
            without_record.append(part_id)
            continue
        _write_moments(con, part_id, record)
        moments_written += 1

    if commit and not con.autocommit:
        con.commit()
    return {"parts": len(part_ids), "with_geometry": with_geometry, "without_geometry": without_geometry,
            "moments_written": moments_written, "without_record": without_record}


# ---------------------------------------------------------------------------
# the command: fill what is missing, say what cannot be filled and why
# ---------------------------------------------------------------------------

def _dump_dir_has_labels(sha_or_id):
    return os.path.isfile(os.path.join(brep_paths.BREP_CACHE, (sha_or_id or "")[:12], "_labels.json"))


def why_no_shape(con, part_ids):
    """{part_id: reason} for parts that have no d2_v1 (plain words, for the coordinator's report)."""
    out = {}
    for part_id, cands in d2_targets(con, part_ids):
        row = con.execute(
            "SELECT d.path, d.format, d.sha256, pr.doc_id FROM part_revision pr "
            "LEFT JOIN document d ON d.doc_id = pr.doc_id WHERE pr.part_id = %s", (part_id,)).fetchone()
        path, fmt, sha, doc_id = row if row else (None, None, None, None)
        if cands:
            out[part_id] = "its shape could not be read (%d candidate%s)" % (len(cands), "" if len(cands) == 1 else "s")
        elif (fmt or "").lower() in ("step", "stp"):
            out[part_id] = "its design file is missing"
        elif _dump_dir_has_labels(sha or doc_id):
            out[part_id] = "no exact shape was kept for it when its design was measured (no solid)"
        elif path and os.path.isfile(path):
            out[part_id] = "its design has not been measured on this copy yet"
        else:
            out[part_id] = "its design file is missing and no exact shape was kept"
    return out


def _lock(con, wait_s=600):
    """The mirror's advisory lock (sync.py), so a write's mirror or a full resync never runs at the same time. Waits up
    to `wait_s`. -> True when held."""
    end = time.monotonic() + wait_s
    said = False
    while True:
        if con.execute("SELECT pg_try_advisory_lock(hashtext('buildcore_sync'))").fetchone()[0]:
            return True
        if time.monotonic() > end:
            return False
        if not said:
            print("waiting for a running catalogue sync to finish ...", flush=True)
            said = True
        time.sleep(2)


def counts(con):
    q = lambda sql: con.execute(sql).fetchone()[0]
    return {"parts": q("SELECT COUNT(*) FROM part_revision"),
            "d2_v1": q("SELECT COUNT(DISTINCT part_id) FROM signature WHERE method='d2_v1'"),
            "moments_v1": q("SELECT COUNT(DISTINCT part_id) FROM signature WHERE method='moments_v1'")}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="database to backfill (default: the database of BC_PG_DSN / .env.buildcore)")
    ap.add_argument("--all", action="store_true", help="recompute every part, not just missing ones")
    a = ap.parse_args(argv)

    t0 = time.perf_counter()
    name = a.db or pgdb.dbname()
    pgdb.refuse_main_db_in_worktree(name)
    con = pgdb.connect(dbname=a.db, autocommit=True)
    try:
        if con.execute("SELECT to_regclass('compat.parts')").fetchone()[0] is None:
            print("database %s has no catalogue read models yet: run `python -m framework.catalog.pg.sync --full` first"
                  % name)
            return 2
        before = counts(con)
        print("database %s: %d parts, %d with d2_v1, %d with moments_v1" % (
            name, before["parts"], before["d2_v1"], before["moments_v1"]), flush=True)
        if not _lock(con):
            print("a catalogue sync held the lock for 10 minutes; nothing written")
            return 3
        try:
            result = backfill(con, only_missing=not a.all)
        finally:
            con.execute("SELECT pg_advisory_unlock(hashtext('buildcore_sync'))")
        after = counts(con)
        missing = sorted(_missing(con, "d2_v1"))
        why = why_no_shape(con, missing) if missing else {}
        no_moments = sorted(_missing(con, "moments_v1"))
        labels = dict(con.execute("SELECT part_id, label FROM part_revision WHERE part_id = ANY(%s)",
                                  (missing + no_moments,)).fetchall()) if (missing or no_moments) else {}
    finally:
        con.close()
    print("signatures: %d parts tried, %d got d2_v1, %d without reachable geometry; moments_v1 written for %d more" % (
        result["parts"], result["with_geometry"], len(result["without_geometry"]), result["moments_written"]))
    print("now: %d parts, %d with d2_v1, %d with moments_v1; without d2_v1: %d; without moments_v1: %d" % (
        after["parts"], after["d2_v1"], after["moments_v1"], len(missing), len(no_moments)))
    for part_id in missing:
        print("  no shape signature: %s (%s): %s" % (part_id, labels.get(part_id) or "", why.get(part_id, "")))
    for part_id in no_moments:
        print("  no moments signature: %s (%s): no stored record" % (part_id, labels.get(part_id) or ""))
    print("elapsed: %.1f s" % (time.perf_counter() - t0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
