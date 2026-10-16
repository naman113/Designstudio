"""The recipe engine's registry. A service command is a one-step recipe run on the job row itself;
`recipe.<name>` jobs come from RECIPES (multi-step). The test recipe is `recipe.echo_pair`; the
first real one is `product.erase` (`catalogue.erase`, then `geometry.drop_derived`).
"""
import os
from dataclasses import dataclass
from typing import Callable

from framework.common import policy


class UnknownType(Exception):
    pass


@dataclass(frozen=True)
class Step:
    name: str                                   # "echo"
    command: str                                # "catalogue.echo" (service.verb; target = text before the dot)
    answers: bool = True                        # this step's result answers the job (a shadow step is False)
    on_fail: str = "fail"                       # "fail" | "log" (a failed "log" step never fails the job)
    args: Callable[[dict, dict], dict] = lambda job_payload, results: job_payload   # None = skip this step
    fallback: bool = False                      # stands in for the answering step when that one failed


@dataclass(frozen=True)
class Recipe:
    type: str
    steps: tuple                                # of Step
    # the parent stays `queued` while an older open job has the same payload value under this key
    wait_for: str = None
    wait_limit_s: int = 120                     # after this long the parent fails `busy`
    # the parent's result IS its last answering step's result (like a dual recipe), while `steps` keeps
    # every step's record. Without it the parent's result is the steps record.
    answer_with_last: bool = False
    # the parent answers as soon as its answering step is done, but its result stays the steps record
    # ({"steps": {name: result}}); later steps trail. `product.erase` keeps the shape the studio's route reads
    # (`res["steps"]["erase"]`) while its videos go in a trailing step.
    answer_record: bool = False


RECIPES = {}                                    # type -> Recipe (multi-step: recipe.echo_pair, product.erase)


def is_factory(job_type):
    """`RECIPES[type]` may be a function `fn(mode) -> Recipe` (the `dual.<op>` recipes, by engine mode)."""
    return callable(RECIPES.get(job_type))


def resolve(job_type, mode=None):
    """The recipe for a job type (`mode`: the engine mode stored in a factory recipe's parent). Raises UnknownType when the
    type has no recipe and no policy."""
    if job_type in RECIPES:
        r = RECIPES[job_type]
        return r(mode or "kernel") if callable(r) else r
    if policy.policy(job_type) is None:
        raise UnknownType(job_type)
    return Recipe(job_type, (Step(job_type.split(".", 1)[1], job_type),))


def _pick(p, *keys):
    return {k: p[k] for k in keys if k in p}


# the test recipe that proves the runner before anything destructive uses it
RECIPES["recipe.echo_pair"] = Recipe("recipe.echo_pair", (
    Step("first", "catalogue.echo",
         args=lambda p, r: {"text": str(p.get("text", "pair"))[:200], "sleep_s": p.get("sleep_s", 0)}),
    Step("second", "catalogue.echo",
         args=lambda p, r: dict({"text": ("after " + r["first"]["text"])[:200]},
                                **({"fail": "error"} if p.get("fail_second") else {}))),
))

# the erase of a design across two services. `_pause_after_rows_s` is a CLI-only knob (the crash check); the studio
# never sends it.
RECIPES["product.erase"] = Recipe("product.erase", (
    Step("erase", "catalogue.erase", args=lambda p, r: _pick(p, "product_id", "_pause_after_rows_s")),
    Step("drop_derived", "geometry.drop_derived", args=lambda p, r: _pick(p, "product_id")),
), wait_for="product_id", wait_limit_s=120, answer_record=True)


# ---- the dual.<op> recipes (kernel only) ------------------------------------------------------------------------------------
# `dual.<op>` takes the engine mode the orchestrator stored in the parent's payload (`engine_mode`, read once at submit:
# engine_mode.py); the replica is kernel-only, so each recipe is its kernel step.


def _pid(p):
    return p.get("product_id")


def _manifest_rel(p):
    from framework.common import derived
    man = derived.manifest(str(_pid(p) or "")) if derived.PID_RX.match(str(_pid(p) or "")) else None
    return derived.rel(man) if man else None


def _sid(r, name):
    return ((r.get("__steps__") or {}).get(name) or {}).get("job_id")


def _dual_mesh(mode):
    from framework.common import derived
    return Recipe("dual.mesh", (Step("primary", "geometry.mesh", args=lambda p, r: {
        "manifest": _manifest_rel(p), "out": derived.rel(derived.mesh_path(str(_pid(p)), derived.mesh_ext())),
        "header": str(_pid(p)), "side": "kernel"}),))


def _dual_step_out(mode):
    from framework.common import derived
    step_rel = lambda p: derived.rel(derived.step_path(str(_pid(p))))
    return Recipe("dual.step_out", (Step("primary", "geometry.step_out", args=lambda p, r: {
        "manifest": _manifest_rel(p), "out": step_rel(p), "root_name": str(_pid(p)), "readback": []}),))


