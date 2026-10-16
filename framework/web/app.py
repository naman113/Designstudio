"""Demo web app: mechanical designs on top of the catalogue.

    python framework/web/app.py          then open http://127.0.0.1:8766
"""
import csv
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict

# Never open a console window for a child process. On Windows, if this server has
# no console of its own (started detached), every console program it starts (and
# their children) gets a NEW visible window. CREATE_NO_WINDOW gives them a hidden console instead, which
# their own children inherit. Patched once here so every subprocess call is covered.
if os.name == "nt":
    _popen_init = subprocess.Popen.__init__

    def _popen_no_window(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        _popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _popen_no_window

# A uv-managed venv interpreter (C:\venvs\buildcore) starts with PYTHONHOME pointing at uv's base
# Python 3.13. Children inherit it, so a child process's plain `python` of another version loads
# 3.13's standard library and dies ("SRE module mismatch"). This process no longer needs it once
# started; a child venv interpreter sets its own again.
if os.environ.get("UV_INTERNAL__PYTHONHOME"):
    for _k in ("PYTHONHOME", "UV_INTERNAL__PYTHONHOME"):
        os.environ.pop(_k, None)

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CATALOG = os.path.join(ROOT, "framework", "catalog")
PRODUCTS = os.path.join(ROOT, "framework", "products")
STATIC = os.path.join(HERE, "static")
MESHES = os.path.join(STATIC, "meshes")

sys.path.insert(0, CATALOG)   # nothing in the studio runs catalogue or engines code (page reads, uploads, 3D views and erases are jobs)
sys.path.insert(0, ROOT)
from framework.common import results  # noqa: E402   the erase recipe's step results
from framework.common import derived  # noqa: E402   derived-file paths and freshness, shared with the engines service
from framework.web import job_client  # noqa: E402   the studio's client for jobs it waits on
from framework.common import asker as _asker  # noqa: E402   page reads come from the catalogue service
from framework.common import policy as _policy  # noqa: E402   a page wait = the job row + 30 s
from framework.common import slog  # noqa: E402   the structured log (one line per step of a job)


app = FastAPI(title="CAD design demo")


@app.middleware("http")
async def _slow_log(request, call_next):
    t0 = time.time()
    resp = await call_next(request)
    dt = time.time() - t0
    if dt > 0.5:
        print("SLOW %5.1fs %s %s" % (dt, request.method, request.url.path), flush=True)
    return resp


def _manifest(product_id):
    p = os.path.join(PRODUCTS, product_id + ".json")
    return p if os.path.exists(p) else None


# Viewer mesh format: the kernel writes a binary glTF (partview.write_design), one node per part with extras {ref}.
MESH_EXT = derived.mesh_ext()


def _mesh_path(product_id):
    return derived.mesh_path(product_id)


def _mesh_stale(product_id):
    return _stale(_mesh_path(product_id), product_id)


def _stale(out_path, product_id):
    """True when the manifest or any part file is newer than the exported file (framework/common/derived.py)."""
    return derived.stale(out_path, product_id)


# the 3D view and the download are made by the engines service; the studio only asks, through the dual recipes
# (the first engine answers, the second engine and the comparison trail).
ENGINE_JOBS = {"mesh": "dual.mesh", "download": "dual.step_out"}


def _engine_file(kind, product_id, label):
    """Ask the engines service for a product's file and wait. -> the absolute path, or None (one line is printed)."""
    try:
        r = job_client.run(ENGINE_JOBS[kind], {"product_id": product_id}, timeout_s=_policy.wait_s(ENGINE_JOBS[kind]))   # the job row + 30 s
    except (job_client.JobError, TimeoutError) as e:
        print("%s %s failed: %s" % (label, product_id, getattr(e, "code", None) or type(e).__name__))
        return None
    p = os.path.join(ROOT, *r["path"].split("/")) if r and r.get("path") else None
    return p if p and os.path.exists(p) else None


def export_mesh(product_id):
    """The product's viewer mesh, made by the engines service (FreeCAD primary, kernel shadow compared there)."""
    if not _manifest(product_id):
        return None
    return _engine_file("mesh", product_id, "mesh")


STEPS = derived.STEPS


def export_step(product_id):
    """One STEP file per product: a fresh file is returned at once, else the engines service makes it."""
    if not _manifest(product_id):
        return None
    out = derived.step_path(product_id)
    if not _stale(out, product_id):
        return out
    return _engine_file("download", product_id, "step")




def _sse(event, data):
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data))


