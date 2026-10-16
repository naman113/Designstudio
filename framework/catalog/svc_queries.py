"""The catalogue service's page reads: what the studio's pages ask the catalogue, answered on `catalogue_q`.

The code below answers the studio's page reads (framework/web/app.py is the gateway). The numbers come from the same functions as before (read_models, query, ingest); nothing here measures.

    handler(qctx, params) -> (code, source, body)       source "pg" | "sqlite" | None, as the studio's X-Catalogue header

    catalogue.q.products   the product list            catalogue.q.drawings   a product's drawing sheets
    catalogue.q.product    a product page              catalogue.q.trash      the trash list
    catalogue.q.part       a part card                 catalogue.q.head       a product's name and revision
    catalogue.q.state      the change signature        catalogue.q.search     the search box
    catalogue.q.drawings_state   the studio's current drawings set of a design
    catalogue.q.browse     the catalogue page's search   catalogue.q.families   its family filter and totals
    catalogue.q.catpart    one part's numbers and the rest of its record on the catalogue page
    catalogue.q.partshape  where one part's exact shape can be read (its own 3D view and STEP file; gateway only)
    catalogue.q.catdesign  an assembly's view on the catalogue page: its numbers and parts list
    catalogue.q.materials  the materials a catalogue part can be given (materials.json)

The trash writers are the trash jobs (svc_trash.py); only the readers are here. `mesh_url` and the mesh export stay
in the gateway.
"""
import base64
import hashlib
import json
import os
import re
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CATALOG = os.path.join(ROOT, "framework", "catalog")
PRODUCTS = os.path.join(ROOT, "framework", "products")
STATIC = os.path.join(ROOT, "framework", "web", "static")
MESHES = os.path.join(STATIC, "meshes")
FILES = os.path.join(STATIC, "files")
PID_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,119}$")

if CATALOG not in sys.path:          # append, never insert: no catalogue module may shadow the standard library
    sys.path.append(CATALOG)
if ROOT not in sys.path:
    sys.path.append(ROOT)
import ingest  # noqa: E402
from framework.catalog import query as catquery  # noqa: E402
from framework.common import paths  # noqa: E402
from framework.catalog import trash as _trash  # noqa: E402
from framework.catalog.pg import read_models as _rm  # noqa: E402


def _manifest(product_id):
    p = os.path.join(PRODUCTS, product_id + ".json")
    return p if os.path.exists(p) else None


# ---- the trash file: one copy of its helpers, framework/catalog/trash.py (this service is its only writer)
_trash_load = _trash.load
_trashed = _trash.trashed


# ---- where the catalogue pages read from: Postgres when reachable AND current, else SQLite ----------------
# The search box and the catalogue page (no SQLite answer of their own) also read Postgres while its copy
# LAGS the SQLite catalogue because a sync is on its way: the last good copy, without only the newest change, instead of
# "not available". A sync commits all at once (pg/sync.py), so that copy is never half-written.
# last probe: when it ended, the stored fingerprint, whether a sync holds the mirror's lock, whether the last full resync failed
_cat_cache = {"t": -100.0, "pg_fp": None, "up": True, "running": False, "failed": False}
_cat_lock = threading.Lock()
_CAT_RECHECK_S = 2.0         # Postgres reachable: probe its stored fingerprint at most every 2 s
_CAT_RECHECK_DOWN_S = 10.0   # unreachable: a probe costs ~2 s (connect timeout), so try again only every 10 s
# How long a lagging copy is still answered from when no sync holds the lock: the moment between a write and its
# sync taking the lock, and a sync that failed (the next write's sync, or this service's start, catches up). Longer
# than that, the old rule: not available.
_CAT_LAG_GRACE_S = float(os.environ.get("BC_PG_LAG_GRACE_S") or 120.0)
# A sync holding the lock extends that, but never past this age of the lag (a mismatch no sync can repair
# would otherwise keep a frozen copy answering for ever); and a failed full resync ends it at once (sync.last_sync_failed).
_CAT_LAG_MAX_S = float(os.environ.get("BC_PG_LAG_MAX_S") or 600.0)
# the stored fingerprint this process saw the copy lag with, and since when (monotonic). Tied to the fingerprint:
# a sync that committed while no request looked starts a new lag, it does not inherit the old one's age.
_cat_lag = {"fp": None, "since": None}


