"""The catalogue page and its reads.

    GET  /catalogue                     the page (static/catalogue.html, no-store)
    POST /api/catalogue/search          the page's search: body = {where, filters, rank, words, kind, same, sort, offset,
                                        limit} (framework/catalog/query.py `browse`) -> parts and assemblies, counted and paged
    GET  /api/catalogue/families        the families that have parts (with counts) and the catalogue's totals
    GET  /api/catalogue/materials       the materials a catalogue part can be given (materials.json): {default, materials}
    GET  /api/catalogue/part?id=<part>  one part's numbers, the designs that use it, its identical parts; the rest of
                                        its record, features with lengths, chamfers
    GET  /api/catalogue/part/view?id=   the part's own 3D view -> {mesh_url, ...}; made by a geometry.part_view job
                                        the first time (the kernel, never here), then served from work/partviews/; a part with
                                        no kept shape answers {mesh_url: null, note: <one sentence>}
    GET  /api/catalogue/part/mesh?id=&v=   the view file itself (immutable: v is its key)
    GET  /api/catalogue/part/step?id=   the part alone as STEP (geometry.part_step the first time), named by its label
    GET  /api/catalogue/design?id=      an assembly's numbers and parts list (catalogue.q.catdesign)

Every read is a page read on the catalogue service's query queue: `catalogue.q.browse | families | catpart`,
answered by framework/catalog/svc_queries.py from Postgres while it is current; this module only asks and passes the
catalogue's JSON text on unchanged (X-Catalogue says the source). No answer in time is one plain sentence (503). The gateway
imports only framework.common.* and framework.web.*.
"""
import json
import os

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response

from framework.common import asker as _asker
from framework.common import partview as _pv
from framework.web import job_client

router = APIRouter()

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DOWN = "The catalogue is not available right now."
VIEW_DOWN = "The 3D view could not be made right now."
STEP_DOWN = "The download could not be made right now."
JOB_WAIT_S = 200               # geometry.part_view / part_step: the policy's 180 s plus the hops
NO_SHAPE = object()            # _shape_of / _job: the part has no kept shape
MAX_BYTES = 16 * 1024          # the same limit as the search box's request (app.py _QUERY_MAX_BYTES)


def _ask(qtype, params):
    try:
        a = _asker.default().ask(qtype, params)
    except _asker.Unavailable as exc:
        print("catalogue: %s not answered (%s)" % (qtype, exc), flush=True)
        return JSONResponse({"error": DOWN}, status_code=503)
    return Response(a.text.encode("utf-8"), status_code=a.code, media_type="application/json",
                    headers={"X-Catalogue": a.source} if a.source else None)


@router.get("/catalogue")
def catalogue_page():
    return FileResponse(os.path.join(STATIC, "catalogue.html"), headers={"Cache-Control": "no-store"})


@router.post("/api/catalogue/search")
async def catalogue_search(request: Request):
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BYTES:
        return JSONResponse({"error": "request too large"}, status_code=413)
    raw = await request.body()
    if len(raw) > MAX_BYTES:
        return JSONResponse({"error": "request too large"}, status_code=413)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "not a valid request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "not a valid request"}, status_code=400)
    return await run_in_threadpool(_ask, "catalogue.q.browse", {"request": body})


@router.get("/api/catalogue/families")
def catalogue_families():
    return _ask("catalogue.q.families", {})


@router.get("/api/catalogue/materials")
def catalogue_materials():
    return _ask("catalogue.q.materials", {})


@router.get("/api/catalogue/part")
def catalogue_part(id: str = ""):
    if not id or len(id) > 300:
        return JSONResponse({"error": "no such part"}, status_code=404)
    return _ask("catalogue.q.catpart", {"part_id": id})


# ---- one part on its own --------------------------------------------------------------------------------------------
def _shape_of(part_id):
    """-> (body, None) with the catalogue's {part_id, label, sources}, or (None, the response to send)."""
    if _pv.split_part(part_id) is None:
        return None, JSONResponse({"error": "no such part"}, status_code=404)
    try:
        a = _asker.default().ask("catalogue.q.partshape", {"part_id": part_id})
    except _asker.Unavailable as exc:
        print("catalogue: partshape not answered (%s)" % exc, flush=True)
        return None, JSONResponse({"error": DOWN}, status_code=503)
    body = json.loads(a.text) if a.text else {}
    if a.code != 200:
        return None, JSONResponse({"error": body.get("error") or DOWN}, status_code=a.code if a.code in (404, 503) else 503)
    if not body.get("sources"):
        return None, NO_SHAPE                     # the caller answers: the view with its sentence, the download 404
    return body, None


