"""The catalogue service's upload jobs: `catalogue.add_prepare` and `catalogue.add` (`catalogue.load`, the third
step of the orchestrator's `upload.build` recipe, is framework/catalog/svc_load.py).

They are the catalogue's steps of the orchestrator's `upload.build` recipe; the geometry service reads the STEP files
with the kernel (`geometry.read_step`) in between. The logic is
framework/catalog/add_step.py's own step functions (prepare, entries_kernel, finish), the ones
`add_step.add()` calls in-process, so the recipe and the command line share one implementation.

Payloads (project-relative paths with forward slashes, never absolute ones)
  catalogue.add_prepare {files: [path], name?, into?, ref?, move: bool, single_part?: bool, material?}
        single_part ("Add part to catalogue"): exactly one file, no `into`; material only with it, one of the
        materials.json names.
        -> add_step.to_wire(state): {ok, pid, title, into, man (the manifest as it will be written), folder, sources:
           [{path, stem, fref}]}.  Copies (or, for files in models/uploads/_incoming, moves) each file into
           models/uploads/<pid>/ with today's naming. Errors: no_product, bad_path, bad_request.
  catalogue.add {prepare: <that result>, k_job?}
        reads the read result of the kernel (`k_job`: work/results/<job>.read_step.json) by
        job id, builds the manifest entries with add_step.entries_kernel, and writes the
        manifest (add_step.finish). -> {product_id, name, manifest, added, messages: [[label, state]]}.
        Errors: no_geometry, not_single_part (a single_part file whose solids, counting qty, are not exactly one:
        its upload folder is removed).

Safety: an input is an existing `.step` / `.stp` file under the project whose first 200 bytes start
with ISO-10303-21; at most 20 files; `move` only for files in models/uploads/_incoming; the manifest and every stored file
land only in framework/products and models/uploads/<pid>.
"""
import json
import os
import re

from framework.common import derived, results
from framework.common.consumer import ServiceError

ROOT = derived.ROOT
INBOX = os.path.join(ROOT, "models", "uploads", "_incoming")
MAX_FILES = 20
_UUID_RX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_BAD = "This request could not be read."


def _bad(why):
    return ServiceError("bad_request", _BAD, detail="upload payload: %s" % why)


def _under(full, folder):
    full, folder = os.path.normcase(os.path.realpath(full)), os.path.normcase(os.path.realpath(folder))
    return full.startswith(folder + os.sep)


def _abs(rel):
    return rel if os.path.isabs(rel) else os.path.join(ROOT, *rel.split("/"))


def _input(p):
    """An existing STEP file under the project; -> its absolute path."""
    if not isinstance(p, str) or not p or len(p) > 1000 or "\x00" in p:
        raise _bad("path")
    full = _abs(p)
    if not re.search(r"\.(step|stp)$", full, re.I) or not os.path.isfile(full) or not _under(full, ROOT):
        raise ServiceError("bad_path", _BAD, detail="upload payload: not a STEP file under the project: %s" % p[:200])
    with open(full, "rb") as fh:
        if not fh.read(200).lstrip().upper().startswith(b"ISO-10303-21"):
            raise ServiceError("bad_path", _BAD, detail="upload payload: not a STEP file (header): %s" % p[:200])
    return full


def _opt_str(payload, key, limit):
    v = payload.get(key)
    if v is not None and not (isinstance(v, str) and len(v) <= limit and "\x00" not in v):
        raise _bad(key)
    return v


def add_prepare(ctx, payload):
    import add_step
    files = payload.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise _bad("files")
    move = payload.get("move", False)
    if not isinstance(move, bool):
        raise _bad("move")
    name, into, ref = _opt_str(payload, "name", 200), _opt_str(payload, "into", 120), _opt_str(payload, "ref", 60)
    if into is not None and not derived.PID_RX.match(into):
        raise _bad("into")
    paths = [_input(f) for f in files]
    if move and not all(_under(p, INBOX) for p in paths):
        raise ServiceError("bad_path", _BAD, detail="upload payload: only files in the upload inbox are moved")
    single_part = payload.get("single_part", False)
    if not isinstance(single_part, bool) or (single_part and (len(files) != 1 or into is not None)):
        raise _bad("single_part")
    material = _opt_str(payload, "material", 60)
    if material is not None:
        from framework.catalog.pg import partrecord
        if not single_part or material not in [m["material"] for m in partrecord.materials()["materials"]]:
            raise _bad("material")
    build = payload.get("build")
    if build is not None and not (isinstance(build, dict) and len(build) <= 4 and all(
            isinstance(v, str) and len(v) <= 300 for v in build.values())):
        raise _bad("build")
    ctx.progress("prepare")
    state = add_step.prepare(paths, name, into, ref, move, single_part, material)
    if not state["ok"]:
        raise ServiceError("no_product", "There is no design with that name.", detail=state["error"])
    return results.pack(ctx.job_id, add_step.to_wire(state))


def _wire(prepare):
    """The prepare result back into a state, checked: a product id, its own upload folder, sources inside it."""
    if not isinstance(prepare, dict) or not isinstance(prepare.get("man"), dict) or not isinstance(prepare.get("sources"), list):
        raise _bad("prepare")
    pid = prepare.get("pid")
    if not isinstance(pid, str) or not derived.PID_RX.match(pid) or prepare.get("man", {}).get("product_id", pid) != pid:
        raise _bad("pid")
    if prepare.get("folder") != "models/uploads/" + pid:
        raise _bad("folder")
    for s in prepare["sources"]:
        if not isinstance(s, dict) or not isinstance(s.get("path"), str) or not s["path"].startswith("models/uploads/%s/" % pid) \
                or ".." in s["path"].split("/") or not isinstance(s.get("stem"), str) or not isinstance(s.get("fref"), str):
            raise _bad("sources")
    import add_step
    return add_step.from_wire(prepare)


def _read_file(job_id):
    """work/results/<job>.read_step.json of one read step (a job id, never a path); None when the step wrote none."""
    if job_id is None:
        return None
    if not isinstance(job_id, str) or not _UUID_RX.match(job_id):
        raise _bad("job id")
    path = os.path.join(results.DIR, "%s.read_step.json" % job_id)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def add(ctx, payload):
    import add_step
    prepare = payload.get("prepare")
    if prepare is None and isinstance(payload.get("prepare_ref"), str):
        prepare = results.unpack({"ref": payload["prepare_ref"]})     # unpack keeps the path inside work/results
    state = _wire(prepare)
    k = _read_file(payload.get("k_job"))
    n = len(state["sources"])
    ctx.progress("add")
    added, messages, k_files = 0, [], []
    for i in range(n):
        k_file = ((k or {}).get("files") or [None] * n)[i] if k else None
        k_files.append(k_file)
        got, labels = add_step.entries_kernel(state, i, k_file)
        added += got
        messages += [list(x) for x in labels]
        if state.get("rejected"):
            add_step.discard(state)
            raise ServiceError("not_single_part", state["rejected"])
    if state.get("single_part") and added:
        messages.append([add_step.catalogue_material(state, 0, k_files[0])[1], "done"])
    fin = add_step.finish(state, added, n)
    if not fin["ok"]:
        add_step.discard(state)                   # no geometry: the stored file goes too (no-op for `into`)
        raise ServiceError("no_geometry", "None of the files contained solid geometry.")
    return results.pack(ctx.job_id, {"product_id": state["pid"], "name": state["title"], "added": added,
                                     "manifest": derived.rel(fin["manifest"]), "messages": messages})


HANDLERS = {"catalogue.add_prepare": add_prepare, "catalogue.add": add}