def _cat_note(what, exc):
    print("catalogue: %s from Postgres failed (%s: %s); served from SQLite" % (what, type(exc).__name__, str(exc)[:160]),
          flush=True)
    with _cat_lock:                            # do not retry an unreachable Postgres on every request
        _cat_cache.update(t=time.monotonic(), pg_fp=None, up=False)


def _catalogue_source(lag_ok=False):
    """"pg" or "sqlite". The SQLite fingerprint is computed per request (milliseconds), so a SQLite write the
    mirror has not caught up with is never answered from a stale mirror. Postgres itself is probed (its stored
    fingerprint read, and whether a sync holds the mirror's lock) at most every 2 s, or every 10 s while it is
    unreachable.
    `lag_ok` (the search box and the catalogue page): "pg" also while the copy lags SQLite: the last good
    copy keeps answering and the newest change appears when the sync commits. The lag's age counts from the first
    request that saw the copy lag with its present stored fingerprint; answered while it is younger than
    BC_PG_LAG_GRACE_S (120 s), or while a sync holds the lock and it is younger than BC_PG_LAG_MAX_S (600 s); never
    after a full resync failed (until a sync succeeds). The studio's own pages keep `lag_ok` False: they have SQLite."""
    from framework.common.config import pg_off
    if pg_off():
        return "sqlite"
    try:
        from framework.catalog.pg import db as pgdb, sync as pgsync
        cur = pgsync.fingerprint_sqlite(ingest.DB)
        window = _CAT_RECHECK_S if _cat_cache["up"] else _CAT_RECHECK_DOWN_S
        if time.monotonic() - _cat_cache["t"] >= window and _cat_lock.acquire(blocking=False):
            try:
                try:
                    con = pgdb.connect(autocommit=True, connect_timeout=2)
                    try:
                        fp = pgsync.stored_fingerprint(con)[0]
                        running = pgsync.sync_running(con)
                        failed = pgsync.last_sync_failed(con) is not None
                    finally:
                        con.close()
                    _cat_cache.update(pg_fp=fp, up=True, running=running, failed=failed)
                except Exception:
                    _cat_cache.update(pg_fp=None, up=False, running=False, failed=False)
                _cat_cache["t"] = time.monotonic()
            finally:
                _cat_lock.release()
        stored = _cat_cache["pg_fp"]
        if stored is None:
            return "sqlite"
        if stored == cur:
            _cat_lag.update(fp=None, since=None)
            return "pg"
        now = time.monotonic()
        if _cat_lag["fp"] != stored or _cat_lag["since"] is None:
            _cat_lag.update(fp=stored, since=now)
        age = now - _cat_lag["since"]
        if lag_ok and not _cat_cache["failed"] and (
                age < _CAT_LAG_GRACE_S or (_cat_cache["running"] and age < _CAT_LAG_MAX_S)):
            return "pg"
        return "sqlite"
    except Exception:
        return "sqlite"


def _source(qctx, params):
    """"sqlite" when a check asks for it (sender cli, params.source == "sqlite"), else the usual choice."""
    if params.get("source") == "sqlite" and qctx.sender == "cli":
        return "sqlite"
    return _catalogue_source()


def _pg_read(qctx, params, what, fn):
    """-> (result, "pg") from the Postgres read models, or (None, "sqlite") when Postgres is not the source
    or the read fails (one log line; the caller then runs its SQLite block).
    Runs on the answerer's pooled connection (qctx.con) instead of opening one per request."""
    if _source(qctx, params) != "pg":
        return None, "sqlite"
    try:
        return fn(qctx.con), "pg"
    except Exception as exc:
        _cat_note(what, exc)
        return None, "sqlite"


# ---- the product list -----------------------------------------------------------------------------------------------
def _products_sqlite():
    con = ingest.connect(readonly=True)
    rows = con.execute("""SELECT p.product_id, p.name, p.revision, p.description,
        (SELECT COUNT(*) FROM product_parts pp WHERE pp.product_id=p.product_id),
        (SELECT COUNT(*) FROM interfaces i WHERE i.product_id=p.product_id)
        FROM products p ORDER BY p.created_at DESC, p.name""").fetchall()
    con.close()
    return [{"product_id": r[0], "name": r[1], "revision": r[2], "description": r[3],
             "parts": r[4], "interfaces": r[5]} for r in rows]


def q_products(qctx, params):
    rows, src = _pg_read(qctx, params, "product list", lambda con: _rm.product_list(con))
    if rows is None:
        rows = _products_sqlite()
    hidden = _trashed("mechanical")
    return 200, src, [r for r in rows if r["product_id"] not in hidden]


