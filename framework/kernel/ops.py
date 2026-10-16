"""The geometry operations. They run ONLY inside a worker process of `worker_pool` (never in the
service process), call the kernel's own code unchanged, and leave their full result in a JSON file written with
`json.dump` (a temp file in the same folder, then `os.replace`: atomic, and deterministic by its path, so a retry or a
redelivery rewrites the same file). What goes back through the pipe, and then through the queues, is a small dict:
the file's path and counts. Numbers keep their bytes because they never travel as nested jsonb.

A path string is passed to the kernel exactly as given (a relative one is joined to the project root, nothing else):
`extract._load_sidecar` keys a `labels.json` beside the STEP file by the file's basename with its original case, and
`source_file` is stored, so normalising the string would change records.

Operations: measure, read_step, mesh, step_out, signature, topology, draw_overview, draw_parts,
selftest, measure_file (the kernel's records of one STEP file, unchanged),
tight_extent, parts' exact extents and solids, and part_view and part_step, one part's own
3D view and STEP file.
"""
import json
import os
import time

from framework.common import config
from framework.common.files import replace_retry, sha256

ROOT = config.ROOT
PROGRESS = None          # set by the worker for the length of a job: PROGRESS(dict) sends a progress message to the handler
SOLID_KEYS = ("volume_val", "volume_method", "volume_unc", "area_val", "area_method", "area_unc")


