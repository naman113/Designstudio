"""The catalogue's drawing hand-over: `catalogue.publish_drawings`; and a build session's single sheet,
`catalogue.publish_drawing`, below, in the same module with the same checks module and the same writer rule.

The only writer of framework/web/static/files/drawings/<pid>/. It takes the folder a geometry job made under
work/drawings/<pid>/<job_id>/, re-checks it (framework/catalog/drawing_checks.py), copies each file into
`<set12>/` (a temp file, then `os.replace`), updates `index.json` with `os.replace`, keeps two sets per design (a
third publish removes the oldest set's folder) and removes the hand-over folder (an inbox, like the upload inbox).

Payload   {"product_id", "job_dir", "fingerprint", "kinds": ["views", ...], "job_id"?, "salvage"? (true with fingerprint null hands
          over the views sheet of an overview job that did not finish, under the fingerprint of its own report)}
Answer    {"set", "published": [file, ...], "sheets": n}
Refusal   ServiceError("drawing_refused", "The drawing could not be handed over.", detail=<which check>); nothing is written.
A command read again after its first run finished (the hand-over folder gone, the set already listed with the same job id)
answers the same.
"""
import datetime
import json
import os
import re
import shutil
import time

from framework.catalog import drawing_bom, drawing_checks as dc
from framework.common import derived, drawfp, results
from framework.common.files import replace_retry
from framework.common.consumer import ServiceError

ROOT = derived.ROOT
FILES = os.path.join(ROOT, "framework", "web", "static", "files")
PUBLISHED = os.path.join(FILES, "drawings")
KEEP_SETS = 2
LABELS = {"views": "Views", "assembly": "Assembly"}
ASSEMBLY_REF = "*assembly"      # the failure entry of the assembly drawing. A part ref is [A-Za-z0-9_]+, so no part can be mistaken for it
LEGACY_ASSEMBLY_REF = "ASSEMBLY"   # an older name for it; sets published earlier may carry it


def index_path(pid):
    return os.path.join(PUBLISHED, pid, "index.json")


def index_load(pid):
    """The design's index.json (a dict), or None when it has no studio set."""
    try:
        with open(index_path(pid), encoding="utf-8") as fh:
            idx = json.load(fh)
    except (OSError, ValueError):
        return None
    return idx if isinstance(idx, dict) and isinstance(idx.get("sets"), list) else None