def _mesh_url(product_id):
    p = _mesh_path(product_id)
    return "/static/meshes/%s%s?v=%d" % (product_id, MESH_EXT, int(os.path.getmtime(p))) if os.path.exists(p) else None


_state_cache = {"t": 0.0, "v": None}


def _title_name(var):
    return (os.environ.get(var) or "").strip()[:80]


@app.get("/api/title-block")
def api_title_block():
    """The names in the drawing title block: BUILDCORE_DRAWN_BY and BUILDCORE_CHECKED_BY (blank when unset)."""
    return {"drawn_by": _title_name("BUILDCORE_DRAWN_BY"), "checked_by": _title_name("BUILDCORE_CHECKED_BY")}


@app.get("/api/state")
def api_state():
    """Cheap change detector. The page polls this and refreshes when `version`
    moves, so edits made in another tab OR from a terminal show up. The
    catalogue service computes it (catalogue.q.state); the 2 s cache stays here."""
    if time.time() - _state_cache["t"] < 2.0 and _state_cache["v"]:
        return _state_cache["v"]
    a = _ask("catalogue.q.state", {"mesh_ext": MESH_EXT})
    if a is None:
        if _state_cache["v"]:
            return _state_cache["v"]
        return _cat_down()
    v = json.loads(a.text)
    _state_cache.update(t=time.time(), v=v)
    return v


# ---- where the catalogue pages come from: the catalogue service answers every page read on its query
# queue (catalogue_q / gw_reply); this file only asks. The answer's text is the exact JSON body, and X-Catalogue
# says which source the catalogue used. There is no SQLite fallback in the studio any more: when the catalogue does
# not answer in time the page gets one plain sentence (503).
CAT_DOWN = "The catalogue is not available right now."


def _ask(qtype, params=None):
    """-> the catalogue's Answer, or None when it did not answer in time (one log line)."""
    try:
        return _asker.default().ask(qtype, params or {})
    except _asker.Unavailable as exc:
        print("catalogue: %s not answered (%s)" % (qtype, exc), flush=True)
        return None


def _raw(a):
    """The catalogue's body bytes unchanged, with X-Catalogue when it named a source."""
    return Response(a.text.encode("utf-8"), status_code=a.code, media_type="application/json",
                    headers={"X-Catalogue": a.source} if a.source else None)


def _cat_down(message=CAT_DOWN):
    return JSONResponse({"error": message}, status_code=503)


def _cat_json(body, src, status_code=200):
    return JSONResponse(body, status_code=status_code, headers={"X-Catalogue": src})


@app.get("/api/products")
def api_products():
    a = _ask("catalogue.q.products")
    return _raw(a) if a else _cat_down()


# ---- catalogue search (the catalogue service answers it): GET /api/query?q=<json>,
# POST /api/query (body = the request JSON). The request bytes travel base64 to the catalogue, which checks the words,
# caps the limit and runs the search; a bad request is a 400 with the plain words, no answer in 8 s is a 503
# "search unavailable"; trashed products never appear.
_QUERY_MAX_BYTES = 16 * 1024


def _search(raw, too_large=False):
    import base64
    params = {"raw_b64": base64.b64encode(raw).decode("ascii") if raw is not None else None, "too_large": too_large}
    a = _ask("catalogue.q.search", params)
    return _raw(a) if a else _cat_down("search unavailable")


@app.get("/api/query")
async def api_query_get(q: str = None):
    return await run_in_threadpool(_search, q.encode("utf-8") if q is not None else None)


@app.post("/api/query")
async def api_query_post(request: Request):
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _QUERY_MAX_BYTES:
        return await run_in_threadpool(_search, None, True)
    raw = await request.body()
    if len(raw) > _QUERY_MAX_BYTES:
        return await run_in_threadpool(_search, None, True)
    return await run_in_threadpool(_search, raw)


@app.get("/api/product/{product_id}")
def api_product(product_id: str):
    a = _ask("catalogue.q.product", {"product_id": product_id})
    if a is None:
        return _cat_down()
    if a.code != 200:
        return _raw(a)
    page = json.loads(a.text)             # same key order as the catalogue wrote; mesh_url goes last, as it always did
    # a stale 3D view is not made here: the page answers at once and posts /api/mesh (it shows "Preparing 3D view...")
    page["mesh_url"] = None if _mesh_stale(product_id) else _mesh_url(product_id)
    return _cat_json(page, a.source)


# ---- upload: STEP files -> a new product in the portfolio
UPLOADS = os.path.join(ROOT, "models", "uploads")
UPLOAD_MAX = 300 * 1024 * 1024


# naming and cataloguing of uploads are the upload.build recipe's steps; the studio only hands the files over
BUSY = {"mechanical": 0}    # uploads/builds running now
BUSY_LOCK = threading.Lock()


