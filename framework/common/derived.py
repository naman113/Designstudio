"""The paths of derived files (3D view, download, motion cache, shadow catalogue) and the rule that says when one is stale.

The gateway reads these paths and asks `stale()` to decide whether a job is needed at all; the engines service writes the
files and asks the same `stale()` to decide whether to make one. One function, so the two never disagree.

Copy-safe: every path is a pure function of the product id, so several replicas of the engines
service write the same file for the same product, and the content is the same.
"""
import json
import os
import re
import uuid

from framework.common import config

ROOT = config.ROOT
PRODUCTS = os.path.join(ROOT, "framework", "products")
MESHES = os.path.join(ROOT, "framework", "web", "static", "meshes")
STEPS = os.path.join(ROOT, "work", "step")
MOTION = os.path.join(ROOT, "work", "motion")
KERNEL_OUT = os.path.join(ROOT, "work", "engines", "kernel")
# a design's videos (the render service is their only writer; the gateway only reads them)
ANIMATIONS = os.path.join(ROOT, "framework", "web", "static", "files", "animations")

# a product id as app.py accepts it (framework/web/app.py PID_RX)
PID_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,119}$")


def mode():
    """The engine mode: always "kernel" in this replica (no second engine). No HTTP probe: the studio imports this at start-up
    (app.py MESH_EXT), so it must answer at once."""
    return "kernel"


def mesh_ext():
    """The viewer mesh's file type: the kernel writes the viewer mesh as GLB (partview.write_design), one node per part."""
    return ".glb"


def manifest(pid):
    """The product's manifest path, or None."""
    p = os.path.join(PRODUCTS, pid + ".json")
    return p if os.path.exists(p) else None


def mesh_path(pid, ext=None):
    """The viewer mesh of a product (`ext` defaults to mesh_ext())."""
    return os.path.join(MESHES, pid + (ext or mesh_ext()))


def step_path(pid):
    return os.path.join(STEPS, pid + ".step")


def kernel_db():
    """The shadow catalogue: owned by the catalogue, engines only reads it."""
    return os.environ.get("CAD_KERNEL_DB") or os.path.join(ROOT, "work", "engines", "kernel_catalog.db")


def stale(out_path, pid):
    """True when the manifest or any part file is newer than the exported file (app.py's `_stale`, moved here unchanged).
    A product without a manifest is never stale; a missing file is."""
    glb = out_path
    man = manifest(pid)
    if not man:
        return False
    if not os.path.exists(glb):
        return True
    t = os.path.getmtime(glb)
    with open(man, encoding="utf-8") as fh:
        m = json.load(fh)
    base = os.path.dirname(man)
    files = [f for e in m["parts"] for f in (e["file"], e.get("fc_file")) if f]
    deps = [man] + [f if os.path.isabs(f) else os.path.join(base, f) for f in files]
    # a 390-part product lists 3 distinct files 781 times; through a container's bind mount every stat costs about
    # 0.9 ms. Each path is looked at once (a missing file is not newer, as before).
    for d in dict.fromkeys(deps):
        try:
            if os.stat(d).st_mtime > t:
                return True
        except OSError:
            continue
    return False


def rel(path):
    """A path relative to the project root, with forward slashes (what travels in a job result)."""
    return os.path.relpath(path, ROOT).replace("\\", "/")


def anim_index(pid):
    """The path of a design's video index (`<ANIMATIONS>/<pid>/index.json`). The file may not exist (no video yet). The
    page and the gateway read it directly, so a stopped video service never hides the list."""
    return os.path.join(ANIMATIONS, pid, "index.json")


def _caption_kinds(acts):
    """The fact kinds of a spec's caption steps (ratio, speed, stroke, swing, opening, size)."""
    return {str(a.get("fact") or "").split(":", 1)[0] for a in acts or []
            if isinstance(a, dict) and a.get("do") == "caption" and a.get("fact")}


def anim_has_mechanism(spec):
    """True when a spec needs the movement data: a `mechanism` action (the design's real movement), or a caption
    whose fact is the ratio, a speed, a stroke or a swing. The movement data must then be fresh before its video is made
    (`needs_motion` of the animation recipes)."""
    acts = spec.get("actions") if isinstance(spec, dict) else None
    return (any(isinstance(a, dict) and a.get("do") == "mechanism" for a in (acts or []))
            or bool(_caption_kinds(acts) & {"ratio", "speed", "stroke", "swing"}))