def index_write(pid, idx):
    path = index_path(pid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        json.dump(idx, fh)
    replace_retry(tmp, path)


def current_set(idx):
    if not idx:
        return None
    return next((s for s in idx["sets"] if s["set"] == idx.get("current")), None)


def _refused(detail):
    print("catalogue.publish_drawings refused: %s" % detail, flush=True)        # the detail reaches the service log, not only the reply
    return ServiceError("drawing_refused", "The drawing could not be handed over.", detail=detail)


def _label(sheet, ref):
    """What the sheet picker shows: "Views", or "Part · <readable name>" (the name the parts list gave the geometry job)."""
    if LABELS.get(sheet["kind"]):
        return LABELS[sheet["kind"]]
    lab = sheet.get("label")
    if isinstance(lab, str) and lab.startswith("Part \u00b7 ") and len(lab) <= 160 and lab.isprintable():
        return lab
    return "Part \u00b7 %s" % ref


KIND_OF = {"views": "views", "assembly": "assembly", "parts": "part"}      # payload kind -> the report's sheet kind
ORDER = {"views": 0, "assembly": 1, "part": 2}
WHY = ("it took too long", "a size did not match the model", "it could not be drawn", "it does not fit on the largest sheet")
DRAWING_FAILED = "The drawings could not be made."


def _plain_failed(report):
    """The parts the geometry service could not draw, with the plain reason only (a traceback or the read-back details stay in
    the report and never reach the page). A reason that is not one of the four becomes the general one."""
    out = []
    for f in report.get("failed", []):
        if not isinstance(f, dict) or not isinstance(f.get("ref"), str) or not (f["ref"] in ("*", ASSEMBLY_REF) or dc.REF_RX.match(f["ref"])):
            continue
        out.append({"ref": f["ref"], "why": f["why"] if f.get("why") in WHY else "it could not be drawn"})
    return out


def _assembly_failed(report, kinds):
    """An assembly drawing that failed its read-back (or did not fit) is listed on the set, with the plain reason only;
    the views sheet was handed over alone. [] when the assembly was made, or this hand-over is not the overview's."""
    if kinds == ["parts"] or any(s.get("kind") == "assembly" for s in report.get("sheets", [])):
        return []
    return [f for f in _plain_failed(report) if f["ref"] == ASSEMBLY_REF]


def _is_assembly(f):
    return f.get("ref") in (ASSEMBLY_REF, LEGACY_ASSEMBLY_REF)


def _entry_of(idx, set12):
    return next((s for s in (idx or {}).get("sets", []) if s["set"] == set12), None)


SWEEP_AGE_S = 900       # a hand-over folder younger than this may belong to a job still running
UUID_DIR = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def sweep_work(pid, keep=None):
    """Remove the design's old hand-over folders under work/drawings/<pid>/ that a failed, refused, stopped or
    lost job left behind (a successful hand-over removes its own). Folders younger than 15 minutes stay (a job may still run), so
    does the folder being handed over (`keep`) and the newest of the older ones, for diagnosis. Only uuid-named folders whose
    resolved path is a direct child of the design's folder are touched. Never raises."""
    try:
        base = os.path.realpath(os.path.join(dc.DRAWINGS_WORK, pid))
        if not os.path.isdir(base):
            return 0
        keep_name = os.path.basename(str(keep).rstrip("/\\")) if isinstance(keep, str) else None
        now = time.time()
        old = []
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if (UUID_DIR.match(name) and name != keep_name and os.path.isdir(p) and not os.path.islink(p)
                    and os.path.dirname(os.path.realpath(p)) == base and now - os.path.getmtime(p) > SWEEP_AGE_S):
                old.append((os.path.getmtime(p), p))
        old.sort(reverse=True)
        for _m, p in old[1:]:
            shutil.rmtree(p, ignore_errors=True)
        return max(0, len(old) - 1)
    except OSError:
        return 0


def publish_drawings(ctx, payload):
    """Hand over the sheets of one geometry job: the views sheet or the part sheets. Both go into the set of the
    design's fingerprint: the parts join the entry the views made. `kinds` is ["views"] or ["parts"]; for ["parts"] the job
    folder may be absent (every part failed): the failure is recorded on the set, and the job fails only when neither the
    views sheet (`overview_published`) nor any part was made."""
    pid = payload.get("product_id")
    fp = payload.get("fingerprint")
    kinds = payload.get("kinds")
    job_id = payload.get("job_id")
    try:
        dc.check_ids(pid)
        sweep_work(pid, payload.get("job_dir"))
        manifest = os.path.join(derived.PRODUCTS, pid + ".json")
        if not os.path.isfile(manifest):
            raise dc.DrawingRefused("the design has no manifest")
        if not isinstance(kinds, list) or not kinds or any(k not in KIND_OF for k in kinds):
            raise dc.DrawingRefused("kinds are not known")
        want_kinds = {KIND_OF[k] for k in kinds}
        job_dir_in = payload.get("job_dir")
        salvage = payload.get("salvage")
        if salvage not in (None, True) or (salvage and (kinds != ["views"] or not isinstance(job_dir_in, str))):
            raise dc.DrawingRefused("salvage is not a flag for a views folder")
        if salvage and fp is None:
            # the overview job did not finish (the assembly was cut off) but had written the views sheet
            # and its own report first: hand that over, under the fingerprint the report carries
            try:
                with open(os.path.join(dc.check_job_dir(pid, job_dir_in), "report.json"), encoding="utf-8") as fh:
                    fp = json.load(fh).get("fingerprint")
            except (OSError, ValueError):
                raise dc.DrawingRefused("report.json is missing or unreadable")
        if job_dir_in is None and kinds != ["parts"]:
            raise dc.DrawingRefused("job_dir is not a path")
        if fp is None and job_dir_in is None:
            fp = drawfp.fingerprint(manifest)              # nothing was drawn: the fingerprint of the design as it is now
        if not isinstance(fp, str) or not dc.FP_RX.match(fp):
            raise dc.DrawingRefused("fingerprint is not a sha256")
        if job_id is not None and (not isinstance(job_id, str) or len(job_id) > 64):
            raise dc.DrawingRefused("job_id is not a string")
        if not isinstance(payload.get("overview_published", False), bool):
            raise dc.DrawingRefused("overview_published is not a flag")
        set12 = drawfp.set_id(fp)
        idx = index_load(pid)
        done = _entry_of(idx, set12)
        key = "parts_job_id" if kinds == ["parts"] else "job_id"
        # a redelivered command whose first run already finished: same set, same job id, hand-over folder gone
        full = job_dir_in
        if (done is not None and job_id and done.get(key) == job_id and isinstance(full, str)
                and not os.path.isdir(full if os.path.isabs(full) else os.path.join(ROOT, full))):
            return results.pack(ctx.job_id, {"set": set12, "published": [s["file"] for s in done["sheets"]],
                                             "sheets": len(done["sheets"])})
        if job_dir_in is None:
            return _publish_failure(ctx, pid, idx, set12, bool(payload.get("overview_published")))
        job_dir = dc.check_job_dir(pid, job_dir_in)
        try:
            with open(os.path.join(job_dir, "report.json"), encoding="utf-8") as fh:
                report = json.load(fh)
        except (OSError, ValueError):
            raise dc.DrawingRefused("report.json is missing or unreadable")
        if report.get("product_id") != pid:
            raise dc.DrawingRefused("the report is for another design")
        if (salvage and "assembly" in (report.get("kinds") or []) and not any(x.get("kind") == "assembly" for x in report.get("sheets", []))
                and not any(x.get("ref") == ASSEMBLY_REF for x in report.get("failed", []))):
            report = dict(report, failed=list(report.get("failed", [])) + [{"ref": ASSEMBLY_REF, "why": "it could not be drawn"}])
        if report.get("fingerprint") != fp:
            raise dc.DrawingRefused("the report's fingerprint is not the request's")
        sheets = [s for s in report.get("sheets", []) if s.get("kind") in want_kinds]
        for k in kinds:
            if k != "parts" and not any(s["kind"] == KIND_OF[k] for s in sheets):
                raise dc.DrawingRefused("no %s sheet in the report" % k)
        todo, seen = [], set()
        for s in sheets:
            fk = dc.file_kind(str(s.get("file")))
            if fk is None or fk[0] != s["kind"]:
                raise dc.DrawingRefused("a sheet's file name does not match its kind")
            dc.check_ids(pid, fk[1])
            if fk[1] in seen or (fk[1] is None and fk[0] in seen):          # Views and Assembly both have no ref
                raise dc.DrawingRefused("a part is listed twice" if fk[1] else "a sheet is listed twice")
            seen.add(fk[1] or fk[0])
            dc.check_report_sheet(s)
            sha = dc.check_file(os.path.join(job_dir, s["file"]), fk[2])
            if sha != s.get("sha256"):
                raise dc.DrawingRefused("a sheet's sha256 is not the report's")
            todo.append((s, fk))
        if "parts" in kinds and not todo and not _plain_failed(report):
            raise dc.DrawingRefused("no part sheet and no failed part in the report")
    except dc.DrawingRefused as exc:
        raise _refused(exc.detail)
    ctx.progress("publish")
    sdir = os.path.join(PUBLISHED, pid, set12)
    os.makedirs(sdir, exist_ok=True)
    for s, _fk in todo:
        dst = os.path.join(sdir, s["file"])
        tmp = dst + ".tmp%d" % os.getpid()
        shutil.copyfile(os.path.join(job_dir, s["file"]), tmp)
        replace_retry(tmp, dst)
    try:
        with open(manifest, encoding="utf-8") as fh:
            revision = json.load(fh).get("revision", "-")
    except (OSError, ValueError):
        revision = "-"
    new_sheets = [{"kind": s["kind"], "ref": fk[1] or s["kind"], "label": _label(s, fk[1]), "file": s["file"],
                   "numbers": s.get("numbers"), "wrong": s.get("wrong")} for s, fk in todo]
    old = _entry_of(idx, set12)
    kept = [x for x in (old["sheets"] if old else []) if x["kind"] not in want_kinds]    # the other kinds of the same set stay
    gone = [x for x in (old["sheets"] if old else []) if x["kind"] in want_kinds and x["file"] not in {n["file"] for n in new_sheets}]
    entry = {"set": set12, "fingerprint": fp,
             "made_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "revision": revision, "job_id": (old or {}).get("job_id") if kinds == ["parts"] else job_id,
             "sheets": sorted(kept + new_sheets, key=lambda x: ORDER.get(x["kind"], 9)),
             "rules_version": drawfp.DRAW_RULES_VERSION,
             "failed": [f for f in (old or {}).get("failed", []) if not _is_assembly(f)] + _assembly_failed(report, kinds),
             "bought_in": (old or {}).get("bought_in", []),
             "sizes": (old or {}).get("sizes", {})}
    if old and old.get("parts_job_id"):
        entry["parts_job_id"] = old["parts_job_id"]
    if kinds != ["parts"] and (old is None or old.get("parts_pending")):
        entry["parts_pending"] = True       # a NEW set made by its views hand-over has no part drawings yet; the parts hand-over (or the failure record) clears it
    if kinds == ["parts"]:
        entry["parts_job_id"] = job_id
        entry["failed"] = [f for f in (old or {}).get("failed", []) if _is_assembly(f)] + [
            f for f in _plain_failed(report) if f["ref"] != ASSEMBLY_REF]
        entry["bought_in"] = [r for r in report.get("bought_in", []) if isinstance(r, str) and dc.REF_RX.match(r)]
    else:
        entry["sizes"] = {pp["ref"]: pp["size_mm"] for pp in report.get("parts", []) if "ref" in pp and "size_mm" in pp}
    idx = idx or {"v": 1, "product_id": pid, "current": None, "sets": []}
    idx["sets"] = [entry] + [s for s in idx["sets"] if s["set"] != set12]       # newest first; the same fingerprint: in place
    idx["current"] = set12
    dropped = idx["sets"][KEEP_SETS:]
    idx["sets"] = idx["sets"][:KEEP_SETS]
    index_write(pid, idx)
    for g in gone:                                                              # a part no longer made: its old file goes
        if dc.file_kind(g["file"]):
            try:
                os.remove(os.path.join(sdir, g["file"]))
            except OSError:
                pass
    for s in dropped:
        if dc.SET_RX.match(s["set"]):
            shutil.rmtree(os.path.join(PUBLISHED, pid, s["set"]), ignore_errors=True)
    shutil.rmtree(job_dir, ignore_errors=True)                                  # the hand-over folder is an inbox
    return results.pack(ctx.job_id, {"set": set12, "published": [s["file"] for s, _ in todo], "sheets": len(todo)})


def _publish_failure(ctx, pid, idx, set12, overview_published):
    """Every part failed (the parts job made nothing): record it on the set the views sheet made, or fail the job when there
    is no set either: nothing at all was made."""
    entry = _entry_of(idx, set12) or current_set(idx)
    if not overview_published or not entry:
        raise ServiceError("drawing_failed", DRAWING_FAILED, detail="neither the views sheet nor any part was made")
    entry["failed"] = [f for f in entry.get("failed", []) if _is_assembly(f)] + [{"ref": "*", "why": "it could not be drawn"}]   # the assembly's entry stays
    entry.pop("parts_pending", None)        # the parts job ran and made nothing: that is recorded, not pending
    index_write(pid, idx)
    return results.pack(ctx.job_id, {"set": entry["set"], "published": [], "sheets": 0})


def drawing_bom_job(ctx, payload):
    """`catalogue.drawing_bom {product_id, sizes?}`: the parts list the drawings use (readable names, quantities,
    materials, bought-in flags). Read-only catalogue data the page already shows. With `sizes: true` every row also carries the part's measured box `size_mm`."""
    pid = payload.get("product_id") if isinstance(payload, dict) else None
    result = drawing_bom.bom(pid, sizes=bool(payload.get("sizes")) if isinstance(payload, dict) else False)
    if result is None:
        raise ServiceError("no_product", "There is no design with that name.")
    return results.pack(ctx.job_id, result)


def publish_drawing(ctx, payload):
    """A build session's sheet. Whatever sent it may have checked it; every check is made again here
    (drawing_checks.check_session_sheet), and the copy is checked once more after it is made, so a file changed between the
    check and the copy is never served. The one writer of static/files/<product>-<REF>.pdf (the studio's own sets are the
    other, under static/files/drawings/; both are policy.SINGLE resource `drawings.store`).

    Payload   {"product", "ref", "pdf" (root-relative, inside work/sessions/<sid>/), "sid"}
    Answer    {"product_id", "ref", "name"}   (strings only, as results.pack text)
    Refusal   ServiceError("drawing_refused", "The drawing could not be handed over.", detail=<which check>); nothing is
              written. A command read again after the first run copies the same bytes to the same place."""
    product, ref, sid, pdf = payload.get("product"), payload.get("ref"), payload.get("sid"), payload.get("pdf")
    name = tmp = None
    try:
        src, sha = dc.check_session_sheet(product, ref, sid, pdf)
        name = "%s-%s.pdf" % (product, ref)
        os.makedirs(FILES, exist_ok=True)
        dest = os.path.join(FILES, name)
        tmp = dest + ".tmp%d" % os.getpid()
        ctx.progress("publish")
        shutil.copyfile(src, tmp)
        if dc.check_file(tmp, "pdf") != sha:
            raise dc.DrawingRefused("the sheet changed while it was copied")
        os.replace(tmp, dest)
        tmp = None
    except dc.DrawingRefused as exc:
        raise _refused(exc.detail)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return results.pack(ctx.job_id, {"product_id": product, "ref": ref, "name": name})   # a plain dict with a "ref" key would be read as a file reference


HANDLERS = {"catalogue.publish_drawings": publish_drawings, "catalogue.drawing_bom": drawing_bom_job,
            "catalogue.publish_drawing": publish_drawing}
