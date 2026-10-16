"""The report of a part-drawing job: `work/drawings/<pid>/<job_id>/report.json`.

Written by the worker (`ops.draw_parts`) after EVERY part, so a part that fails, runs out of time or is stopped never costs the
sheets already made; read and amended by the handler (`svc_handlers.draw_parts`) when its per-part watchdog stops a part. Both
use `save` (a temp file next to the target, then `os.replace`).

    {"v": 1, "product_id", "fingerprint", "name", "revision", "kinds": ["parts"],
     "sheets": [{"kind": "part", "ref", "label": "Part · <name>", "file": "part-<REF>.pdf", "sha256", "bytes", "size": "A3",
                 "scale": "1:2", "part_kind", "numbers", "expected", "unexplained": 0, "missing": 0, "wrong": 0, "words": [],
                 "overlaps", "ms", "layout_s", "pdf_s", ...}],
     "failed": [{"ref", "why", ...}], "bought_in": [REF, ...], "times_s": {...}}

The plain reasons a part is listed as failed (the only words the page shows): WHY.
"""
import json
import os

from framework.common.files import replace_retry

WHY_SLOW = "it took too long"
WHY_SIZE = "a size did not match the model"
WHY_DRAW = "it could not be drawn"
WHY_FIT = "it does not fit on the largest sheet"
WHY = (WHY_SLOW, WHY_SIZE, WHY_DRAW, WHY_FIT)


def drawable(bom):
    """The refs a job draws, in parts-list order: not bought in (a design of one part always draws it)."""
    rows = bom["rows"]
    single = len(rows) == 1
    return [r["ref"] for r in rows if single or not r["bought_in"]]


def new(product_id, fp, name, revision, bom):
    return {"v": 1, "product_id": product_id, "fingerprint": fp, "name": name, "revision": revision, "kinds": ["parts"],
            "sheets": [], "failed": [], "bought_in": [r["ref"] for r in bom["rows"] if r["bought_in"] and len(bom["rows"]) > 1],
            "times_s": {}}


def load_or_new(path, product_id, fp, name, revision, bom):
    """The report already written for this job (a resumed job after the watchdog stopped a part), or a new one."""
    try:
        with open(path, encoding="utf-8") as fh:
            rep = json.load(fh)
        if rep.get("product_id") == product_id and rep.get("fingerprint") == fp:
            return rep
    except (OSError, ValueError):
        pass
    return new(product_id, fp, name, revision, bom)


def done_refs(report):
    return {s["ref"] for s in report["sheets"]} | {f["ref"] for f in report["failed"]}


def in_bom_order(report, bom):
    pos = {r["ref"]: i for i, r in enumerate(bom["rows"])}
    report["sheets"].sort(key=lambda s: pos.get(s["ref"], 1 << 30))
    report["failed"].sort(key=lambda s: pos.get(s["ref"], 1 << 30))
    return report


def save(path, report):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        json.dump(report, fh)
    replace_retry(tmp, path)


def fail(report, ref, why, **extra):
    """List `ref` as failed (once) with one of the plain reasons; `extra` (a traceback tail, the read-back details) stays in
    the report and never reaches the page."""
    report["sheets"] = [s for s in report["sheets"] if s["ref"] != ref]
    report["failed"] = [f for f in report["failed"] if f["ref"] != ref]
    report["failed"].append(dict({"ref": ref, "why": why}, **extra))