# ---- the product page -----------------------------------------------------------------------------------------------
def _product_sqlite(product_id):
    """The product page without mesh_url; None when there is no such product."""
    con = ingest.connect(readonly=True)
    prod = con.execute("SELECT product_id, name, revision, description FROM products WHERE product_id=?",
                       (product_id,)).fetchone()
    if not prod:
        con.close()
        return None
    bom = [dict(zip(("ref", "qty", "label", "family", "volume_mm3", "bbox_a_mm", "bbox_b_mm", "bbox_c_mm", "source"), r))
           for r in con.execute("""SELECT pp.ref_des, pp.qty, pa.label, pa.family, pa.volume_mm3,
                pa.bbox_a_mm, pa.bbox_b_mm, pa.bbox_c_mm, d.path
                FROM product_parts pp JOIN parts pa ON pa.part_id=pp.part_id JOIN documents d ON d.doc_id=pa.doc_id
                WHERE pp.product_id=? ORDER BY pp.ref_des""", (product_id,))]
    for b in bom:
        b["source"] = paths.basename(b["source"])           # a Windows path stored by the catalogue, read on Linux
    ifs = [dict(zip(("kind", "ref_a", "ref_b", "nominal_a_mm", "nominal_b_mm", "clearance_mm", "fit_class", "contact_area_mm2"), r))
           for r in con.execute("""SELECT kind, ref_a, ref_b, nominal_a_mm, nominal_b_mm, clearance_mm, fit_class, contact_area_mm2
                FROM interfaces WHERE product_id=? ORDER BY kind, nominal_a_mm DESC""", (product_id,))]
    con.close()
    return {"product_id": prod[0], "name": prod[1], "revision": prod[2], "description": prod[3],
            "bom": bom, "interfaces": ifs}


def _same_groups(product_id):
    """{ref: (group number, parts in the group)} -- identical parts share a group (drawing_bom._shape_keys: volume, area,
    principal moments, faces, material): the same item is one item with a quantity, and
    the viewer gives one colour to one group."""
    try:
        from framework.catalog import drawing_bom
        keys = drawing_bom._shape_keys(product_id)
    except Exception:
        return {}
    gid, size = {}, {}
    for ref in sorted(keys):
        gid.setdefault(keys[ref], len(gid))
        size[keys[ref]] = size.get(keys[ref], 0) + 1
    return {ref: (gid[k], size[k]) for ref, k in keys.items()}


def q_product(qctx, params):
    product_id = str(params.get("product_id") or "")
    page, src = _pg_read(qctx, params, "product page", lambda con: _rm.product_payload(con, product_id) or {})
    if page is None:                      # Postgres not used, or it failed: the SQLite block
        page = _product_sqlite(product_id)
    elif not page:                        # Postgres says there is no such product
        page = None
    if page is None:
        return 404, src, {"error": "no such product"}
    same = _same_groups(product_id)
    for b in page.get("bom") or []:
        g = same.get(b.get("ref"))
        if g:
            b["same"], b["same_n"] = g
    page["bom_groups"] = _bom_groups(page.get("bom") or [])
    return 200, src, page                 # no mesh_url here; the gateway adds it


def _bom_groups(bom):
    """The BOM as a person reads it: one row per group of identical parts (the `same` group the viewer colours use; a
    ref with no group is its own row), in first-appearance order, with the summed qty, the members' refs and their common
    name. The per-ref rows (`bom`) stay as they are: the studio looks parts up by ref there. Never raises: on an error
    every ref is its own row."""
    from framework.catalog import drawing_bom

    def row(i, members, same):
        first = members[0]
        if len(members) > 1:
            name = drawing_bom._common_name([drawing_bom.readable(m.get("label"), m.get("ref")) for m in members])
        else:
            name = drawing_bom.readable(first.get("label"), first.get("ref"))
        return {"item": i, "ref": first.get("ref"), "refs": [m.get("ref") for m in members], "name": name,
                "label": first.get("label"), "qty": sum(int(m.get("qty") or 1) for m in members),
                "volume_mm3": first.get("volume_mm3"), "same": same}

    try:
        groups, order = {}, []
        for b in bom:
            gk = ("same", b["same"]) if b.get("same") is not None else ("ref", b.get("ref"))
            if gk not in groups:
                groups[gk] = []
                order.append(gk)
            groups[gk].append(b)
        return [row(i, groups[gk], gk[1] if gk[0] == "same" else None) for i, gk in enumerate(order, start=1)]
    except Exception:
        out = []
        for i, b in enumerate(bom, start=1):
            out.append({"item": i, "ref": b.get("ref"), "refs": [b.get("ref")], "name": str(b.get("label") or b.get("ref")),
                        "label": b.get("label"), "qty": b.get("qty"), "volume_mm3": b.get("volume_mm3"), "same": None})
        return out