@app.post("/api/upload/file")
async def api_upload_file(request: Request, name: str):
    """Raw body = one STEP file. Returns a token for /api/upload/build."""
    if not re.search(r"\.(step|stp)$", name, re.I):
        return JSONResponse({"error": "Only STEP files (.step, .stp) can be added."}, status_code=400)
    body = await request.body()
    if not body or len(body) > UPLOAD_MAX:
        return JSONResponse({"error": "The file is empty or larger than 300 MB."}, status_code=400)
    if not body[:200].lstrip().upper().startswith(b"ISO-10303-21"):
        return JSONResponse({"error": "%s is not a STEP file." % name}, status_code=400)
    inc = os.path.join(UPLOADS, "_incoming")
    os.makedirs(inc, exist_ok=True)
    token = "%d_%s" % (int(time.time() * 1000), re.sub(r"[^A-Za-z0-9._+-]", "_", name))
    with open(os.path.join(inc, token), "wb") as fh:
        fh.write(body)
    return {"token": token, "bytes": len(body)}


class BuildIn(BaseModel):
    tokens: list[str]
    name: str | None = None
    single_part: bool = False        # "Add part to catalogue": one file, one solid
    material: str | None = None      # its material (a materials.json name); None: from the STEP file or steel


def upload_stream(tokens, name, single_part=False, material=None):
    """The studio's upload: it submits `upload.build` and shows the job's steps in today's words (framework/web/upload_follow.py).
    Nothing of the catalogue or the engines runs here; the files are only received and handed over by their inbox paths."""
    from framework.web import upload_follow
    t0 = time.time()
    inc = os.path.join(UPLOADS, "_incoming")
    names = [t for t in tokens if re.fullmatch(r"\d+_[A-Za-z0-9._+-]+", t) and os.path.exists(os.path.join(inc, t))]
    if not names:
        yield _sse("done", {"ok": False, "error": "No uploaded files found. Choose the files again."}); return
    if single_part and len(names) > 1:
        yield _sse("done", {"ok": False, "error": "Add one part at a time."}); return
    payload = {"files": ["models/uploads/_incoming/" + t for t in names], "name": name, "move": True}
    if single_part:
        payload["single_part"] = True
        if material is not None:
            payload["material"] = material
    stems = [re.sub(r"^\d{10,}_", "", re.sub(r"\.(step|stp)$", "", t, flags=re.I)) for t in names]
    with BUSY_LOCK:
        BUSY["mechanical"] += 1          # no permanent delete of products while one is being built (until its trailing steps end)
    try:
        job_id = job_client.submit("upload.build", payload)
    except job_client.JobError:
        with BUSY_LOCK:
            BUSY["mechanical"] -= 1
        yield _sse("done", {"ok": False, "error": "The design could not be added right now."}); return
    try:                                 # the files were received and handed over (counts only, never a name)
        size = sum(os.path.getsize(os.path.join(inc, t)) for t in names)
    except OSError:
        size = None
    slog.log("upload.received", job_id=job_id, type="upload.build", n=len(names), bytes=size)

    def release():                       # the job's second-engine 3D view and comparisons run after the answer
        end = time.monotonic() + upload_follow.LIMIT_S
        while time.monotonic() < end and not upload_follow.finished_all(job_id):
            time.sleep(0.25)
        with BUSY_LOCK:
            BUSY["mechanical"] -= 1
    threading.Thread(target=release, name="upload-release", daemon=True).start()
    n = 0
    for kind, *rest in upload_follow.follow(job_id, stems):
        if kind == "status":
            n += 1
            yield _sse("status", {"label": rest[0], "state": rest[1], "n": n})
        else:
            res = rest[0]
            if res.get("ok"):
                res.update(mesh_url=_mesh_url(res["product_id"]), seconds=round(time.time() - t0, 1))
            yield _sse("done", res)
            return


