"""The catalogue service's trash jobs: `catalogue.trash`, `catalogue.restore`, `catalogue.erase`.

The trash list (work/portfolio_trash.json) has one writer: this service (framework/catalog/trash.py, one copy of the helpers).
`catalogue.erase` is the first step of the `product.erase` recipe (orchestrator); its second step, `engines.drop_derived`,
removes the files the engines service made.

Payloads  trash {"product_id"}   restore {"kind": "mechanical", "product_id"}   erase {"product_id"[, "_pause_after_rows_s"]}
Errors    bad_id, no_product, bad_kind, not_in_trash, reference_design

Erase order (a half-done erase finishes when its command is read again): the rows (SQLite, the
Postgres mirror, the shadow catalogue), then the uploaded files and the design's drawings, then the manifest, then the trash entry. Every step
tolerates being done already. The guard (`trash.product_removable`) needs the manifest, so the manifest goes last: a
redelivery still finds it and passes the guard. When the rows were already gone, `product.remove` changes nothing and so
does not mirror to Postgres; a run killed between the SQLite commit and the mirror would leave Postgres behind, so the
mirror is run explicitly in that case.
"""
import os
import shutil

from framework.catalog import trash
from framework.common import derived, results
from framework.common.consumer import ServiceError

UPLOADS = os.path.join(derived.ROOT, "models", "uploads")
NO_ROWS = {"products": 0, "documents": 0, "parts": 0}


def _pid(payload):
    pid = payload.get("product_id")
    if not isinstance(pid, str) or not derived.PID_RX.match(pid):
        raise ServiceError("bad_id", "This design name is not valid.")
    return pid


def _entry(pid):
    return next((e for e in trash.load() if e["kind"] == "mechanical" and e["id"] == pid), None)


def _has_row(pid):
    import ingest
    con = ingest.connect(readonly=True)
    try:
        return con.execute("SELECT 1 FROM products WHERE product_id = ?", (pid,)).fetchone() is not None
    finally:
        con.close()


def trash_(ctx, payload):
    pid = _pid(payload)
    name = trash.product_name(pid)
    if name is None:
        raise ServiceError("no_product", "This design is not in the catalogue.")
    # idempotent: a design already in the trash returns the same entry
    return results.pack(ctx.job_id, trash.add("mechanical", pid, name, trash.product_removable(pid)))


def restore(ctx, payload):
    pid = payload.get("product_id")
    kind = payload.get("kind")
    if kind != "mechanical":
        raise ServiceError("bad_kind", "This kind of item cannot be restored.")
    if not isinstance(pid, str) or not derived.PID_RX.match(pid):
        raise ServiceError("bad_id", "This design name is not valid.")
    if not trash.drop(kind, pid) and ctx.read_ct <= 1:
        raise ServiceError("not_in_trash", "This design is not in the trash.")
    # a redelivery (read_ct > 1) whose first read already dropped the entry returns the same answer
    return results.pack(ctx.job_id, {"ok": True, "kind": kind, "id": pid})


def _rmtree_upload(folder):
    """Remove the upload folder, only inside UPLOADS. Returns the number of files removed."""
    up = os.path.realpath(folder)
    if os.path.isdir(up) and os.path.dirname(up) == os.path.realpath(UPLOADS):
        n = sum(len(fs) for _, _, fs in os.walk(up))
        shutil.rmtree(up)
        return n
    return 0


DRAW_ROOTS = (os.path.join(derived.ROOT, "framework", "web", "static", "files", "drawings"),      # the published sets
              os.path.join(derived.ROOT, "work", "drawings"))                                       # the geometry jobs' hand-over folders


def _rmtree_drawings(pid):
    """Remove the design's drawings (published sets and hand-over folders), each only when its resolved path is a direct
    child of its root. Returns the number of files removed; a second call removes nothing."""
    n = 0
    for root in DRAW_ROOTS:
        d = os.path.realpath(os.path.join(root, pid))
        if os.path.isdir(d) and os.path.dirname(d) == os.path.realpath(root):
            n += sum(len(fs) for _, _, fs in os.walk(d))
            shutil.rmtree(d, ignore_errors=True)
    return n


def erase(ctx, payload):
    import product as catalog_product
    from framework.catalog.pg import sync as pgsync
    pid = _pid(payload)
    folder = os.path.join(UPLOADS, pid)
    manifest = trash.manifest(pid)
    remains = _has_row(pid) or manifest is not None or os.path.isdir(folder)
    if _entry(pid) is None:
        if not remains and ctx.read_ct > 1:           # a redelivery after the entry was dropped: it was all done
            return results.pack(ctx.job_id, {**NO_ROWS, "kernel": dict(NO_ROWS), "files": 0, "already": True})
        raise ServiceError("not_in_trash", "This design is not in the trash.")
    rows, files = dict(NO_ROWS, kernel=dict(NO_ROWS)), 0
    if remains:
        # no row is removed while a load of this design writes: catalogue.load and this job hold the same one-at-a-time
        # lock (policy.SINGLE "catalog.db"), so the second engine's load trails the upload and an erase waits for it
        if not trash.product_removable(pid):
            raise ServiceError("reference_design",
                               "This is a reference design: it can be hidden or restored, not erased.")
        ctx.progress("erase")
        rows = catalog_product.remove(pid, folder)
        if not any(rows.values()):
            pgsync.after_remove(product_id=pid)          # see the module note: the mirror of a half-done erase
        kdb = derived.kernel_db()
        if kdb and os.path.exists(kdb):
            try:
                rows["kernel"] = catalog_product.remove(pid, folder, db=kdb)
            except Exception as exc:                     # the shadow catalogue never blocks an erase
                rows["kernel"] = "skipped: %s" % exc
        if ctx.read_ct == 1 and payload.get("_pause_after_rows_s"):    # the crash check's window; the studio never sends it
            ctx.sleep(min(60.0, float(payload["_pause_after_rows_s"])))
        files += _rmtree_upload(folder)
        files += _rmtree_drawings(pid)                  # before the manifest, so a redelivered erase still finishes
        manifest = trash.manifest(pid)
        if manifest:
            os.remove(manifest)
            files += 1
    files += _rmtree_drawings(pid)                      # a half-done erase (rows already gone) still clears them
    trash.drop("mechanical", pid)
    rows["files"] = files
    print("catalogue erase %s: products %d, documents %d, parts %d, files %d (read %d)"
          % (pid, rows["products"], rows["documents"], rows["parts"], files, ctx.read_ct), flush=True)
    return results.pack(ctx.job_id, rows)


HANDLERS = {"catalogue.trash": trash_, "catalogue.restore": restore, "catalogue.erase": erase}