# ---- the part card --------------------------------------------------------------------------------------------------
def _part_sqlite(product_id, ref):
    """One part of a product as the viewer's part card shows it (None when there is no such part): the
    rows come from SQLite, the grouping from read_models.build_part_card (shared with the Postgres path)."""
    con = ingest.connect(readonly=True)
    con.row_factory = __import__("sqlite3").Row
    pp = con.execute("""SELECT pp.ref_des, pp.qty, pa.* FROM product_parts pp JOIN parts pa ON pa.part_id=pp.part_id
                        WHERE pp.product_id=? AND pp.ref_des=?""", (product_id, ref)).fetchone()
    if not pp:
        con.close()
        return None
    cyl_rows = con.execute("""SELECT role, dia_mm, extent_mm, axis_x, axis_y, axis_z, pos_x, pos_y, pos_z,
                                     bb_xmin, bb_xmax, bb_ymin, bb_ymax, bb_zmin, bb_zmax
                              FROM features WHERE part_id=? AND kind='cylinder'""", (pp["part_id"],)).fetchall()
    kinds = dict(con.execute("SELECT kind, COUNT(*) FROM features WHERE part_id=? GROUP BY kind", (pp["part_id"],)).fetchall())
    card = _rm.build_part_card(pp, cyl_rows, kinds)
    con.close()
    return card


def q_part(qctx, params):
    product_id, ref = str(params.get("product_id") or ""), str(params.get("ref") or "")
    card, src = _pg_read(qctx, params, "part card", lambda con: _rm.part_card(con, product_id, ref) or {})
    if card is None:
        card = _part_sqlite(product_id, ref)
    elif not card:
        card = None
    if card is None:
        return 404, src, {"error": "no such part"}
    g = _same_groups(product_id).get(ref)
    if g:
        card["same_n"] = g[1]
    return 200, src, card


# ---- the change signature the page polls (/api/state) ---------------------------------------------------------------
def _mesh_path(product_id, mesh_ext):
    return os.path.join(MESHES, product_id + mesh_ext)


def _product_sig(con, product_id, mesh_ext):
    """Everything that should make the viewer refresh: BOM, interfaces, the
    manifest and the part files on disk, and the exported mesh."""
    row = con.execute("""SELECT p.revision, p.created_at,
        (SELECT COUNT(*) FROM product_parts pp WHERE pp.product_id=p.product_id),
        (SELECT COUNT(*) FROM interfaces i WHERE i.product_id=p.product_id),
        (SELECT MAX(d.ingested_at) FROM product_parts pp JOIN parts pa ON pa.part_id=pp.part_id
           JOIN documents d ON d.doc_id=pa.doc_id WHERE pp.product_id=p.product_id)
        FROM products p WHERE p.product_id=?""", (product_id,)).fetchone()
    parts = [str(x) for x in (row or ())]
    man = _manifest(product_id)
    if man:
        parts.append(str(int(os.path.getmtime(man))))
        try:
            with open(man, encoding="utf-8") as fh:
                m = json.load(fh)
            base = os.path.dirname(man)
            for e in m.get("parts", []):
                f = e["file"] if os.path.isabs(e["file"]) else os.path.join(base, e["file"])
                parts.append(str(int(os.path.getmtime(f))) if os.path.exists(f) else "missing")
        except Exception:
            parts.append("bad-manifest")
    obj = _mesh_path(product_id, mesh_ext)
    parts.append(str(int(os.path.getmtime(obj))) if os.path.exists(obj) else "nomesh")
    return "|".join(parts)


_state_cache = {"t": 0.0, "ext": None, "v": None}


def q_state(qctx, params):
    """Cheap change detector. The page polls this and refreshes when `version` moves, so edits made in the studio OR
    from a terminal show up. The 2 s cache lives here (keyed by the mesh format)."""
    ext = str(params.get("mesh_ext") or ".glb")
    if time.time() - _state_cache["t"] < 2.0 and _state_cache["v"] and _state_cache["ext"] == ext:
        return 200, None, _state_cache["v"]
    con = ingest.connect(readonly=True)
    hidden = _trashed("mechanical")
    ids = [r[0] for r in con.execute("SELECT product_id FROM products ORDER BY product_id") if r[0] not in hidden]
    sigs = {pid: _product_sig(con, pid, ext) for pid in ids}
    con.close()
    version = hashlib.sha1("\n".join("%s=%s" % kv for kv in sorted(sigs.items())).encode()).hexdigest()[:12]
    out = {"version": version, "products": sigs}
    _state_cache.update(t=time.time(), ext=ext, v=out)
    return 200, None, out


