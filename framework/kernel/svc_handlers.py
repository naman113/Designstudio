"""The geometry service's command handlers: job types on a pool of warm worker processes.

    geometry.measure   geometry.read_step   geometry.mesh   geometry.step_out
    geometry.signature   geometry.topology   geometry.selftest
    geometry.draw_overview   (the views sheet of a design, framework/kernel/drawings/)
    geometry.draw_parts      (one first-angle drawing per drawable part, progress per part)
    geometry.measure_file    (the kernel's records of one STEP file inside the copy, unchanged)
    geometry.tight_extent    (parts' exact extents and solids for the catalogue's part records)
    geometry.part_view       geometry.part_step   (one part's own 3D view and its STEP file, from the
                             part's exact shape, cached per part and key in work/partviews/, framework/common/partview.py)
    geometry.drop_derived    (the product.erase recipe's step: removes a design's 3D views and STEP exports)

A handler is `fn(ctx, payload) -> dict`. It checks the payload (types, sizes, paths), hands the operation to the pool
(`worker_pool.Pool.submit`, with the job's cancel event, so Stop kills the worker running it), and answers with the path
of the result file the worker wrote (framework/kernel/ops.py) and a few counts, through `results.pack`. The full result
is a JSON file under work/results/ written with json.dump; nothing numeric travels as nested jsonb.

Paths in a payload are strings, absolute or relative to the project root, and are passed on exactly as given (see
ops.py). Inputs must exist and end in .step .stp .fcstd .brep (a manifest: .json under framework/products/ or work/).
Outputs must resolve under work/ or framework/web/static/meshes/. Anything else is `bad_path`.

Copy-safe: no state is shared between replicas; each replica owns its own pool, and every result
path is deterministic (`work/results/<job_id>.<op>.json`) and written as a temp file plus an atomic replace.
"""
import os
import re
import threading
import time

from framework.common import config, derived, paths, policy, results
from framework.common.consumer import ServiceError, Stopped
from framework.kernel import worker_pool

ROOT = config.ROOT
POOL = None                       # worker_pool.Pool, created by start_pool() in a background thread
_POOL_LOCK = threading.Lock()

# Op time limits in the pool (s). The policy rows (framework/common/policy.py) are these plus a 60 s margin.
# Every op a design or animation request waits on may run policy.LONG_RUN_S. Kept short on purpose: topology 120, part_view 120, part_step 120 (the catalogue page's
# part cards, not a request's wait) and the per-part drawing cap PART_LIMIT_S below.
_LONG = policy.LONG_RUN_S
TIMEOUT_S = {"measure": _LONG, "signature": _LONG, "read_step": _LONG, "mesh": _LONG, "step_out": _LONG,
             "topology": 120, "draw_overview": _LONG, "measure_file": _LONG,
             "draw_parts": _LONG, "part_view": 120, "part_step": 120, "tight_extent": _LONG}
PART_LIMIT_S = int(os.environ.get("GEO_PART_LIMIT_S", "240"))   # a part still being drawn after this is stopped and listed "it took too long"
REF_RX = re.compile(r"^[A-Za-z0-9_]+$")

ALLOWED_IN = (".step", ".stp", ".fcstd", ".brep")
STEP_ONLY = (".step", ".stp")
MAX_FILES, MAX_TARGETS, MAX_SECONDS, MAX_PAIRS = 20, 2000, 120, 200
MAX_EXTENT_TARGETS = 400       # parts per geometry.tight_extent job (one part: median 0.1 s, a large STEP read once: about 10 s)


def start_pool(root, n=None):
    """Create the pool (called from a background thread in main(): spawning four workers takes about 3 s in parallel)."""
    global POOL
    n = int(os.environ.get("GEO_WORKERS", "4")) if n is None else n
    pool = worker_pool.Pool(n, root)
    with _POOL_LOCK:
        POOL = pool
    return pool


def pool_stats():
    return POOL.stats() if POOL is not None else {"workers": 0, "ready": 0, "busy": 0}