def _job(kind, payload, down):
    """Run a geometry job and wait. -> (result, None) or (None, the response to send)."""
    try:
        return job_client.run(kind, payload, timeout_s=JOB_WAIT_S), None
    except job_client.JobFailed as exc:
        if exc.code == "no_shape":
            return None, NO_SHAPE
        print("%s failed: %s" % (kind, exc.code), flush=True)
    except (job_client.JobError, TimeoutError) as exc:
        print("%s failed: %s" % (kind, type(exc).__name__), flush=True)
    return None, JSONResponse({"error": down}, status_code=503)


def _no_view(part_id):
    """A part with no kept shape: the view answers its one sentence (200; the page shows it where the 3D would be)."""
    return JSONResponse({"part_id": part_id, "mesh_url": None, "note": _pv.NO_SHAPE}, headers={"Cache-Control": "no-store"})


def _view_answer(part_id, key, made):
    try:
        with open(_pv.path(part_id, key, "json"), encoding="utf-8") as fh:
            facts = json.load(fh)
    except (OSError, ValueError):
        facts = {}
    return JSONResponse({"part_id": part_id, "mesh_url": "/api/catalogue/part/mesh?id=%s&v=%s" % (part_id, key),
                         "made_now": made, "triangles": facts.get("triangles")},
                        headers={"Cache-Control": "no-store"})


@router.get("/api/catalogue/part/view")
def catalogue_part_view(id: str = ""):
    """The part's own 3D view: served from the cache when it was made from these sources with this rule, else made by a
    geometry.part_view job first (the gateway never meshes)."""
    shape, bad = _shape_of(id)
    if bad is NO_SHAPE:
        return _no_view(id)
    if bad:
        return bad
    try:
        key = _pv.key("view", shape["sources"])
    except OSError:
        return JSONResponse({"error": VIEW_DOWN}, status_code=503)
    if _pv.fresh(id, key, "json") and _pv.fresh(id, key, "glb"):
        return _view_answer(id, key, False)
    res, bad = _job("geometry.part_view", {"part_id": id, "sources": shape["sources"], "key": key}, VIEW_DOWN)
    if bad is NO_SHAPE:
        return _no_view(id)
    if bad:
        return bad
    if not (_pv.fresh(id, key, "json") and _pv.fresh(id, key, "glb")):
        return JSONResponse({"error": VIEW_DOWN}, status_code=503)
    return _view_answer(id, key, not res.get("reused"))


@router.get("/api/catalogue/part/mesh")
def catalogue_part_mesh(id: str = "", v: str = ""):
    p = _pv.fresh(id, v, "glb")
    if not p:
        return JSONResponse({"error": "no such view"}, status_code=404)
    return FileResponse(p, media_type="model/gltf-binary", headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/api/catalogue/part/step")
def catalogue_part_step(id: str = ""):
    """The part alone as STEP, named by its label (never an internal id)."""
    shape, bad = _shape_of(id)
    if bad is NO_SHAPE:
        return JSONResponse({"error": _pv.NO_SHAPE}, status_code=404)
    if bad:
        return bad
    try:
        key = _pv.key("step", shape["sources"])
    except OSError:
        return JSONResponse({"error": STEP_DOWN}, status_code=503)
    name = _pv.download_name(shape.get("label"))
    p = _pv.fresh(id, key, "step")
    if not p:
        _res, bad = _job("geometry.part_step", {"part_id": id, "sources": shape["sources"], "key": key,
                                                "name": (shape.get("label") or "part")[:200]}, STEP_DOWN)
        if bad is NO_SHAPE:
            return JSONResponse({"error": _pv.NO_SHAPE}, status_code=404)
        if bad:
            return bad
        p = _pv.fresh(id, key, "step")
        if not p:
            return JSONResponse({"error": STEP_DOWN}, status_code=503)
    return FileResponse(p, media_type="application/step", filename=name, headers={"Cache-Control": "no-store"})


@router.get("/api/catalogue/design")
def catalogue_design(id: str = ""):
    if not id or len(id) > 120:
        return JSONResponse({"error": "no such design"}, status_code=404)
    return _ask("catalogue.q.catdesign", {"product_id": id})