# ---- the search box -----------------------------------------------------------------------------------------------
_QUERY_MAX_BYTES = 16 * 1024
_QUERY_MAX_LIMIT = 1000
_query_terms_cache = []


def _query_terms():
    """The plain search words (the synonym list of the dictionary), for 400 answers."""
    if not _query_terms_cache:
        try:
            with open(os.path.join(ROOT, "framework", "catalog", "pg", "dictionary.json"), encoding="utf-8") as fh:
                words = [x["term"] for x in json.load(fh).get("synonyms", [])]
        except Exception:
            words = ["bore", "od", "length", "width", "height", "volume", "family"]
        _query_terms_cache.extend(words + ["label", "like"])
    return list(_query_terms_cache)


def _query_bad(src, message="not a valid request"):
    return 400, src, {"error": message, "terms": _query_terms()}


def _query_drop_hidden(con, response, request, hidden):
    """Take products in the trash out of an answer (the coverage line is left as it is)."""
    if not hidden:
        return
    if "groups" in response:
        response["groups"] = [g for g in response["groups"] if g["product_id"] not in hidden]
        return
    rows = response.get("results")
    if not rows:
        return
    find = request.get("find", "part")
    if find == "product":
        response["results"] = [r for r in rows if r["id"] not in hidden]
    elif find == "interface":
        response["results"] = [r for r in rows if r["id"].split("/", 1)[0] not in hidden]
    elif find in ("part", "feature"):       # a part that lives only in trashed products is trashed with them
        part_ids = sorted({r["part_id"] for r in rows})
        owners = {}
        for part_id, product_id in con.execute(
                "SELECT part_id, product_id FROM occurrence WHERE part_id = ANY(%s)", (part_ids,)).fetchall():
            owners.setdefault(part_id, set()).add(product_id)
        response["results"] = [r for r in rows if not owners.get(r["part_id"]) or owners[r["part_id"]] - hidden]


def _query_run_blocking(qctx, request):
    """-> (status, source, body). Runs synchronously on the answerer's connection; the last good copy
    while a sync runs."""
    src = _catalogue_source(lag_ok=True)
    if src != "pg":
        return 503, src, {"error": "search unavailable"}
    con = qctx.con
    try:
        response = catquery.run(request, con=con)
        if "error" in response:
            return 400, src, {"error": response["error"], "terms": _query_terms()}
        _query_drop_hidden(con, response, request, _trashed("mechanical"))
        return 200, src, response
    except Exception as exc:
        print("catalogue: search failed (%s: %s)" % (type(exc).__name__, str(exc)[:160]), flush=True)
        return 503, src, {"error": "search unavailable"}


def q_search(qctx, params):
    """params: raw_b64 (the request bytes, base64, or None: the page sent no `q`), too_large (the body was declared bigger
    than the limit; the gateway need not send it). Base64 carries the bytes exactly, so invalid utf-8 is a 400 as before."""
    src = _catalogue_source(lag_ok=True)                       # X-Catalogue on every answer
    if params.get("too_large"):
        return 413, src, {"error": "request too large"}
    b64 = params.get("raw_b64")
    if b64 is None:
        return _query_bad(src)
    try:
        raw = base64.b64decode(b64)
    except ValueError:
        return _query_bad(src)
    if len(raw) > _QUERY_MAX_BYTES:
        return 413, src, {"error": "request too large"}
    try:
        request = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return _query_bad(src)
    if not isinstance(request, dict) or not isinstance(request.get("where") or {}, dict):
        return _query_bad(src)
    try:
        limit = request.get("limit")
        if limit is not None:
            request["limit"] = min(max(int(limit), 1), _QUERY_MAX_LIMIT)
    except (TypeError, ValueError):
        return _query_bad(src)
    return _query_run_blocking(qctx, request)


# ---- the catalogue page: parts and assemblies, the families, one part ----------------------------------------------------
CAT_PAGE_DOWN = "The catalogue is not available right now."