# ---- payload checks ----------------------------------------------------------------------------------------------------
def _bad(why="request"):
    code = "bad_path" if why == "path" else "bad_request"
    return ServiceError(code, "This request could not be read.", detail="geometry payload: %s" % why)


_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def _full(p):
    # an absolute path in the host's form (drive letter, lower case) is opened where it lives in this process (paths.local)
    return paths.local(p) if os.path.isabs(p) or _DRIVE.match(p) else os.path.join(ROOT, p)


def _under(full, *folders):
    full = os.path.normcase(os.path.abspath(full))
    for f in folders:
        f = os.path.normcase(os.path.abspath(f))
        if full == f or full.startswith(f + os.sep):
            return True
    return False


def _in_path(p, exts=ALLOWED_IN, must_exist=True):
    """An input path: a plain string with an allowed extension, existing (unless told otherwise). Returned unchanged."""
    if not isinstance(p, str) or not p or len(p) > 1000 or "\x00" in p:
        raise _bad("path")
    full = _full(p)
    if os.path.splitext(full)[1].lower() not in exts:
        raise _bad("path")
    if must_exist and not os.path.isfile(full):
        raise _bad("path")
    return full if (os.path.isabs(p) or _DRIVE.match(p)) and full != p else p      # an absolute path as this process opens it (else as given)


def _manifest(p):
    if not isinstance(p, str) or not p or len(p) > 1000 or "\x00" in p:
        raise _bad("path")
    full = _full(p)
    if os.path.splitext(full)[1].lower() != ".json" or not os.path.isfile(full):
        raise _bad("path")
    if not _under(full, os.path.join(ROOT, "framework", "products"), os.path.join(ROOT, "work")):
        raise _bad("path")
    return p


def _out_path(p):
    if not isinstance(p, str) or not p or len(p) > 1000 or "\x00" in p:
        raise _bad("path")
    if not _under(_full(p), os.path.join(ROOT, "work"), os.path.join(ROOT, "framework", "web", "static", "meshes")):
        raise _bad("path")
    return p