def _dual_measure(mode):
    return Recipe("dual.measure", (Step("primary", "geometry.measure", args=lambda p, r: {"path": p.get("path"), "side": "kernel"}),))


RECIPES["dual.mesh"] = _dual_mesh
RECIPES["dual.step_out"] = _dual_step_out
RECIPES["dual.measure"] = _dual_measure


# ---- a product load as steps ------------------------------------------------------------------------------------------------
# `product.load {manifest, product_id}`: the kernel's load fills the catalogue and answers. The load is one `catalogue.load` job
# that measures through child jobs (geometry.measure): see framework/catalog/svc_load.py.
def _product_load(mode):
    return Recipe("product.load", (Step("load", "catalogue.load", args=lambda p, r: {
        "manifest": p.get("manifest"), "product_id": p.get("product_id"), "side": "kernel", "target": "primary"}),),
        wait_for="product_id", wait_limit_s=18000)


RECIPES["product.load"] = _product_load


# ---- an upload as a recipe ----------------------------------------------------------------------------------------------
# upload.build {files, name?, into?, ref?, move}: the catalogue prepares the product's folder, the kernel (geometry) reads every
# file, the catalogue writes the manifest and measures, then the 3D view. The parent answers when "load" succeeds (its result
# carries the upload's answer: the keys of `carry`); the 3D view trails. `upload.build` into an existing design (`into`) waits for
# an older upload into the same design.
def _upload_prep(r):
    return r.get("prepare") or {}


def _prepare_arg(r):
    """The prepare result for the add step: inline when small, else by reference to the file results.pack already wrote
    for it (a large design's manifest with every part's build script was 382 KB, over the
    queue's 256 KB message limit)."""
    import json as _json
    from framework.common import results
    prep = _upload_prep(r)
    jid = _sid(r, "prepare")
    if jid and len(_json.dumps(prep)) > results.INLINE_MAX:
        return {"prepare_ref": "work/results/%s.json" % jid}
    return {"prepare": prep}


def _upload_pid(r):
    return _upload_prep(r).get("pid")


def _upload_files(r):
    return [s["path"] for s in _upload_prep(r).get("sources") or []]


def _upload_manifest(r):
    from framework.common import derived
    return derived.rel(os.path.join(derived.PRODUCTS, "%s.json" % _upload_pid(r)))


def _loaded(r):
    """The 3D view and its comparisons are made only for a design the catalogue measured (a failed load skips them)."""
    return ((r.get("__steps__") or {}).get("load") or {}).get("state") == "succeeded"


def _upload_prepare_args(p, r):
    return _pick(p, "files", "name", "into", "ref", "move", "single_part", "material")


def _upload_build(mode):
    from framework.common import derived
    prepare = Step("prepare", "catalogue.add_prepare", args=_upload_prepare_args)
    kread = Step("kread", "geometry.read_step", args=lambda p, r: {"files": _upload_files(r), "side": "kernel"})
    add = Step("add", "catalogue.add", args=lambda p, r: {**_prepare_arg(r), "fc_job": None, "k_job": _sid(r, "kread")})
    load = Step("load", "catalogue.load", args=lambda p, r: {
        "manifest": r["add"]["manifest"], "product_id": r["add"]["product_id"],
        "side": "kernel", "target": "primary",
        "carry": {"name": r["add"]["name"], "added": r["add"]["added"], "messages": r["add"]["messages"]}})
    kmesh = Step("kmesh", "geometry.mesh", answers=False, on_fail="log", args=lambda p, r: None if not _loaded(r) else {
        "manifest": _upload_manifest(r), "out": derived.rel(derived.mesh_path(str(_upload_pid(r)), derived.mesh_ext())),
        "header": str(_upload_pid(r)), "side": "kernel"})
    return Recipe("upload.build", (prepare, kread, add, load, kmesh), wait_for="into", wait_limit_s=18000)


RECIPES["upload.build"] = _upload_build


# FOLLOW_ON[type](result) -> (job type, payload) or None: a job the orchestrator runs as a child of a turn job before it
# answers. No job type uses it here, so it stays empty.
FOLLOW_ON = {}


# ---- the drawings of a design ------------------------------------------------------------------------------------------
# drawings.make {product_id, reason: "button" | "changed" | "backfill"}: the geometry service draws the views
# sheet (checked there), then the catalogue re-checks and publishes it (the only writer of the studio's drawing files). It
# waits for an older open job of the same design. Further steps make the parts list and the part drawings, and the
# overview step adds the assembly drawing (same load as the views sheet) when the design has more than one part.
# Every drawing step but the last logs its failure and the recipe goes on, so a design whose views sheet could not be made
# still gets its part drawings, and the job fails only when nothing at all was made (the last step).
def _bom_arg(r):
    """The parts list for a drawing step: inline when small, else by reference to the file results.pack wrote for the
    bom step (a parts list of a few thousand parts can be over the 256 KB message limit)."""
    import json as _json
    from framework.common import results
    bom, jid = r.get("bom"), _sid(r, "bom")
    if jid and len(_json.dumps(bom)) > results.INLINE_MAX:
        return {"bom_ref": "work/results/%s.json" % jid}
    return {"bom": bom}