def _catalogue_page_read(qctx, what, fn):
    """Postgres while it is current, or while a sync brings it up to date (as the search box); the trash's
    designs and every design that is not a catalogue part (`_not_catalogue`: an opened assembly) are left out of
    every catalogue-page read -- the list, the families, a part, its 3D view and STEP, a design. -> (code, source, body)."""
    src = _catalogue_source(lag_ok=True)
    if src != "pg":
        return 503, src, {"error": CAT_PAGE_DOWN}
    try:
        return fn(qctx.con, set(_trashed("mechanical")) | _not_catalogue(qctx.con), src)
    except catquery.QueryError as exc:
        return 400, src, {"error": str(exc), "terms": _query_terms()}
    except Exception as exc:
        print("catalogue: %s failed (%s: %s)" % (what, type(exc).__name__, str(exc)[:160]), flush=True)
        return 503, src, {"error": CAT_PAGE_DOWN}


def _not_catalogue(con=None):
    """Designs that are not catalogue parts: every product whose manifest origin is not "part" (made by "Add part to
    catalogue"). An opened assembly ("upload"), a product with no readable manifest, or a design the mirror
    knows without a manifest is not catalogue; the catalogue page leaves their parts out as it leaves out the trash's."""
    ids = {f[:-5] for f in os.listdir(PRODUCTS) if f.endswith(".json")} if os.path.isdir(PRODUCTS) else set()
    if con is not None:
        ids |= {r[0] for r in con.execute("SELECT product_id FROM product").fetchall()}
    out = set()
    for pid in ids:
        try:
            with open(os.path.join(PRODUCTS, pid + ".json"), encoding="utf-8") as fh:
                origin = json.load(fh).get("origin")
        except (OSError, ValueError, AttributeError):
            origin = None
        if origin != "part":
            out.add(pid)
    return out


def q_browse(qctx, params):
    """params: {"request": the page's search (framework/catalog/query.py browse)}. Catalogue parts only."""
    request = params.get("request")
    if not isinstance(request, dict):
        return _query_bad(_catalogue_source(lag_ok=True))
    return _catalogue_page_read(
        qctx, "catalogue page search",
        lambda con, hidden, src: (200, src, catquery.browse(con, request, hidden)))


def q_families(qctx, params):
    return _catalogue_page_read(
        qctx, "families",
        lambda con, hidden, src: (200, src, catquery.families(con, hidden)))