def _num(v, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _bad()
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise _bad()
    return v


def _check_side(payload):
    if payload.get("side", "kernel") != "kernel":
        raise ServiceError("bad_side", "This request could not be read.", detail="side must be 'kernel'")


def _out_json(ctx, op):
    return os.path.join(ROOT, "work", "results", "%s.%s.json" % (ctx.job_id, op))


# ---- the pool call and its error mapping ---------------------------------------------------------------------------------
def _pool(ctx):
    """The pool; a job that arrives before it exists waits (and can still be stopped)."""
    while POOL is None:
        ctx.sleep(0.1)
    return POOL


def _check_fault(payload):
    """The CLI's way to make a step fail on purpose (to check the failure path).
    The gateway never forwards it."""
    if payload.get("check_fault") is True:
        raise ServiceError("check_fault", "This step failed on purpose.")


def _run(ctx, op, args, timeout_s, progress=False):
    """`progress=True`: the op sends `{"step": name}` messages (PROGRESS) and each becomes a job progress step."""
    ctx.progress(op)
    on_message = None
    if progress:
        def on_message(msg):
            if isinstance(msg, dict) and isinstance(msg.get("step"), str) and re.match(r"^[a-z_0-9]{1,40}$", msg["step"]):
                ctx.progress(msg["step"])
    r = _pool(ctx).submit(op, args, timeout_s, cancel=ctx.cancelled, on_message=on_message)
    if r.get("stopped"):
        raise Stopped()
    if r.get("timeout"):
        print("geometry %s timed out (job %s): no answer in %s s, worker %s killed" % (op, ctx.job_id, timeout_s, r.get("pid")), flush=True)
        raise ServiceError("geometry_timeout", "This geometry step took too long and was stopped.",
                           detail="%s: no answer in %s s, worker %s killed" % (op, timeout_s, r.get("pid")))
    if r.get("lost"):
        print("geometry %s lost its worker (job %s): worker %s" % (op, ctx.job_id, r.get("pid")), flush=True)
        raise ServiceError("geometry_worker_lost", "The geometry step stopped unexpectedly.", retryable=True,
                           detail="%s: the worker %s was lost" % (op, r.get("pid")))
    if not r.get("ok"):
        if r.get("kind") == "nodump":
            raise ServiceError("no_dump", "This design needs the first engine's pass first.", detail=r.get("err", ""))
        print("geometry %s failed (job %s): %s | %s" % (op, ctx.job_id, r.get("err", ""), (r.get("tb") or "")[-800:]),
              flush=True)                       # the detail reaches the service log, not only the reply
        raise ServiceError("geometry_failed", "The geometry step could not be done.",
                           detail="%s: %s | %s" % (op, r.get("err", ""), (r.get("tb") or "")[-800:]))
    ctx.check()
    return results.pack(ctx.job_id, dict(r["res"], worker_pid=r.get("pid"), sec=r["sec"]))


# ---- the eight handlers ------------------------------------------------------------------------------------------------------
def measure(ctx, payload):
    _check_fault(payload)
    _check_side(payload)
    path = _in_path(payload.get("path"))
    topo = payload.get("topology", False)
    if not isinstance(topo, bool):
        raise _bad()
    args = {"path": path, "out_json": _out_json(ctx, "measure"), "topology": topo}
    return _run(ctx, "measure", args, TIMEOUT_S["measure"])


def read_step(ctx, payload):
    _check_fault(payload)
    _check_side(payload)
    files = payload.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise _bad()
    files = [_in_path(f, STEP_ONLY) for f in files]
    return _run(ctx, "read_step", {"files": files, "out_json": _out_json(ctx, "read_step")},
                TIMEOUT_S["read_step"])      # one limit for the whole job


def mesh(ctx, payload):
    _check_fault(payload)
    manifest, out = _manifest(payload.get("manifest")), _out_path(payload.get("out"))
    header = payload.get("header", "")
    if not isinstance(header, str) or len(header) > 200:
        raise _bad()
    args = {"manifest": manifest, "out": out, "header": header, "out_json": _out_json(ctx, "mesh")}
    return _run(ctx, "mesh", args, TIMEOUT_S["mesh"])


def step_out(ctx, payload):
    _check_fault(payload)
    manifest, out = _manifest(payload.get("manifest")), _out_path(payload.get("out"))
    root_name = payload.get("root_name")
    if not isinstance(root_name, str) or not root_name or len(root_name) > 200:
        raise _bad()
    readback = payload.get("readback") or []
    if not isinstance(readback, list) or len(readback) > MAX_FILES:
        raise _bad()
    readback = [_in_path(p, STEP_ONLY, must_exist=False) for p in readback]
    args = {"manifest": manifest, "out": out, "root_name": root_name, "readback": readback,
            "out_json": _out_json(ctx, "step_out")}
    return _run(ctx, "step_out", args, TIMEOUT_S["step_out"])


def signature(ctx, payload):
    _check_fault(payload)
    targets = payload.get("targets")
    if not isinstance(targets, list) or len(targets) > MAX_TARGETS:
        raise _bad()
    clean = []
    for t in targets:
        if not isinstance(t, list) or len(t) != 2 or not isinstance(t[0], str) or not isinstance(t[1], list) \
                or len(t[1]) > 10:
            raise _bad()
        cands = []
        for c in t[1]:
            if not isinstance(c, list) or len(c) != 2 or not isinstance(c[1], str):
                raise _bad()
            p = _in_path(c[0], STEP_ONLY + (".brep",), must_exist=False)
            if p.lower().endswith(".brep") and not _under(_full(p), os.path.join(ROOT, "work", "engines", "brep")):
                raise _bad("path")          # a part's BREP dump is read only from the dump cache
            cands.append([p, c[1]])
        clean.append([t[0], cands])
    return _run(ctx, "signature", {"targets": clean, "out_json": _out_json(ctx, "signature")}, TIMEOUT_S["signature"])


def topology(ctx, payload):
    _check_fault(payload)
    files = payload.get("files")
    if not isinstance(files, dict) or not files or len(files) > MAX_TARGETS:
        raise _bad()
    clean = {str(k): _in_path(v, (".brep",)) for k, v in files.items()}
    return _run(ctx, "topology", {"files": clean, "out_json": _out_json(ctx, "topology")}, TIMEOUT_S["topology"])


def draw_overview(ctx, payload):
    """`{manifest, product_id, kinds: ["views"]}`; kinds may also be `["views", "assembly"]`,
    which needs `bom` (the catalogue's parts list: names, quantities, materials, bought-in flags) and makes the assembly drawing
    in the same load. `check_fault` ("number": the assembly drawing prints its length 0.02 mm wrong, the CLI's proof that the
    read-back check keeps a bad sheet out) is accepted only from a job the CLI submitted. The output folder is derived here from
    the job id (work/drawings/<pid>/<job_id>/), never read from the payload. `preview` (True, CLI only) also
    writes a picture of the assembly's first page beside the report."""
    import json
    if set(payload) - {"manifest", "product_id", "kinds", "bom", "check_fault", "preview"}:       # no output path, no extra key
        raise _bad()
    preview = payload.get("preview")
    if preview is not None and (preview is not True or _submitted_by(ctx) != "cli"):
        raise _bad()                                            # a picture of page 1 for the CLI's checks only
    fault = payload.get("check_fault")
    if fault is True:
        _check_fault(payload)
    elif fault is not None and (fault != "number" or _submitted_by(ctx) != "cli"):
        raise _bad()                                            # the fault never reaches the studio
    manifest = _manifest(payload.get("manifest"))
    pid = payload.get("product_id")
    if not isinstance(pid, str) or not derived.PID_RX.match(pid):
        raise _bad()
    try:
        with open(_full(manifest), encoding="utf-8") as fh:
            man_pid = json.load(fh).get("product_id") or os.path.splitext(os.path.basename(_full(manifest)))[0]
    except (OSError, ValueError):
        raise _bad("path")
    if man_pid != pid:
        raise _bad()
    kinds = payload.get("kinds")
    if kinds not in (["views"], ["views", "assembly"]):
        raise _bad()
    skipped = None
    try:
        bom_in = _bom_in(payload)
        bom = _bom_checked(bom_in, pid) if ("assembly" in kinds or bom_in is not None) else None
    except ServiceError:
        if "assembly" not in kinds:
            raise
        bom, kinds, skipped = None, ["views"], "the parts list did not pass its check"   # the views sheet is still made
    base = os.path.join(ROOT, "work", "drawings", pid)
    out_dir = os.path.join(base, ctx.job_id)
    if not _under(out_dir, base):
        raise _bad("path")
    args = {"manifest": manifest, "product_id": pid, "out_dir": out_dir, "kinds": list(kinds), "bom": bom,
            "check_fault": fault if fault == "number" else None, "preview": preview is True,
            "out_json": _out_json(ctx, "draw_overview"), "assembly_skipped": skipped}
    return _run(ctx, "draw_overview", args, TIMEOUT_S["draw_overview"], progress=True)


def _file_in_copy(p):
    """File ops open only a .step / .stp file that exists INSIDE this copy (its
    sessions, models, uploads); the path is checked after resolving `..` and links. Returned unchanged."""
    p = _in_path(p, STEP_ONLY)
    full = os.path.realpath(_full(p))
    if not _under(full, os.path.realpath(ROOT)):
        raise _bad("path")
    return p


def measure_file(ctx, payload):
    """`{path}` -> the kernel's record of every part of the file, as the in-process re-measure makes them."""
    _check_fault(payload)
    if set(payload) - {"path", "check_fault"}:
        raise _bad()
    args = {"path": _file_in_copy(payload.get("path")), "out_json": _out_json(ctx, "measure_file")}
    return _run(ctx, "measure_file", args, TIMEOUT_S["measure_file"])


def _submitted_by(ctx):
    """Who submitted this job (`orch.jobs.submitted_by`). The command a service reads always carries sender "orchestrator"
    (envelope.command_for), so the submitter is read from the job row (one short read-only connection)."""
    from framework.common import pg
    try:
        con = pg.connect()
        try:
            row = con.execute("SELECT submitted_by FROM orch.jobs WHERE job_id = %s", (ctx.job_id,)).fetchone()
        finally:
            con.close()
    except Exception:                                           # noqa: BLE001 - when in doubt it is not the CLI
        return None
    return row[0] if row else None


def _bom_in(payload):
    """The parts list: inline (`bom`) or, for a large design, by reference (`bom_ref`, a file in work/results that
    results.unpack keeps inside that folder) -- a 2,640-part design's list was 441 KB, over the
    queue's 256 KB message limit."""
    if payload.get("bom") is not None:
        return payload.get("bom")
    if isinstance(payload.get("bom_ref"), str):
        return results.unpack({"ref": payload["bom_ref"]})
    return None


def _bom_checked(bom, pid):
    """The parts list the catalogue made (`catalogue.drawing_bom`), checked: it only names which parts to draw and what to
    call them, never a path."""
    if not isinstance(bom, dict) or bom.get("product_id") != pid or not isinstance(bom.get("rows"), list):
        raise _bad()
    rows = bom["rows"]
    if not rows or len(rows) > 2000:
        raise _bad()
    clean = []
    for r in rows:
        if (not isinstance(r, dict) or not isinstance(r.get("ref"), str) or not REF_RX.match(r["ref"])
                or not isinstance(r.get("name"), str) or not r["name"] or len(r["name"]) > 200
                or not isinstance(r.get("bought_in"), bool) or not isinstance(r.get("material"), str)
                or len(r["material"]) > 100 or isinstance(r.get("qty"), bool) or not isinstance(r.get("qty"), int)
                or not isinstance(r.get("item"), int)):
            raise _bad()
        clean.append({"item": r["item"], "ref": r["ref"], "name": r["name"], "label": r.get("label"), "qty": r["qty"],
                      "material": r["material"], "bought_in": r["bought_in"]})
    nm, rev = bom.get("name"), bom.get("revision")
    if not isinstance(nm, str) or not isinstance(rev, str):
        raise _bad()
    return {"product_id": pid, "name": nm, "revision": rev, "rows": clean}


def draw_parts(ctx, payload):
    """`{manifest, product_id, bom, refs?, check_fault?}`. The output folder is derived from the job id
    (work/drawings/<pid>/<job_id>/), never read from the payload. `check_fault` ("number": print the first size 0.02 mm
    wrong, the CLI's proof that the read-back check keeps a bad sheet out) is accepted only from a job the CLI submitted (orch.jobs.submitted_by "cli").

    One load, one sheet per drawable part (ops.draw_parts), the report rewritten after every part. A part still being drawn
    after PART_LIMIT_S is stopped with its worker (a fresh warm one takes its place), listed "it took too long", and the rest
    go on in a second call (one reload); the whole job has TIMEOUT_S["draw_parts"] seconds, and what is left of it when the
    budget runs out is listed "it took too long" too. A real Stop raises Stopped; the sheets already made stay in the report."""
    import json
    from framework.common import drawfp
    from framework.kernel.drawings import partreport as pr
    if set(payload) - {"manifest", "product_id", "bom", "refs", "check_fault"}:
        raise _bad()
    manifest = _manifest(payload.get("manifest"))
    pid = payload.get("product_id")
    if not isinstance(pid, str) or not derived.PID_RX.match(pid):
        raise _bad()
    try:
        with open(_full(manifest), encoding="utf-8") as fh:
            man = json.load(fh)
    except (OSError, ValueError):
        raise _bad("path")
    if (man.get("product_id") or os.path.splitext(os.path.basename(_full(manifest)))[0]) != pid:
        raise _bad()
    bom = _bom_checked(_bom_in(payload), pid)
    refs = payload.get("refs")
    if refs is not None and (not isinstance(refs, list) or len(refs) > 2000
                             or any(not isinstance(r, str) or not REF_RX.match(r) for r in refs)):
        raise _bad()
    fault = payload.get("check_fault")
    if fault is not None and (fault != "number" or _submitted_by(ctx) != "cli"):
        raise _bad()                                            # the fault never reaches the studio
    base = os.path.join(ROOT, "work", "drawings", pid)
    out_dir = os.path.join(base, ctx.job_id)
    if not _under(out_dir, base):
        raise _bad("path")
    rep_path = os.path.join(out_dir, "report.json")
    want = [r for r in pr.drawable(bom) if refs is None or r in refs]
    all_drawable = pr.drawable(bom)
    t_start = time.monotonic()
    budget = TIMEOUT_S["draw_parts"]
    ctx.progress("draw_parts")
    state = {"cur": None, "last": 0.0, "timed_out": None}

    def on_message(msg):
        if isinstance(msg, dict) and msg.get("t") == "start":
            now = time.monotonic()
            state["cur"] = (msg.get("ref"), now)
            if now - state["last"] >= 1.0:                      # at most one progress step a second
                state["last"] = now
                ctx.progress("part_%d_of_%d" % (int(msg.get("i", 0)), int(msg.get("of", 0))))

    def amend(why_by_ref):
        """List parts as failed in the report the worker wrote (or a new one when it died before writing any)."""
        rep = pr.load_or_new(rep_path, pid, drawfp.fingerprint(_full(manifest)), bom["name"], bom["revision"], bom)
        for ref, why in why_by_ref:
            pr.fail(rep, ref, why)
        pr.save(rep_path, pr.in_bom_order(rep, bom))

    def remaining():
        try:
            with open(rep_path, encoding="utf-8") as fh:
                done = pr.done_refs(json.load(fh))
        except (OSError, ValueError):
            done = set()
        return [r for r in want if r not in done]

    pool = _pool(ctx)
    result = None
    while True:
        todo = remaining() if os.path.isfile(rep_path) else list(want)
        if not todo:
            break
        left = budget - (time.monotonic() - t_start)
        if left <= 5:
            amend([(r, pr.WHY_SLOW) for r in todo])
            break
        cancel, finished = threading.Event(), threading.Event()
        state["cur"], state["timed_out"] = None, None

        def watchdog():
            while not finished.wait(0.5):
                if ctx.cancelled.is_set():
                    cancel.set()
                    return
                cur = state["cur"]
                if cur and time.monotonic() - cur[1] > PART_LIMIT_S:
                    state["timed_out"] = cur[0]
                    cancel.set()
                    return

        threading.Thread(target=watchdog, name="draw-parts-watchdog", daemon=True).start()
        args = {"manifest": manifest, "product_id": pid, "out_dir": out_dir, "bom": bom, "refs": todo,
                "check_fault": fault, "out_json": _out_json(ctx, "draw_parts")}
        try:
            r = pool.submit("draw_parts", args, left, cancel=cancel, on_message=on_message)
        finally:
            finished.set()
        if r.get("ok"):
            result = r
            continue                                           # the loop ends when nothing is left to draw
        if r.get("stopped"):
            if ctx.cancelled.is_set():
                raise Stopped()
            slow = state["timed_out"]
            amend([(slow, pr.WHY_SLOW)] if slow else [])
            if not slow:
                raise ServiceError("geometry_worker_lost", "The geometry step stopped unexpectedly.", retryable=True,
                                   detail="draw_parts: stopped without a reason, worker %s" % r.get("pid"))
            continue
        if r.get("timeout"):
            cur = state["cur"]
            amend([(x, pr.WHY_SLOW) for x in ([cur[0]] if cur else []) + [t for t in todo if not cur or t != cur[0]]])
            break
        if r.get("lost"):
            raise ServiceError("geometry_worker_lost", "The geometry step stopped unexpectedly.", retryable=True,
                               detail="draw_parts: the worker %s was lost" % r.get("pid"))
        print("geometry draw_parts failed (job %s): %s | %s" % (ctx.job_id, r.get("err", ""), (r.get("tb") or "")[-800:]),
              flush=True)
        if r.get("kind") == "nodump":
            raise ServiceError("no_dump", "This design needs the first engine's pass first.", detail=r.get("err", ""))
        raise ServiceError("geometry_failed", "The geometry step could not be done.",
                           detail="draw_parts: %s | %s" % (r.get("err", ""), (r.get("tb") or "")[-800:]))
    ctx.check()
    try:
        with open(rep_path, encoding="utf-8") as fh:
            rep = json.load(fh)
    except (OSError, ValueError):
        raise ServiceError("geometry_failed", "The geometry step could not be done.", detail="draw_parts: no report")
    out = {"result_file": os.path.relpath(rep_path, ROOT).replace(os.sep, "/"),
           "job_dir": os.path.relpath(out_dir, ROOT).replace(os.sep, "/"), "fingerprint": rep["fingerprint"],
           "sheets": len(rep["sheets"]), "failed": len(rep["failed"]), "wrong": 0}
    return results.pack(ctx.job_id, dict(out, worker_pid=(result or {}).get("pid"), sec=(result or {}).get("sec")))


def _part_payload(payload, extra=()):
    """`{part_id, sources: [[path, object], ...], key}` checked: a catalogue part id, 1..3 sources that exist (a STEP
    file, or a shape file only from the dump cache or the split cache), a key that is this part's for these sources
    (recomputed here, so a payload cannot name another part's file). -> (part_id, sources, key)."""
    from framework.common import partview as cpv
    if set(payload) - ({"part_id", "sources", "key", "check_fault"} | set(extra)):
        raise _bad()
    part_id = payload.get("part_id")
    if not isinstance(part_id, str) or cpv.split_part(part_id) is None:
        raise _bad()
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources or len(sources) > 3:
        raise _bad()
    clean = []
    for s in sources:
        if not isinstance(s, list) or len(s) != 2 or not isinstance(s[1], str) or not s[1] or len(s[1]) > 200:
            raise _bad()
        p = _in_path(s[0], STEP_ONLY + (".brep",))
        if p.lower().endswith(".brep") and not (_under(_full(p), os.path.join(ROOT, "work", "engines", "brep"))
                                                or cpv.in_split(_full(p))):
            raise _bad("path")
        clean.append([p, s[1]])
    return part_id, clean


def part_view(ctx, payload):
    """One part's own 3D view. Reused without a worker when the file made with this key is there."""
    from framework.common import partview as cpv
    _check_fault(payload)
    part_id, sources = _part_payload(payload)
    key = cpv.key("view", [(_full(p), o) for p, o in sources])
    if payload.get("key") is not None and payload.get("key") != key:
        raise _bad()
    glb, facts = cpv.path(part_id, key, "glb"), cpv.path(part_id, key, "json")
    if os.path.isfile(facts) and os.path.isfile(glb):
        ctx.progress("reused")
        return results.pack(ctx.job_id, {"found": True, "result_file": derived.rel(facts), "key": key, "reused": True})
    args = {"part_id": part_id, "sources": sources, "key": key, "out_glb": glb, "out_facts": facts,
            "out_json": _out_json(ctx, "part_view")}
    res = results.unpack(_run(ctx, "part_view", args, TIMEOUT_S["part_view"]))
    if not res.get("found"):
        raise ServiceError("no_shape", cpv.NO_SHAPE)
    res["key"] = key
    return results.pack(ctx.job_id, res)


def part_step(ctx, payload):
    """The part alone as STEP, named `name` (its label). Reused when the file made with this key is there."""
    from framework.common import partview as cpv
    _check_fault(payload)
    part_id, sources = _part_payload(payload, extra=("name",))
    name = payload.get("name")
    if not isinstance(name, str) or not name or len(name) > 200:
        raise _bad()
    key = cpv.key("step", [(_full(p), o) for p, o in sources])
    if payload.get("key") is not None and payload.get("key") != key:
        raise _bad()
    out = cpv.path(part_id, key, "step")
    if os.path.isfile(out):
        ctx.progress("reused")
        return results.pack(ctx.job_id, {"found": True, "path": derived.rel(out), "key": key, "reused": True})
    args = {"part_id": part_id, "sources": sources, "key": key, "name": name, "out_step": out,
            "out_json": _out_json(ctx, "part_step")}
    res = results.unpack(_run(ctx, "part_step", args, TIMEOUT_S["part_step"]))
    if not res.get("found"):
        raise ServiceError("no_shape", cpv.NO_SHAPE)
    res.update(key=key, path=derived.rel(out))
    return results.pack(ctx.job_id, res)


def tight_extent(ctx, payload):
    """Parts' exact extents (and solids) for the catalogue's part records.
    payload {"targets": [[part_id, [[path, object], ...]], ...], "solids": bool}: each part's sources checked as
    geometry.part_view checks them (1..3 sources, a STEP file or a shape file from the dump or split cache)."""
    _check_fault(payload)
    if set(payload) - {"targets", "solids", "check_fault"}:
        raise _bad()
    targets = payload.get("targets")
    if not isinstance(targets, list) or not targets or len(targets) > MAX_EXTENT_TARGETS:
        raise _bad()
    clean = []
    for t in targets:
        if not isinstance(t, list) or len(t) != 2:
            raise _bad()
        part_id, sources = _part_payload({"part_id": t[0], "sources": t[1]})
        clean.append([part_id, sources])
    args = {"targets": clean, "solids": bool(payload.get("solids", True)), "out_json": _out_json(ctx, "tight_extent")}
    return _run(ctx, "tight_extent", args, TIMEOUT_S["tight_extent"])


def selftest(ctx, payload):
    kind = payload.get("kind")
    if kind not in ("sleep", "spin"):
        raise _bad()
    seconds = _num(payload.get("seconds"), 0, MAX_SECONDS)
    timeout_s = _num(payload.get("timeout_s", MAX_SECONDS), 1, MAX_SECONDS)
    return _run(ctx, "selftest", {"kind": kind, "seconds": seconds}, timeout_s)


# ---- the second step of the product.erase recipe ---------------------------------------------------------------------------
_LOCKS = {}
_GUARD = threading.Lock()


def _lock_for(pid):
    with _GUARD:
        return _LOCKS.setdefault(pid, threading.Lock())


def _rm_in(folder, name):
    """Remove folder/name if it is a file directly inside folder (today's rule). -> the removed name or None."""
    p = os.path.join(folder, name)
    if os.path.dirname(os.path.realpath(p)) == os.path.realpath(folder) and os.path.isfile(p):
        try:
            os.remove(p)
        except FileNotFoundError:      # removed meanwhile (another replica, a redelivery): nothing to do
            return None
        return name
    return None


def drop_derived(ctx, payload):
    """Remove what was made for a product: its 3D views and exports and the kernel's own mesh and export. Held under a
    per-product lock, so two removals of one design never interleave. Needs no manifest (the catalogue's erase has
    already removed it). A redelivery removes nothing and returns files 0."""
    pid = str(payload.get("product_id") or "")
    if not derived.PID_RX.match(pid):
        raise ServiceError("bad_id", "This design name is not valid.")
    ctx.progress("drop_derived")
    kmeshes, ksteps = os.path.join(derived.KERNEL_OUT, "meshes"), os.path.join(derived.KERNEL_OUT, "step")
    removed = []
    with _lock_for(pid):
        for folder, names in ((derived.MESHES, [pid + x for x in (".glb", ".glb.log", ".obj", ".obj.log")]),
                              (kmeshes, [pid + x for x in (".glb", ".glb.log", ".obj", ".obj.log")]),
                              (derived.STEPS, [pid + ".step"]),
                              (ksteps, [pid + ".step"])):
            removed += [n for n in (_rm_in(folder, n) for n in names) if n]
    return results.pack(ctx.job_id, {"files": len(removed), "names": removed})


HANDLERS = {"geometry.measure": measure, "geometry.read_step": read_step, "geometry.mesh": mesh,
            "geometry.step_out": step_out, "geometry.signature": signature,
            "geometry.topology": topology, "geometry.draw_overview": draw_overview, "geometry.draw_parts": draw_parts, "geometry.selftest": selftest,
            "geometry.measure_file": measure_file,
            "geometry.part_view": part_view, "geometry.part_step": part_step, "geometry.tight_extent": tight_extent,
            "geometry.drop_derived": drop_derived}