def _overview_args(p, r):
    """The views sheet, and the assembly drawing in the same load when the design has more than one part
    (a single part's own drawing is its drawing). The assembly needs the parts list the first step made."""
    man = _manifest_rel(p)
    if man is None:
        return None
    bom = r.get("bom")
    multi = isinstance(bom, dict) and len(bom.get("rows") or []) > 1
    out = {"manifest": man, "product_id": _pid(p), "kinds": ["views", "assembly"] if multi else ["views"]}
    if multi:
        out.update(_bom_arg(r))
    return out


def _publish_overview_args(p, r):
    """Publishes the views sheet, and the assembly drawing when the geometry service kept it (its answer counts two sheets);
    an assembly that failed its read-back is listed on the set, the views sheet is handed over alone."""
    res = r.get("overview") or {}
    ov = (r.get("__steps__") or {}).get("overview") or {}
    if ov.get("state") in ("failed", "timed_out") and ov.get("job_id") and _pid(p):
        # the overview job was cut off (a time limit, a lost worker) but writes the views sheet and its own
        # report before it tries the assembly: the catalogue hands over what is there (nothing there: the step logs a refusal)
        return {"product_id": _pid(p), "job_dir": "work/drawings/%s/%s" % (_pid(p), ov["job_id"]), "fingerprint": None,
                "kinds": ["views"], "job_id": ov["job_id"], "salvage": True}
    if ov.get("state") != "succeeded" or not res.get("job_dir"):
        return None
    kinds = ["views", "assembly"] if int(res.get("sheets") or 1) >= 2 else ["views"]
    return {"product_id": _pid(p), "job_dir": res["job_dir"], "fingerprint": res["fingerprint"], "kinds": kinds,
            "job_id": _sid(r, "overview")}


def _bom_args(p, r):
    return {"product_id": _pid(p)} if _manifest_rel(p) is not None else None


def _parts_args(p, r):
    man, bom = _manifest_rel(p), r.get("bom")
    if man is None or not isinstance(bom, dict):
        return None
    return {"manifest": man, "product_id": _pid(p), **_bom_arg(r)}


def _publish_parts_args(p, r):
    """Never skipped: the hand-over step is the one that fails the job when nothing at all was made. With no part sheets
    (the parts step failed, or drew none) `job_dir` is null and the step only records the failure on the views set."""
    steps = r.get("__steps__") or {}
    res = r.get("parts") or {}
    out = {"product_id": _pid(p), "kinds": ["parts"],
           "overview_published": (steps.get("publish_overview") or {}).get("state") == "succeeded"}
    if (steps.get("parts") or {}).get("state") == "succeeded" and res.get("job_dir"):
        out.update(job_dir=res["job_dir"], fingerprint=res["fingerprint"], job_id=_sid(r, "parts"))
    else:
        out.update(job_dir=None, fingerprint=(r.get("overview") or {}).get("fingerprint"))
    return out


RECIPES["drawings.make"] = Recipe("drawings.make", (
    Step("bom", "catalogue.drawing_bom", args=_bom_args),
    Step("overview", "geometry.draw_overview", on_fail="log", args=_overview_args),
    Step("publish_overview", "catalogue.publish_drawings", on_fail="log", args=_publish_overview_args),
    Step("parts", "geometry.draw_parts", on_fail="log", args=_parts_args),
    Step("publish_parts", "catalogue.publish_drawings", args=_publish_parts_args),
), wait_for="product_id", wait_limit_s=18000)


# ---- drawings follow the designs by themselves -------------------------------------------------------------------------
# THEN[type](payload, result) -> (job type, payload) or None. When a product.load or upload.build parent
# SUCCEEDS (it answers at its "load" step), the orchestrator queues `drawings.make {product_id, reason: "changed", after: <that
# job>}` as a TOP-LEVEL job in the same transaction (dispatcher `_then`). It is not FOLLOW_ON: a FOLLOW_ON child is one its
# parent waits for, while a set of drawings takes minutes and must not hold the upload or the 3D view open (that is also
# why it is not a trailing step inside these recipes). The drawings job waits
# (wait_for product_id) for older open jobs of the design only; the trigger's own trailing steps are created after it and are
# not waited for (the drawings read nothing they write).
def _drawings_after(payload, result):
    from framework.common import derived
    pid = (result or {}).get("product_id") or (payload or {}).get("product_id")
    if isinstance(pid, str) and derived.PID_RX.match(pid) and derived.manifest(pid) is not None:
        return "drawings.make", {"product_id": pid, "reason": "changed"}
    return None


THEN = {"product.load": _drawings_after, "upload.build": _drawings_after}