def anim_has_flow(spec):
    """True when a spec needs the openings: a `flow` step, or a caption whose fact is an opening's
    size. The design's openings (geometry.openings, the file work/openings/<pid>.json) must then be fresh before its video
    is made (`needs_openings` of the animation recipes)."""
    acts = spec.get("actions") if isinstance(spec, dict) else None
    return (any(isinstance(a, dict) and a.get("do") == "flow" for a in (acts or []))
            or "opening" in _caption_kinds(acts))


def anim_has_fits(spec):
    """True when a spec needs the design's fit-and-clearance numbers: a `tight` step, or a caption whose
    fact is a gap or an overlap. They (geometry.fits, the file work/fits/<pid>.json) must then be fresh before its video is
    made (`needs_fits` of the animation recipes)."""
    acts = spec.get("actions") if isinstance(spec, dict) else None
    return (any(isinstance(a, dict) and a.get("do") == "tight" for a in (acts or []))
            or bool(_caption_kinds(acts) & {"gap", "overlap"}))


_ANIM_NS = uuid.UUID("b3f0c1de-5a42-4e6b-9a11-7d2f5e0a4c21")      # namespace of the video jobs' idempotency ids


def anim_job_id(con, pid, n, quality):
    """The one job id a press of "Make final" / "Photo-real" for version n of a design stands for, as
    `(job_id, is_open)`. An open job of that (design, version, quality) is returned as it is (`is_open` True). Otherwise the
    id is derived from the key and from the newest FINISHED job of that key (none = the first run), so every press made
    before the orchestrator has written the job gets the SAME id, however far apart the presses are, and the orchestrator's
    `ON CONFLICT (job_id) DO NOTHING` turns a repeated submit into no second job, whatever process sent it. Once the job has
    ended (done, failed or stopped) the next press chains from it and gets a new id: a later re-run is never blocked.
    The caller holds the advisory lock of the key and submits with this id."""
    from framework.common import jobs
    rows = con.execute(
        "SELECT job_id, state FROM orch.jobs WHERE type = %s AND payload->>'product_id' = %s AND payload->>'version' = %s "
        "ORDER BY created_at DESC", ("animation." + quality, pid, str(n))).fetchall()
    for job_id, state in rows:
        if state not in jobs.FINAL:
            return str(job_id), True
    last = str(rows[0][0]) if rows else ""
    return str(uuid.uuid5(_ANIM_NS, "anim:%s:%d:%s:%s" % (pid, n, quality, last))), False


_FITS_NS = uuid.UUID("5c0e7a52-91d4-4f3b-8a67-2e6d4b9f1c08")      # namespace of the fit-and-clearance jobs' ids


def fits_job_id(con, pid, fingerprint, min_limit_s=0):
    """The one job id a fit-and-clearance check of this design (this geometry: `fingerprint`) stands for, as
    `(job_id, state)`: `state` is None when no such job exists yet (submit it with this id), an open state when it is
    running or waiting (attach to it), or "timed_out" (it ran past its limit: do not start another). A job that failed,
    was stopped or succeeded without leaving a fresh file is followed by the next id of the chain, so a rerun is never
    blocked; a changed design has another fingerprint, hence another chain. A job that timed out under a shorter limit than
    `min_limit_s` (the limit now in force) is followed by the next id like a failed one, so the check is tried again."""
    from framework.common import jobs
    last = ""
    while True:
        jid = str(uuid.uuid5(_FITS_NS, "fits:%s:%s:%s" % (pid, fingerprint, last)))
        row = con.execute("SELECT state, error_code, limit_s FROM orch.jobs WHERE job_id = %s", (jid,)).fetchone()
        if row is None:
            return jid, None
        if row[0] == "timed_out" or row[1] == "geometry_timeout":      # the sweeper's timeout, or the worker's own limit
            if (row[2] or 0) >= min_limit_s:
                return jid, "timed_out"
            last = jid                                                 # it ran under an older, shorter limit: try again
            continue
        if row[0] not in jobs.FINAL:
            return jid, row[0]
        last = jid