def q_materials(qctx, params):
    """The materials a catalogue part can be given (framework/catalog/materials.json): {"default", "materials"}."""
    from framework.catalog.pg import partrecord
    try:
        m = partrecord.materials()
        return 200, None, {"default": m["default"]["material"], "materials": [x["material"] for x in m["materials"]]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("catalogue: materials failed (%s: %s)" % (type(exc).__name__, str(exc)[:160]), flush=True)
        return 503, None, {"error": CAT_PAGE_DOWN}


def q_catpart(qctx, params):
    """params: {"part_id": "<doc_id>:<object>"} -> one part's numbers, its designs and its identical parts; 404 if none."""
    part_id = params.get("part_id")
    if not isinstance(part_id, str) or not part_id or len(part_id) > 300:
        return 404, None, {"error": "no such part"}

    def read(con, hidden, src):
        body = catquery.part_facts(con, part_id, hidden)
        return (404, src, {"error": "no such part"}) if body is None else (200, src, body)
    return _catalogue_page_read(qctx, "part", read)


def q_partshape(qctx, params):
    """params: {"part_id"} -> {part_id, label, sources: [[path, object], ...]}; 404 if no such part."""
    part_id = params.get("part_id")
    if not isinstance(part_id, str) or not part_id or len(part_id) > 300:
        return 404, None, {"error": "no such part"}

    def read(con, hidden, src):
        body = catquery.part_shape(con, part_id, hidden)
        return (404, src, {"error": "no such part"}) if body is None else (200, src, body)
    return _catalogue_page_read(qctx, "part shape", read)


def q_catdesign(qctx, params):
    """params: {"product_id"} -> an assembly's numbers and parts list; 404 if no such design (or in the trash)."""
    product_id = params.get("product_id")
    if not isinstance(product_id, str) or not PID_RX.match(product_id):
        return 404, None, {"error": "no such design"}

    def read(con, hidden, src):
        body = catquery.design_facts(con, product_id, hidden)
        return (404, src, {"error": "no such design"}) if body is None else (200, src, body)
    return _catalogue_page_read(qctx, "design", read)


# ---- drawings -------------------------------------------------------------------------------------------------------
def _all_product_ids():
    ids = {f[:-5] for f in os.listdir(PRODUCTS) if f.endswith(".json")} if os.path.isdir(PRODUCTS) else set()
    try:
        con = ingest.connect(readonly=True)
        ids |= {r[0] for r in con.execute("SELECT product_id FROM products")}
        con.close()
    except Exception:
        pass
    return ids


def q_drawings(qctx, params):
    """Drawing sheets of a product: files named <product_id>-<REF>.pdf|png|svg. A file is
    given to the LONGEST product id it starts with, so product "x-1" does not list "x-1-2"'s sheets."""
    product_id = str(params.get("product_id") or "")
    ids = _all_product_ids()
    if not PID_RX.match(product_id) or product_id not in ids:
        return 404, None, {"error": "no such product"}
    out = _studio_sheets(product_id, bool(params.get("previous")))      # the studio's own set first (Views, ...)
    for f in sorted(os.listdir(FILES)) if os.path.isdir(FILES) else []:
        stem, ext = os.path.splitext(f)
        if ext.lower() not in (".pdf", ".png", ".svg") or not f.startswith(product_id + "-"):
            continue
        owner = max((i for i in ids if f.startswith(i + "-")), key=len)
        if owner != product_id:
            continue
        p = os.path.join(FILES, f)
        ref = stem[len(product_id) + 1:]
        mine = {"label": "Drawing %s" % ref, "ref": ref, "format": ext[1:].lower(),
                "url": "/static/files/%s?v=%d" % (f, int(os.path.getmtime(p))),
                "updated": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
                "kind": "designer", "group": "designer"}
        # an added sheet of a REF the studio also drew sits right before the studio's part sheet of that REF
        # ("Drawing <REF> · added sheet", then "Part · <name>"); the others (pictures, other names) stay at the end.
        # (case-insensitive; only a drawn sheet, pdf or svg, is placed there: a picture named like a REF stays at the end)
        at = next((i for i, x in enumerate(out) if ext.lower() in (".pdf", ".svg") and x.get("kind") == "part"
                   and x.get("set", "current") == "current" and str(x.get("ref")).lower() == ref.lower()), None)
        if at is None:
            out.append(mine)
        else:
            mine.update(label="Drawing %s · added sheet" % ref, group="parts")
            out.insert(at, mine)
    return 200, None, out


def _set_sheets(product_id, st, which, stale=False):
    """One kept set's sheets in index order, each marked `set` (current | previous), with the set's `made_at` and `revision`
    and, for a current set that no longer matches its design, `stale`."""
    from framework.catalog import svc_drawings
    made = _utc_to_local(st.get("made_at"))
    out = []
    for sh in st.get("sheets", []):
        p = os.path.join(svc_drawings.PUBLISHED, product_id, st["set"], sh["file"])
        try:
            v = int(os.path.getmtime(p))
        except OSError:
            continue
        out.append({"label": sh["label"], "ref": sh["ref"], "format": os.path.splitext(sh["file"])[1][1:].lower(),
                    "url": "/static/files/drawings/%s/%s/%s?v=%d" % (product_id, st["set"], sh["file"], v),
                    "updated": made, "kind": sh["kind"],
                    "group": {"part": "parts", "assembly": "assembly"}.get(sh["kind"], "studio"),
                    "set": which, "made_at": st.get("made_at"), "revision": st.get("revision"), "stale": bool(stale)})
    return out


def _studio_sheets(product_id, with_previous=False):
    """The current set's sheets in index order (Views, Assembly, the parts: framework/catalog/svc_drawings.py index.json), each with
    `kind` and `group` ("studio" for Views, "assembly", "parts"; sheets added beside them are "designer"). Each carries
    `set` "current"; `with_previous` (the page's drawing viewer) adds the second kept set's sheets after them, marked
    "previous". The default list stays the current set alone (the portfolio's count and the earlier checks read it)."""
    from framework.catalog import svc_drawings
    idx = svc_drawings.index_load(product_id)
    cur = svc_drawings.current_set(idx)
    if not cur:
        return []
    out = _set_sheets(product_id, cur, "current", _stale_of(product_id, cur)[0])
    if with_previous:
        prev = _previous_set(idx, cur)
        if prev:
            out += _set_sheets(product_id, prev, "previous")
    return out


def _previous_set(idx, cur):
    """The kept set before the current one (None when there is none): the next entry of the index after the current set."""
    return next((s for s in idx["sets"] if s["set"] != cur["set"]), None)


def _stale_of(product_id, st):
    """(stale, reason, current fingerprint) of a set. Stale = its fingerprint differs from the
    design's now (the drawing rules, the bought-in list, the manifest, every file it names: framework/common/drawfp.py, memoised
    by size and mtime). Reason "rules" when the set was made under older drawing rules, else "design". A design with no
    manifest any more is not stale (nothing to compare with)."""
    from framework.common import drawfp
    man = _manifest(product_id)
    try:
        fp = drawfp.fingerprint(man) if man else None
    except (OSError, ValueError):
        fp = None
    if fp is not None and st.get("parts_pending"):
        return True, "design", fp           # a set whose part drawings were never handed over (the job was stopped) is not current
    if fp is None or fp == st.get("fingerprint"):
        return False, None, fp
    older = int(st.get("rules_version") or 0) < drawfp.DRAW_RULES_VERSION
    return True, "rules" if older else "design", fp


def _utc_to_local(iso):
    import calendar
    try:
        t = calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def q_drawings_state(qctx, params):
    """The studio's current set of a design, read from its index.json: {"exists", "current": {set, fingerprint,
    made_at, revision, sheets, failed, sizes, stale, stale_reason, current_fingerprint} | null, "previous": {set, made_at,
    revision, sheets} | null} (stale = the set's fingerprint is not the design's now)."""
    product_id = str(params.get("product_id") or "")
    if not PID_RX.match(product_id) or product_id not in _all_product_ids():
        return 404, None, {"error": "no such product"}
    from framework.catalog import svc_drawings
    idx = svc_drawings.index_load(product_id)
    cur = svc_drawings.current_set(idx)
    if not cur:
        return 200, None, {"exists": False, "current": None}
    stale, why, fp = _stale_of(product_id, cur)
    prev = _previous_set(idx, cur)
    return 200, None, {"exists": True, "current": {
        "set": cur["set"], "fingerprint": cur["fingerprint"], "made_at": cur["made_at"], "revision": cur.get("revision"),
        "sheets": len(cur.get("sheets", [])), "failed": cur.get("failed", []), "bought_in": cur.get("bought_in", []),
        "sizes": cur.get("sizes", {}), "rules_version": cur.get("rules_version"), "current_fingerprint": fp, "stale": stale,
        "stale_reason": why},
        "previous": {"set": prev["set"], "made_at": prev["made_at"], "revision": prev.get("revision"),
                     "sheets": len(prev.get("sheets", []))} if prev else None}


# ---- trash list, product name and revision --------------------------------------------------------------------------
def q_trash(qctx, params):
    return 200, None, sorted(reversed(_trash_load()), key=lambda e: e["deleted_at"], reverse=True)   # ties: newest first


def q_head(qctx, params):
    """A product's name and revision (the studio's Delete uses the name, Download the revision). The name falls back to the
    manifest's `name`, as `_product_name` did."""
    product_id = str(params.get("product_id") or "")
    if not PID_RX.match(product_id):                 # the id becomes a file name below: only plain ids
        return 404, None, {"error": "no such product"}
    con = ingest.connect(readonly=True)
    row = con.execute("SELECT name, revision FROM products WHERE product_id=?", (product_id,)).fetchone()
    con.close()
    if row:
        return 200, None, {"product_id": product_id, "in_catalogue": True, "name": row[0], "revision": row[1]}
    man = _manifest(product_id)
    if man:
        with open(man, encoding="utf-8") as fh:
            name = json.load(fh).get("name", product_id)
        return 200, None, {"product_id": product_id, "in_catalogue": False, "name": name, "revision": None}
    return 404, None, {"error": "no such product"}


QUERIES = {
    "catalogue.q.products": q_products,
    "catalogue.q.product": q_product,
    "catalogue.q.part": q_part,
    "catalogue.q.state": q_state,
    "catalogue.q.search": q_search,
    "catalogue.q.drawings": q_drawings,
    "catalogue.q.drawings_state": q_drawings_state,
    "catalogue.q.trash": q_trash,
    "catalogue.q.head": q_head,
    "catalogue.q.browse": q_browse,          # the catalogue page
    "catalogue.q.families": q_families,
    "catalogue.q.catpart": q_catpart,
    "catalogue.q.partshape": q_partshape,    # the part view's 3D and download, the assembly view
    "catalogue.q.catdesign": q_catdesign,
    "catalogue.q.materials": q_materials,   # the material list (materials.json) for the studio and the catalogue page
}