@app.post("/api/upload/build")
def api_upload_build(inp: BuildIn):
    return StreamingResponse(upload_stream(inp.tokens, inp.name, inp.single_part, inp.material), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/part/{product_id}/{ref}")
def api_part(product_id: str, ref: str):
    """One part of a product as the viewer's part card shows it: measured record
    plus its bores and diameters grouped by size. Cylinder faces larger than the
    part itself are gentle blends, not features, and are left out (counted)."""
    a = _ask("catalogue.q.part", {"product_id": product_id, "ref": ref})
    return _raw(a) if a else _cat_down()


@app.get("/api/step/{product_id}")
def api_step(product_id: str):
    """Download the product as one STEP file (AP214, mm, one named solid per part)."""
    a = _ask("catalogue.q.head", {"product_id": product_id})
    if a is None:
        return _cat_down()
    head = json.loads(a.text) if a.code == 200 else {}
    if a.code != 200 or not head.get("in_catalogue"):
        return JSONResponse({"error": "no such product"}, status_code=404)
    path = export_step(product_id)
    if not path:
        return JSONResponse({"error": "export failed"}, status_code=500)
    name = "%s-rev%s.step" % (product_id, head["revision"] or "A")
    return FileResponse(path, media_type="application/step", filename=name,
                        headers={"Cache-Control": "no-store"})


@app.post("/api/mesh/{product_id}")
def api_mesh(product_id: str):
    glb = export_mesh(product_id)
    if not glb:
        return JSONResponse({"error": "export failed"}, status_code=500)
    return {"mesh_url": _mesh_url(product_id)}


@app.get("/api/glb/{product_id}")
def api_glb(product_id: str):
    """Download the design's 3D model as one binary glTF: the kernel's viewer mesh (partview.write_design), one node per
    part with extras {ref}. A fresh mesh is served as it is; a stale or missing one is made by the dual.mesh job."""
    if not PID_RX.match(product_id):
        return JSONResponse({"error": "no such product"}, status_code=404)
    a = _ask("catalogue.q.head", {"product_id": product_id})
    if a is None:
        return _cat_down()
    head = json.loads(a.text) if a.code == 200 else {}
    if a.code != 200 or not head.get("in_catalogue"):
        return JSONResponse({"error": "no such product"}, status_code=404)
    path = _mesh_path(product_id)
    if not os.path.exists(path) or _mesh_stale(product_id):
        path = export_mesh(product_id)
    if not path or not path.lower().endswith(".glb") or not os.path.exists(path):
        return JSONResponse({"error": "export failed"}, status_code=500)
    name = "%s-rev%s.glb" % (product_id, head.get("revision") or "A")
    return FileResponse(path, media_type="model/gltf-binary", filename=name, headers={"Cache-Control": "no-store"})


BOM_CSV_HEADER = ("item", "name", "qty", "material", "bought_in", "ref")


def _csv_cell(v):
    """A BOM cell for a spreadsheet: a text starting with = + - @ (or a tab / carriage return) is prefixed with a single
    quote, so no formula runs. A lone "-" (the BOM's "no material") is no formula and stays as it is."""
    s = "" if v is None else str(v)
    return "'" + s if s != "-" and s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@app.get("/api/bom/{product_id}")
def api_bom(product_id: str, format: str = "json"):
    """Download the design's bill of materials (the drawings' parts list: identical parts one row with their qty and a
    common name) as JSON or CSV."""
    if not PID_RX.match(product_id):
        return JSONResponse({"error": "no such product"}, status_code=404)
    if format not in ("json", "csv"):
        return JSONResponse({"error": "format is json or csv"}, status_code=400)
    try:
        r = job_client.run("catalogue.drawing_bom", {"product_id": product_id},
                           timeout_s=_policy.wait_s("catalogue.drawing_bom"))
    except job_client.JobFailed as e:
        if e.code == "no_product":
            return JSONResponse({"error": "no such product"}, status_code=404)
        return _cat_down()
    except (job_client.JobError, TimeoutError):
        return _cat_down()
    if not r:
        return JSONResponse({"error": "no such product"}, status_code=404)
    rows = r.get("rows") or []
    if format == "json":
        body = {"product_id": r.get("product_id"), "name": r.get("name"), "revision": r.get("revision"), "rows": rows}
        return JSONResponse(body, headers={"Cache-Control": "no-store",
                                           "Content-Disposition": 'attachment; filename="%s-bom.json"' % product_id})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(BOM_CSV_HEADER)
    for row in rows:
        w.writerow([_csv_cell(row.get("item")), _csv_cell(row.get("name")), _csv_cell(row.get("qty")),
                    _csv_cell(row.get("material")), "yes" if row.get("bought_in") else "no", _csv_cell(row.get("ref"))])
    return Response(buf.getvalue(), media_type="text/csv", headers={
        "Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="%s-bom.csv"' % product_id})


# ---- mechanical drawings: the sheets made for a product (design-catalogue convention
# framework/web/static/files/<product_id>-<REF>.pdf|png|svg)
FILES = os.path.join(STATIC, "files")
PID_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,119}$")


@app.get("/api/drawings/{product_id}")
def api_drawings(product_id: str, previous: int = 0):
    """Drawing sheets of a product (files named <product_id>-<REF>.pdf|png|svg): listed by the catalogue service.
    `?previous=1` adds the studio's previous set (each entry says `set` current or previous)."""
    if not PID_RX.match(product_id):
        return JSONResponse({"error": "no such product"}, status_code=404)
    a = _ask("catalogue.q.drawings", {"product_id": product_id, "previous": bool(previous)})
    return _raw(a) if a else _cat_down()


# ---- the trash: the studio holds no trash code. Hiding a design, restoring it and erasing it for good are
# jobs: catalogue.trash, catalogue.restore and the product.erase recipe (catalogue.erase, then engines.drop_derived). The
# answers below are the ones the page has always had.
_REFERENCE_SENTENCE = "This is a reference design: it can be hidden or restored, not erased."
_TRASH_DOWN = {"error": "The design could not be removed right now."}


@app.delete("/api/product/{product_id}")
def api_product_delete(product_id: str):
    if not PID_RX.match(product_id):
        return JSONResponse({"error": "bad id"}, status_code=400)
    try:
        entry = job_client.run("catalogue.trash", {"product_id": product_id}, timeout_s=70)
    except job_client.JobFailed as e:
        if e.code == "no_product":
            return JSONResponse({"error": "no such product"}, status_code=404)
        if e.code == "bad_id":
            return JSONResponse({"error": "bad id"}, status_code=400)
        return JSONResponse(_TRASH_DOWN, status_code=503)
    except (job_client.JobError, TimeoutError):
        return JSONResponse(_TRASH_DOWN, status_code=503)
    _state_cache["t"] = 0
    return entry


@app.get("/api/trash")
def api_trash():
    a = _ask("catalogue.q.trash")
    return _raw(a) if a else _cat_down()


@app.post("/api/trash/{kind}/{item_id}/restore")
def api_trash_restore(kind: str, item_id: str):
    if kind != "mechanical":
        return JSONResponse({"error": "bad kind"}, status_code=400)
    try:
        job_client.run("catalogue.restore", {"kind": kind, "product_id": item_id}, timeout_s=70)
    except job_client.JobFailed as e:
        if e.code in ("not_in_trash", "bad_id"):
            return JSONResponse({"error": "not in the trash"}, status_code=404)
        return JSONResponse({"error": "The design could not be restored right now."}, status_code=503)
    except (job_client.JobError, TimeoutError):
        return JSONResponse({"error": "The design could not be restored right now."}, status_code=503)
    _state_cache["t"] = 0
    return {"ok": True, "kind": kind, "id": item_id}


def _wait_own_shadows(timeout_s=15.0):
    """An upload's kernel shadow may still be running in THIS process: wait for it (as engines.wait_shadow does)
    without importing engines code, so no erase starts while it is still writing its rows."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end and any(t.name.startswith("kernel-shadow-") and t.is_alive() for t in threading.enumerate()):
        time.sleep(0.2)


@app.delete("/api/trash/{kind}/{item_id}")
def api_trash_erase(kind: str, item_id: str):
    """Permanent delete: the product.erase recipe. Only items that came from an upload; never reference
    items; never while an upload/build is running."""
    if kind != "mechanical":
        return JSONResponse({"error": "bad kind"}, status_code=400)
    if BUSY["mechanical"]:
        return JSONResponse({"error": "A design is being added right now; try again when it is done."}, status_code=409)
    _wait_own_shadows(15)
    try:
        res = job_client.run("product.erase", {"product_id": item_id}, timeout_s=990)
    except job_client.JobFailed as e:
        if e.code in ("not_in_trash", "bad_id"):
            return JSONResponse({"error": "not in the trash"}, status_code=404)
        if e.code == "reference_design":
            return JSONResponse({"error": _REFERENCE_SENTENCE}, status_code=403)
        if e.code == "busy":
            return JSONResponse({"error": "This design is busy right now; try again in a minute."}, status_code=409)
        return JSONResponse({"error": "The design could not be erased right now."}, status_code=500)
    except (job_client.JobError, TimeoutError):
        return JSONResponse({"error": "The design could not be erased right now."}, status_code=500)
    removed = results.unpack(res["steps"]["erase"])
    removed["files"] = removed.get("files", 0) + results.unpack(res["steps"]["drop_derived"]).get("files", 0)
    _state_cache["t"] = 0
    return {"ok": True, "kind": kind, "id": item_id, "removed": removed}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")

if __name__ == "__main__":
    import uvicorn
    os.makedirs(MESHES, exist_ok=True)
    port = int(os.environ.get("PORT", "8766"))
    print("open http://127.0.0.1:%d" % port)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