def _abs(p):
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def _rel(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def _write(out_json, obj):
    """json.dump to a temp file next to the target, then an atomic replace."""
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    tmp = out_json + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        json.dump(obj, fh)
    replace_retry(tmp, out_json)
    return _rel(out_json)


def _ms(t0):
    return (time.time() - t0) * 1000.0


# ---- measure ---------------------------------------------------------------------------------------------------------
def measure(path, out_json, topology=False):
    """One document: the extract dict (STEP: framework.kernel.extract.extract; .FCStd: its BREP dump), the per-solid
    volume/area values captured from the ONE mass_props call measure() already makes per part, the three shape
    signatures of every part (from a SECOND fresh load, because the shape measure() touched may carry a triangulation
    that would change d2_histogram), and, when asked, the degenerate-excluding topology counts."""
    from framework.kernel import io as kio
    from framework.kernel import measure as kmeasure
    from framework.kernel import signature as ksig
    p = _abs(path)
    orig = kmeasure.mass_props
    caps = []

    def capture(shape):
        res = orig(shape)
        caps.append(res)
        return res

    is_fc = p.lower().endswith(".fcstd")
    t0 = time.time()
    kmeasure.mass_props = capture
    try:
        if not is_fc:
            from framework.kernel.extract import extract
            doc = extract(p)
        else:
            from framework.kernel import brep_dump
            doc = brep_dump.extract_dump(p, brep_dump.brep_dir_for(p), sha256(p))
    finally:
        kmeasure.mass_props = orig
    doc_ms = _ms(t0)
    parts = doc["parts"]
    if len(caps) != len(parts):
        raise AssertionError("mass_props was called %d times for %d part records" % (len(caps), len(parts)))
    solids = {rec["object"]: [{k: s[k] for k in SOLID_KEYS} for s in cap["solids"]] for rec, cap in zip(parts, caps)}

    records = {rec["object"]: rec for rec in parts}
    shapes = {}                                   # ref -> a fresh, never-measured shape
    if not is_fc:
        for part in kio.load(p).parts:
            if part.ref in records and part.ref not in shapes:
                shapes[part.ref] = part.shape
    else:
        from framework.kernel import brep_dump
        d = brep_dump.brep_dir_for(p)
        for ref in records:
            shapes[ref] = brep_dump._read_brep(os.path.join(d, ref + ".brep"))
    signatures, topo = {}, {}
    for ref, rec in records.items():
        shape = shapes.get(ref)
        if shape is None:
            continue
        if topology:                          # before d2_histogram, which tessellates the shape
            topo[ref] = kmeasure.topology_excl_degenerate(shape)
        signatures[ref] = {"geom_hash": ksig.geom_hash(rec), "moments_v1": ksig.moments_vec(rec),
                           "d2_v1": ksig.d2_histogram(shape)}
    out = {"v": 1, "op": "measure", "path": path, "doc": doc, "doc_ms": doc_ms, "solids": solids,
           "signatures": signatures}
    if topology:
        out["topology"] = topo
    rel = _write(out_json, out)
    return {"result_file": rel, "parts": len(parts), "skipped": len(doc["skipped"]),
            "checksum_sha256": doc["checksum_sha256"]}


# ---- read_step -------------------------------------------------------------------------------------------------------
def read_step(files, out_json):
    """Each file loaded and summed up (parts with the volume rounded to 3
    decimals). A file that raises gives an `ok: false` entry and the next file still runs. The Model never leaves."""
    from framework.kernel import io as kio
    from framework.kernel import measure as kmeasure
    entries = []
    for path in files:
        t0 = time.time()
        try:
            model = kio.load(_abs(path))
            parts = [{"object": q.ref, "label": q.label, "qty": q.qty, "solids": q.n_solids,
                      "volume_mm3": round(kmeasure.volume(q.shape), 3), "material": q.material}
                     for q in model.parts]
            entries.append({"path": path, "ok": True, "error": None, "name": model.name, "parts": parts,
                            "healed": list(model.healed), "log": list(model.log), "ms": _ms(t0)})
        except Exception as exc:
            entries.append({"path": path, "ok": False, "error": repr(exc), "name": None, "parts": [], "healed": [],
                            "log": [], "ms": _ms(t0)})
    rel = _write(out_json, {"v": 1, "op": "read_step", "files": entries})
    return {"result_file": rel, "files": len(entries), "parts": [len(e["parts"]) for e in entries],
            "ok": [e["ok"] for e in entries]}


# ---- mesh, step_out --------------------------------------------------------------------------------------------------
def _dump_dir(p):
    from framework.kernel import brep_dump
    d = brep_dump.brep_dir_for(p)
    if not os.path.exists(os.path.join(d, "_labels.json")):
        raise FileNotFoundError("no BREP dump for %s" % p)
    return d


def mesh(manifest, out, header, out_json):
    """The design's viewer mesh: a GLB (partview.write_design) when `out` ends with .glb, else an OBJ (write_obj)."""
    from framework.kernel import brep_dump
    t0 = time.time()
    parts = brep_dump.product_parts(_abs(manifest), _dump_dir)
    if out.lower().endswith(".glb"):
        from framework.kernel import partview
        stats = partview.write_design(parts, _abs(out), header=header or "")
    else:
        from framework.kernel.mesh import write_obj
        stats = write_obj(parts, _abs(out), header=header or "")
    stats["ms"] = _ms(t0)
    rel = _write(out_json, {"v": 1, "op": "mesh", "stats": stats})
    return {"result_file": rel, "path": out}


def step_out(manifest, out, root_name, readback, out_json):
    """Parts named REF, REF_2 ..., plus, for each readback path that exists, the
    volume of every part read back (the compare shadow's own readback)."""
    from framework.kernel import brep_dump
    from framework.kernel import io as kio
    from framework.kernel import measure as kmeasure
    t0 = time.time()
    named, used = [], {}
    for r, shape in brep_dump.product_parts(_abs(manifest), _dump_dir):
        used[r] = used.get(r, 0) + 1
        named.append((r if used[r] == 1 else "%s_%d" % (r, used[r]), shape))
    out_abs = _abs(out)
    os.makedirs(os.path.dirname(out_abs), exist_ok=True)
    kio.write_step(named, out_abs, root_name=root_name)
    stats = {"path": out, "parts": len(named), "bytes": os.path.getsize(out_abs), "ms": _ms(t0)}
    volumes = {}
    for rb in readback or ():
        if os.path.exists(_abs(rb)):
            volumes[rb] = {q.ref: kmeasure.volume(q.shape) for q in kio.load(_abs(rb)).parts}
    rel = _write(out_json, {"v": 1, "op": "step_out", "stats": stats, "volumes": volumes})
    return {"result_file": rel, "path": out}


# ---- measure_file, check_file ------------------------------------------------------------------------------------------
def measure_file(path, out_json):
    """A re-measure of one STEP file, in process: `kio.load`, then `kmeasure.measure(part.shape, part.ref, label=part.label)` for every part in order
    (None = not measurable). The records go back unchanged in a file written with json.dump (a float keeps its bytes,
    -0.0 stays -0.0); the caller keeps its own aggregation, rounding and printing."""
    from framework.kernel import io as kio
    from framework.kernel import measure as kmeasure
    t0 = time.time()
    model = kio.load(_abs(path))
    parts = [{"ref": q.ref, "label": q.label, "rec": kmeasure.measure(q.shape, q.ref, label=q.label)}
             for q in model.parts]
    rel = _write(out_json, {"v": 1, "op": "measure_file", "path": path, "n_parts": len(model.parts),
                            "valid": bool(model.valid), "parts": parts, "ms": _ms(t0)})
    return {"result_file": rel, "n_parts": len(model.parts), "valid": bool(model.valid),
            "records": sum(1 for q in parts if q["rec"] is not None)}


# ---- signature, topology ---------------------------------------------------------------------------------------------
def signature(targets, out_json):
    """pg/signatures.resolve_shape's rule (one implementation, `brep_dump.candidate_shape`): each key's candidates in
    order (a STEP file and its part ref, or one part's BREP dump file), each file loaded once per job, the first
    shape found wins; d2_histogram of that shape. Keys run in the given order."""
    from framework.kernel import brep_dump
    from framework.kernel import signature as ksig
    cache = {}
    d2, found = {}, {}
    for key, cands in targets:
        d2[key], found[key] = None, None
        for path, ref in cands:
            shape = brep_dump.candidate_shape(_abs(path), ref, cache)
            if shape is not None:
                d2[key] = ksig.d2_histogram(shape)
                found[key] = [path, ref]
                break
    rel = _write(out_json, {"v": 1, "op": "signature", "d2_v1": d2, "found": found})
    n = sum(1 for v in found.values() if v is not None)
    return {"result_file": rel, "found": n, "missing": len(found) - n}


def topology(files, out_json):
    """tests/conftest.py's own rule: topology_excl_degenerate of the exact BREP."""
    from build123d import import_brep
    from framework.kernel import measure as kmeasure
    out = {key: kmeasure.topology_excl_degenerate(import_brep(_abs(path)).wrapped) for key, path in files.items()}
    rel = _write(out_json, {"v": 1, "op": "topology", "topology": out})
    return {"result_file": rel, "n": len(out)}


# ---- draw_overview ---------------------------------------------------------------------------------------------------
def _lower_priority():
    """A drawing job runs below normal priority so the studio's own work stays quick. Returns the old value."""
    import psutil
    proc = psutil.Process()
    old = proc.nice()
    try:
        proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
    except Exception:
        return None
    return old


def _restore_priority(old):
    if old is None:
        return
    try:
        import psutil
        psutil.Process().nice(old)
    except Exception:
        pass


def draw_overview(manifest, product_id, out_dir, kinds, bom, check_fault, preview, out_json, assembly_skipped=None):
    """The views sheet of a design: tight extents first, then the kernel's own triangles, the shaded views, the
    read-back check, and `<out_dir>/views.png` + `<out_dir>/report.json` (framework/kernel/drawings/). A sheet whose
    check finds a wrong number is still written, with `wrong` > 0 in its report; the catalogue refuses to publish it.

    With "assembly" in `kinds` and more than one distinct part, the SAME load also makes the assembly drawing
    (`drawings/assembly.py`): its balloons reuse the part-id buffers the views sheet rendered, its numbers are read back from the
    finished document (`check.check_assembly`), and `<out_dir>/assembly.pdf` (one page, or two above 30 parts-list rows) is added
    to the report. An assembly drawing whose check finds anything is NOT kept (`failed [{"ref": "*assembly", ...}]`: a sentinel no part ref can match); the views
    sheet stays. Progress goes to the handler through PROGRESS (`views`, `assembly`)."""
    import hashlib
    import io as _io
    import numpy as np
    from PIL import Image
    from framework.common import drawfp
    from framework.kernel import brep_dump
    from framework.kernel.drawings import check as dcheck
    from framework.kernel.drawings import extents, tess, views
    old_prio = _lower_priority()
    try:
        t_start = time.time()
        times, mark = {}, [time.perf_counter()]
        if PROGRESS is not None:
            PROGRESS({"step": "views"})

        def lap(name):
            now = time.perf_counter()
            times[name] = round(now - mark[0], 3)
            mark[0] = now

        mpath = _abs(manifest)
        fp = drawfp.fingerprint(mpath)
        with open(mpath, encoding="utf-8") as fh:
            man = json.load(fh)
        name, revision = man.get("name") or product_id, man.get("revision", "-")
        parts = brep_dump.product_parts(mpath, _dump_dir)
        lap("load")
        # tight extents BEFORE any tessellation (a triangulation on a shape changes what some boxes see)
        occ_ext = [extents.extent(shape) for _ref, shape in parts]
        by_ref, qty, first = {}, {}, {}
        for (ref, _s), e in zip(parts, occ_ext):
            first.setdefault(ref, e)
            by_ref[ref] = extents.union([by_ref[ref], e]) if ref in by_ref else e
            qty[ref] = qty.get(ref, 0) + 1
        overall = extents.union(occ_ext)
        lap("extents")
        colors = {}
        for ref, _s in parts:
            colors.setdefault(ref, len(colors))
        tparts, edges, tri, nseg, vext, lin_max = [], {}, 0, 0, [], 0.0
        for ref, shape in parts:
            lin_max = max(lin_max, tess.deflection(shape))
            P, N, T, S = tess.tessellate(shape)
            if not len(P):
                continue
            tparts.append((ref, P, N, T))
            edges.setdefault(ref, []).append(S)
            tri += len(T)
            nseg += len(S)
            vext.append((P[:, 0].min(), P[:, 0].max(), P[:, 1].min(), P[:, 1].max(), P[:, 2].min(), P[:, 2].max()))
        edges = {ref: np.concatenate(v) for ref, v in edges.items()}
        lap("tessellate")
        png, printed, rows, id_views = views.compose(name, revision, len(parts), by_ref, overall, tparts, edges, colors,
                                                     clock=lap)
        lap("compose")
        size = extents.size(overall)
        res = dcheck.check_views(printed, size, len(parts), rows)
        tvt = dcheck.tess_vs_tight(vext, overall) if vext else 0.0
        tol = max(dcheck.TESS_TOL_MM, lin_max if lin_max > 0.1 else 0.0)   # vertices can lie one deflection inside the true edge
        if tvt > tol:
            res["wrong"] += 1
            res["details"].append("the tessellation's extent differs from the tight extent by %.3f mm (limit %.3f)" % (tvt, tol))
        studio_texts = [r["caption"] for r in rows if r["view"] != "header_title"]
        word_hits = dcheck.words(studio_texts)
        name_words = dcheck.words([str(name)])
        lap("check")
        out_abs = _abs(out_dir)
        os.makedirs(out_abs, exist_ok=True)
        png_path = os.path.join(out_abs, "views.png")
        tmp = png_path + ".tmp%d" % os.getpid()
        with open(tmp, "wb") as fh:
            fh.write(png)
        replace_retry(tmp, png_path)
        sheet = {"kind": "views", "file": "views.png", "sha256": hashlib.sha256(png).hexdigest(), "bytes": len(png),
                 "px": list(Image.open(_io.BytesIO(png)).size), "numbers": res["numbers"], "wrong": res["wrong"],
                 "unexplained": res["unexplained"], "missing": res["missing"], "words": word_hits,
                 "details": res["details"], "ms": round(sum(times.get("view_" + v, 0) for v in
                                                           ("front", "right", "top", "rear", "bottom", "iso")) * 1000),
                 "printed": printed, "views": [r for r in rows if "sec" in r]}
        sheets, failed = [sheet], []
        if assembly_skipped:                        # the handler fell back to the views sheet alone (the parts list failed its check)
            failed.append({"ref": "*assembly", "why": "it could not be drawn", "detail": str(assembly_skipped)[:200]})
            print("draw_overview %s: no assembly drawing (%s)" % (product_id, assembly_skipped), flush=True)
        report = {"v": 1, "product_id": product_id, "fingerprint": fp, "name": name, "revision": revision,
                  "kinds": list(kinds), "made_ms": round((time.time() - t_start) * 1000), "sheets": sheets, "failed": failed,
                  "parts": [{"ref": ref, "qty": qty[ref], "extent": list(by_ref[ref]),   # extent: all occurrences; size_mm: one
                             "size_mm": list(extents.size(first[ref]))} for ref in by_ref],
                  "overall": {"extent": list(overall), "size_mm": list(size)}, "times_s": times, "triangles": tri,
                  "edge_segments": nseg, "tess_vs_tight_mm": round(float(tvt), 4), "name_words": name_words}
        if "assembly" in kinds and len(by_ref) > 1:
            _write(os.path.join(out_abs, "report.json"), report)    # the views sheet is on disk, with its own report, before the assembly is tried
            if PROGRESS is not None:
                PROGRESS({"step": "assembly"})
            asm, why = _draw_assembly(parts, first, overall, bom, {"product_id": product_id, "name": name,
                                                                   "revision": revision}, id_views, out_abs, check_fault, preview)
            lap("assembly")
            if asm is not None:
                sheets.append(asm)
            else:
                failed.append({"ref": "*assembly", "why": why["why"], "detail": why.get("detail", "")})
                print("draw_overview %s: the assembly drawing was not kept (%s): %s" % (product_id, why["why"], str(why.get("detail", ""))[:600]), flush=True)
            report["made_ms"] = round((time.time() - t_start) * 1000)
        _write(os.path.join(out_abs, "report.json"), report)
        return {"result_file": _rel(os.path.join(out_abs, "report.json")), "job_dir": _rel(out_abs), "fingerprint": fp,
                "sheets": len(sheets), "failed": len(failed),
                "wrong": res["wrong"] + res["unexplained"] + res["missing"] + len(word_hits)}
    finally:
        _restore_priority(old_prio)


def _draw_assembly(parts, first, overall, bom, design, id_views, out_abs, check_fault, preview=False):
    """The assembly drawing of the design just loaded. -> (sheet dict for the report, None) or (None, {"why", "detail"}).
    The shapes are COPIED (geometry only, no triangulation): the views sheet has meshed the originals finely, and the
    triangulation pass of the sheet engine would draw every one of those triangles; the copy is meshed at the paper deflection."""
    import hashlib
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.TopoDS import TopoDS_Compound
    from framework.kernel.drawings import assembly as asm
    from framework.kernel.drawings import check as dcheck
    from framework.kernel.drawings import partreport as pr
    from framework.kernel.drawings import sheet as dsheet
    t0 = time.perf_counter()
    try:
        comp = TopoDS_Compound()
        bld = BRep_Builder()
        bld.MakeCompound(comp)
        for _ref, shape in parts:
            bld.Add(comp, BRepBuilderAPI_Copy(shape, True, False).Shape())
        copy_s = time.perf_counter() - t0
        docs, sheets, expected, rep = asm.draw_assembly(comp, first, overall, bom, design, id_views,
                                                        fault=check_fault)
        rows = rep.pop("_rows")
        res = dcheck.check_assembly(docs, expected, asm.furniture(sheets, design, rows))
        if res["unexplained"] or res["missing"] or res["wrong"] or res["words"]:
            return None, {"why": pr.WHY_SIZE, "detail": "; ".join(res["details"][:6]) + (" | words %s" % res["words"] if res["words"] else "")}
        t1 = time.perf_counter()
        pdf = dsheet.pdf_bytes(docs, sheets)
        pdf_s = time.perf_counter() - t1
        path = os.path.join(out_abs, "assembly.pdf")
        tmp = path + ".tmp%d" % os.getpid()
        with open(tmp, "wb") as fh:
            fh.write(pdf)
        replace_retry(tmp, path)
        if preview:                                   # the CLI's check: a picture of page 1 for a reviewer, never published
            for k, (d, sh) in enumerate(zip(docs, sheets)):
                dsheet.preview_png(d, sh, os.path.join(out_abs, "assembly-preview.png" if k == 0 else "assembly-preview-%d.png" % (k + 1)), dpi=110)
        entry = dict(rep, kind="assembly", label="Assembly", file="assembly.pdf", sha256=hashlib.sha256(pdf).hexdigest(),
                     bytes=len(pdf), numbers=res["numbers"], expected=res["expected"], unexplained=0, missing=0, wrong=0,
                     words=[], name_words=res["name_words"], texts=res["texts"], copy_s=round(copy_s, 3),
                     pdf_s=round(pdf_s, 3), ms=round((time.perf_counter() - t0) * 1000))
        return entry, None
    except asm.DrawFail as exc:
        return None, {"why": exc.why, "detail": exc.detail}
    except Exception as exc:                          # noqa: BLE001 - an assembly that cannot be drawn is reported, not fatal
        import traceback
        return None, {"why": pr.WHY_DRAW, "detail": "%r | %s" % (exc, traceback.format_exc()[-600:])}
    finally:
        dsheet.clear_cache()


# ---- draw_parts ------------------------------------------------------------------------------------------------------
def draw_parts(manifest, product_id, out_dir, bom, refs, check_fault, out_json):
    """One first-angle drawing per drawable part of a design (not bought in; a design of one part always draws it), from ONE
    load of the design. Each sheet is drawn by `drawings/autodim.py`, its numbers are READ BACK from the finished document
    (`drawings/check.py`) and compared with the kernel values the rules placed; a sheet with an unexplained, missing or wrong
    number, or a word the studio may not show, is NOT kept (the part is listed "a size did not match the model"). Each kept
    sheet is `<out_dir>/part-<REF>.pdf`; `<out_dir>/report.json` is rewritten after EVERY part, so a later failure or a
    time-out keeps what is made. `refs` limits the parts (the handler resumes after a stopped part); a report already there
    for this job and fingerprint is continued. Progress per part goes to the handler through PROGRESS."""
    import hashlib
    from framework.common import drawfp
    from framework.kernel import brep_dump
    from framework.kernel.drawings import autodim
    from framework.kernel.drawings import partreport as pr
    from framework.kernel.drawings import sheet as dsheet
    old_prio = _lower_priority()
    try:
        t_start = time.time()
        mpath = _abs(manifest)
        fp = drawfp.fingerprint(mpath)
        with open(mpath, encoding="utf-8") as fh:
            man = json.load(fh)
        name, revision = man.get("name") or product_id, man.get("revision", "-")
        out_abs = _abs(out_dir)
        os.makedirs(out_abs, exist_ok=True)
        rep_path = os.path.join(out_abs, "report.json")
        report = pr.load_or_new(rep_path, product_id, fp, name, revision, bom)
        want = pr.drawable(bom)
        total = len(want)
        todo = [r for r in want if (refs is None or r in refs) and r not in pr.done_refs(report)]
        t0 = time.perf_counter()
        parts = brep_dump.product_parts(mpath, _dump_dir)
        report["times_s"]["load"] = round(report["times_s"].get("load", 0.0) + time.perf_counter() - t0, 3)
        first = {}
        for ref, shape in parts:
            first.setdefault(ref, shape)                  # the first occurrence draws the part
        design = {"product_id": product_id, "name": name, "revision": revision}
        rows = {r["ref"]: r for r in bom["rows"]}
        for ref in todo:
            i = want.index(ref) + 1
            if PROGRESS is not None:
                PROGRESS({"ref": ref, "i": i, "of": total, "t": "start"})
            t_part = time.perf_counter()
            if ref not in first:
                pr.fail(report, ref, pr.WHY_DRAW, detail="the design has no shape for this part")
            else:
                try:
                    res, pdf = autodim.draw_part(first[ref], rows[ref], design, check_fault)
                except autodim.DrawFail as exc:
                    pr.fail(report, ref, exc.why)
                except Exception as exc:                  # noqa: BLE001 - a part that cannot be drawn is reported, not fatal
                    import traceback
                    pr.fail(report, ref, pr.WHY_DRAW, detail="%r | %s" % (exc, traceback.format_exc()[-600:]))
                else:
                    res.pop("_doc", None)
                    res.pop("_sheet", None)
                    if res["unexplained"] or res["missing"] or res["wrong"] or res["words"]:
                        pr.fail(report, ref, pr.WHY_SIZE, numbers=res["numbers"], unexplained=res["unexplained"],
                                missing=res["missing"], wrong=res["wrong"], words=res["words"], details=res["details"],
                                texts=res["texts"])
                    else:
                        fname = "part-%s.pdf" % ref
                        pdf_path = os.path.join(out_abs, fname)
                        tmp = pdf_path + ".tmp%d" % os.getpid()
                        with open(tmp, "wb") as fh:
                            fh.write(pdf)
                        replace_retry(tmp, pdf_path)
                        entry = dict(res, kind="part", ref=ref, label="Part \u00b7 %s" % rows[ref]["name"], file=fname,
                                     sha256=hashlib.sha256(pdf).hexdigest(), bytes=len(pdf),
                                     ms=round((time.perf_counter() - t_part) * 1000))
                        report["failed"] = [f for f in report["failed"] if f["ref"] != ref]
                        report["sheets"] = [s for s in report["sheets"] if s["ref"] != ref] + [entry]
            dsheet.clear_cache()
            report["times_s"]["total"] = round(report["times_s"].get("total", 0.0) + time.perf_counter() - t_part, 3)
            pr.save(rep_path, pr.in_bom_order(report, bom))
        report["made_ms"] = round(report.get("made_ms", 0) + (time.time() - t_start) * 1000)
        pr.save(rep_path, pr.in_bom_order(report, bom))
        return {"result_file": _rel(rep_path), "job_dir": _rel(out_abs), "fingerprint": fp, "sheets": len(report["sheets"]),
                "failed": len(report["failed"]), "wrong": 0}
    finally:
        _restore_priority(old_prio)


# ---- one part on its own ----------------------------------------------------------------------------------------------
def part_view(part_id, sources, key, out_glb, out_facts, out_json):
    """One part's own 3D view (framework/kernel/partview.py) from the first of its sources that holds its shape, written
    to `out_glb` with its facts beside it (`out_facts`); the part's views made with another key are removed. A part none
    of whose sources holds it answers found: false (the handler turns it into one plain sentence)."""
    from framework.common import partview as cpv
    from framework.kernel import partview as kpv
    t0 = time.time()
    shape, used, kind = kpv.load_source([(_abs(p), o) for p, o in sources])
    load_ms = _ms(t0)
    if shape is None:
        _write(out_json, {"v": 1, "op": "part_view", "found": False})
        return {"found": False}
    stats = kpv.write_view(shape, _abs(out_glb), extras={"key": key, "source": kind})
    facts = dict(stats, load_ms=round(load_ms, 1), total_ms=round(_ms(t0), 1), source_index=used,
                 made_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    rel = _write(_abs(out_facts), facts)
    cpv.drop_others(part_id, key, "glb")
    _write(out_json, {"v": 1, "op": "part_view", "found": True, "facts": rel})
    return {"found": True, "result_file": rel, "triangles": stats["triangles"], "ms": facts["total_ms"], "reused": False}


def tight_extent(targets, out_json, solids=True):
    """Each part's exact extent (framework/kernel/partview.extent: BRepBndLib.AddOptimal, no
    triangulation, no tolerance -- the drawings' rule) from the first of its sources that holds its shape
    (partview.load_source, the STEP read-once cache), and, with `solids`, each solid's volume and area by the catalogue's
    one rule (measure.mass_props, the per-solid breakdown measure() sums). Keys in the given order; a part none of whose
    sources holds it answers None."""
    from framework.kernel import measure as kmeasure
    from framework.kernel import partview as kpv
    out, found = {}, 0
    t0 = time.time()
    for key, sources in targets:
        t1 = time.time()
        try:                                   # one bad part is its own answer, never the end of the whole job
            shape, used, kind = kpv.load_source([(_abs(p), o) for p, o in sources])
            if shape is None:
                out[key] = None
                continue
            rec = {"extent": [float(x) for x in kpv.extent(shape)], "source": kind, "ms": None}
            if solids:
                rec["solids"] = [{k: s[k] for k in ("volume_val", "volume_method", "volume_unc", "area_val", "area_method",
                                                     "area_unc")} for s in kmeasure.mass_props(shape)["solids"]]
            rec["ms"] = round(_ms(t1), 1)
        except Exception as exc:
            out[key] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
            continue
        out[key] = rec
        found += 1
    rel = _write(out_json, {"v": 1, "op": "tight_extent", "parts": out, "ms": round(_ms(t0), 1)})
    return {"result_file": rel, "found": found, "missing": len(out) - found,
            "errors": sum(1 for v in out.values() if isinstance(v, dict) and v.get("error"))}


def part_step(part_id, sources, key, name, out_step, out_json):
    """The part alone as one STEP file (its exact shape, named `name`), written to `out_step`."""
    from framework.common import partview as cpv
    from framework.kernel import partview as kpv
    t0 = time.time()
    shape, used, kind = kpv.load_source([(_abs(p), o) for p, o in sources])
    if shape is None:
        _write(out_json, {"v": 1, "op": "part_step", "found": False})
        return {"found": False}
    st = kpv.write_part_step(shape, _abs(out_step), name)
    cpv.drop_others(part_id, key, "step")
    rel = _write(out_json, {"v": 1, "op": "part_step", "found": True, "file": _rel(_abs(out_step)), "bytes": st["bytes"],
                            "source": kind, "ms": round(_ms(t0), 1)})
    return {"found": True, "result_file": rel, "bytes": st["bytes"], "ms": round(_ms(t0), 1), "reused": False}


# ---- selftest --------------------------------------------------------------------------------------------------------
def selftest(kind, seconds):
    """`sleep`: a plain sleep. `spin`: a loop of OCP measures that cannot be interrupted from Python (what a runaway
    kernel call looks like), so only a kill can end it. The workload is the sample assembly, written by the repo's own
    sample generator (framework.samples.make) into a temporary folder."""
    if kind == "sleep":
        time.sleep(seconds)
        return {}
    import shutil
    import tempfile
    from framework.kernel import io as kio
    from framework.kernel import measure as kmeasure
    from framework.samples import make as samples_make
    tmp = tempfile.mkdtemp(prefix="buildcore-selftest-")
    try:
        path = os.path.join(tmp, "bracket_assembly.step")
        samples_make.bracket_assembly(path)
        model = kio.load(path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    end = time.time() + seconds
    while time.time() < end:
        for q in model.parts:
            kmeasure.measure(q.shape, q.ref)
    return {}


def run(op, args):
    """The worker's dispatcher: `args` is the dict the handler built (paths and options, no shapes)."""
    if op == "measure":
        return measure(args["path"], args["out_json"], args.get("topology", False))
    if op == "read_step":
        return read_step(args["files"], args["out_json"])
    if op == "mesh":
        return mesh(args["manifest"], args["out"], args.get("header", ""), args["out_json"])
    if op == "step_out":
        return step_out(args["manifest"], args["out"], args["root_name"], args.get("readback"), args["out_json"])
    if op == "measure_file":
        return measure_file(args["path"], args["out_json"])
    if op == "signature":
        return signature(args["targets"], args["out_json"])
    if op == "topology":
        return topology(args["files"], args["out_json"])
    if op == "draw_overview":
        return draw_overview(args["manifest"], args["product_id"], args["out_dir"], args["kinds"], args.get("bom"),
                             args.get("check_fault"), args.get("preview", False), args["out_json"], args.get("assembly_skipped"))
    if op == "draw_parts":
        return draw_parts(args["manifest"], args["product_id"], args["out_dir"], args["bom"], args.get("refs"),
                          args.get("check_fault"), args["out_json"])
    if op == "part_view":
        return part_view(args["part_id"], args["sources"], args["key"], args["out_glb"], args["out_facts"], args["out_json"])
    if op == "part_step":
        return part_step(args["part_id"], args["sources"], args["key"], args["name"], args["out_step"], args["out_json"])
    if op == "tight_extent":
        return tight_extent(args["targets"], args["out_json"], args.get("solids", True))
    if op == "selftest":
        return selftest(args["kind"], args["seconds"])
    raise ValueError("unknown geometry operation %r" % (op,))
