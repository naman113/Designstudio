"""Bring STEP files into the catalogue: as a new product, or as parts added to an existing one.

    python framework/catalog/add_step.py <a.step> [b.step ...] [--name "Title"]
    python framework/catalog/add_step.py <part.step> --into <product_id> [--ref BEARING]

Engines: the studio and the CLI upload through the services (`upload()` with BC_SERVICES=1 hands the
files to the orchestrator's `upload.build` recipe: the first engine reads each file and saves NN.FCStd, the second engine
reads it too and the two reads are compared, as before). `add()` is the in-process path (tests, offline use) and is the
kernel's alone: each file is read in-process (framework.kernel.io.load), healed if needed, one part
per top-level solid; every part is fully catalogued (volume, area, envelope,
inertia, bores, diameters, faces, family) and fits are detected by
`product.py load`; the viewer mesh is refreshed in-process too. The original STEP
file is copied under models/uploads/<product_id>/ -- no .FCStd is ever written --
so the product never depends on where it came from.

This module imports neither the kernel nor the engines layer at module level; `add()` imports the kernel where it reads.

The upload's logic is step functions -- prepare(), entries_kernel(), finish() --
that add() calls in order and that the orchestrator's `upload.build` recipe calls one at a time across the catalogue,
engines and geometry services (framework/catalog/svc_upload.py). The studio no longer imports this module.

Prints one progress line per step and ends with a line  RESULT {json}.
"""
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import product  # noqa: E402

PRODUCTS = os.path.join(ROOT, "framework", "products")
UPLOADS = os.path.join(ROOT, "models", "uploads")


def slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return (s or "design")[:48].strip("-")


def ref_of(s):
    r = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()
    return (r or "PART")[:24]


def names(stems):
    """Title for the product and a short reference per file. Drawing numbers share a
    long project prefix: the prefix names the product, the rest names each part.
    Cut only at '-', backing off until every remainder has at least 3 characters."""
    if len(stems) == 1:
        segs = stems[0].split("-")
        tail = segs[-1]
        if len(tail) < 3 and len(segs) > 1:
            tail = segs[-2] + "-" + tail
        return stems[0], [ref_of(tail)]
    common = os.path.commonprefix(stems)
    for cut in reversed([i + 1 for i, ch in enumerate(common) if ch == "-"]):
        rest = [st[cut:] for st in stems]
        if all(len(r) >= 3 for r in rest):
            return common[:cut].rstrip("-") or stems[0], [ref_of(r) for r in rest]
    return stems[0], [ref_of(st) for st in stems]


def _rel(path, base):
    return os.path.relpath(path, base).replace("\\", "/")


def _manifest_path(pid):
    return os.path.join(PRODUCTS, pid + ".json")


def _folder(pid):
    return os.path.join(UPLOADS, pid)


def _stems(paths):
    stems = [re.sub(r"\.(step|stp)$", "", os.path.basename(p), flags=re.I) for p in paths]
    return [re.sub(r"^\d{10,}_", "", s) for s in stems]           # web upload tokens carry a time prefix


def prepare(paths, name=None, into=None, ref=None, move=False, single_part=False, material=None):
    """Step 1 of an upload: names, the manifest (loaded for `into`, new otherwise), the product's upload folder and the
    stored copy of every source (copied, or moved when `move`; never over another stored source: `_2`, `_3` ...).
    `single_part`: "Add part to catalogue" (one solid only, origin "part"); `material`: the user's materials.json
    choice for it (None: the STEP file's material, else the default).
    -> the state the next steps take (plain JSON: `man` is the manifest dict, `sources[i].path` an absolute path) or
    {"ok": False, "error": "no product X"}."""
    stems = _stems(paths)
    auto_title, file_refs = names(stems)
    if into:
        mp = _manifest_path(into)
        if not os.path.exists(mp):
            return {"ok": False, "error": "no product %s" % into}
        with open(mp, encoding="utf-8") as fh:
            man = json.load(fh)
        pid, title = into, man.get("name", into)
        if ref and len(paths) == 1:
            file_refs = [ref_of(ref)]
    else:
        title = (name or auto_title).strip()
        pid, n, shown = slug(title), 2, title
        while os.path.exists(_manifest_path(pid)):
            pid = "%s-%d" % (slug(title), n)
            shown = "%s (%d)" % (title, n)    # the display name carries the same number as the id
            n += 1
        title = shown
        # origin = provenance: made by an upload, so the portfolio may erase it
        # from its trash (reference products have no origin and are hide-only); "part" = a single
        # part added to the catalogue, "upload" = an opened assembly (kept out of the catalogue)
        man = {"product_id": pid, "name": title, "revision": "A", "description": "",
               "origin": "part" if single_part else "upload", "parts": []}
    folder = _folder(pid)
    os.makedirs(folder, exist_ok=True)
    sources = []
    for path, stem, fref in zip(paths, stems, file_refs):
        ext = os.path.splitext(path)[1]
        src = os.path.join(folder, stem + ext)
        n = 2
        while os.path.exists(src) and os.path.abspath(path) != os.path.abspath(src):
            src = os.path.join(folder, "%s_%d%s" % (stem, n, ext))  # never overwrite a stored source
            n += 1
        stored = os.path.abspath(path) != os.path.abspath(src)
        if stored:
            (shutil.move if move else shutil.copyfile)(path, src)
        sources.append({"path": src, "stem": stem, "fref": fref, "stored": stored})
    return {"ok": True, "pid": pid, "title": title, "into": bool(into), "man": man, "folder": folder, "sources": sources,
            "single_part": bool(single_part), "material": material}


def to_wire(state):
    """The state with project-relative paths (what `catalogue.add_prepare` returns through the queues)."""
    out = dict(state, folder=_rel(state["folder"], ROOT))
    out["sources"] = [dict(s, path=_rel(s["path"], ROOT)) for s in state["sources"]]
    return out


def from_wire(wire):
    out = dict(wire, folder=os.path.join(ROOT, *wire["folder"].split("/")))
    out["sources"] = [dict(s, path=os.path.join(ROOT, *s["path"].split("/"))) for s in wire["sources"]]
    return out


def _add_refs(state, src, fref, items, extra):
    """Append one manifest entry per part item, with today's reference naming (`fref`, or `fref_label`, de-duplicated)."""
    man = state["man"]
    used = {p["ref"] for p in man["parts"]}
    for it in items:
        r = fref if len(items) == 1 else ref_of(fref + "_" + it["label"])
        base, n = r, 2
        while r in used:
            r = "%s_%d" % (base[:21], n)
            n += 1
        used.add(r)
        man["parts"].append(dict({"ref": r, "file": _rel(src, PRODUCTS)}, **extra(it)))


def entries_kernel(state, i, k_file):
    """Step 2 of an upload, first engine = the kernel (CAD_ENGINE=kernel): the entries of file `i` from the kernel's read
    (`k_file`: one `files[i]` entry of the geometry service's read_step result, or kernel_file(model)).
    -> (parts added, [(label, state)])."""
    src = state["sources"][i]["path"]
    stem, fref = state["sources"][i]["stem"], state["sources"][i]["fref"]
    if not k_file or not k_file.get("ok", True) or not k_file.get("parts"):
        return 0, [("No solid geometry in %s, skipped" % stem, "error")]
    labels = []
    if k_file.get("healed"):
        labels.append(("Repaired the geometry of %s" % stem, "done"))
        for line in k_file.get("log") or []:
            if line.startswith("healed"):
                print("  %s" % line)
    parts = k_file["parts"]
    if state.get("single_part"):
        n = sum(int(p.get("qty") or 1) * int(p.get("solids") or 1) for p in parts)
        if n != 1:
            state["rejected"] = "this file has %d parts — upload each part on its own" % n
            return 0, [(state["rejected"], "error")]
    _add_refs(state, src, fref, parts, lambda p: {"object": p["object"], "qty": p["qty"]})
    labels.append(("Read %s: %d part%s" % (stem, len(parts), "" if len(parts) == 1 else "s"), "done"))
    return len(parts), labels


def kernel_file(model):
    """A loaded kernel Model as the dict the geometry service's read_step writes per file (what entries_kernel reads)."""
    return {"ok": True, "parts": [{"object": q.ref, "label": q.label, "qty": q.qty, "solids": q.n_solids,
                                   "material": q.material} for q in model.parts],
            "healed": list(model.healed), "log": list(model.log)}


def catalogue_material(state, i, k_file):
    """The material of a catalogue part (file `i`): the user's choice, else the STEP file's own material as its
    materials.json name when it matches one, else the default (steel) -- a STEP material that is not in the list is never
    stored as is (its mass would be steel's and the material filter could not find it); the upload message says so.
    Written as the file-level "material" of the labels.json sidecar beside the stored STEP (what kernel extract reads).
    -> (the material, the upload message line)."""
    from framework.catalog.pg import partrecord
    final = state.get("material")
    note = ""
    if not final:
        step_mat = ((((k_file or {}).get("parts") or [{}])[0]).get("material") or "").strip()
        if step_mat:
            _dens, word, assumed = partrecord.density_for(step_mat)
            if not assumed:
                final = word
            elif step_mat.lower() != "default":
                note = " (the file says '%s', which is not in the materials list)" % step_mat[:60]
    if not final:
        final = partrecord.materials()["default"]["material"]
    sidecar = os.path.join(state["folder"], "labels.json")
    data = {}
    if os.path.exists(sidecar):
        with open(sidecar, encoding="utf-8") as fh:
            data = json.load(fh)
    key = os.path.basename(state["sources"][i]["path"])
    data[key] = dict(data.get(key) or {}, material=final)
    tmp = sidecar + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, sidecar)
    return final, "Material: %s%s" % (final, note)


def discard(state):
    """A failed new upload leaves nothing behind: only what this upload created is removed -- the sources it stored, their
    keys in the folder's labels.json -- and the upload folder (inside models/uploads only) when it is empty afterwards.
    Another upload sharing the folder keeps its files. Nothing is removed for an upload `into` an existing product."""
    if state.get("into"):
        return
    folder = os.path.realpath(state["folder"])
    if not folder.startswith(os.path.realpath(UPLOADS) + os.sep) or os.path.basename(folder) in ("", "_incoming"):
        raise ValueError("refusing to remove %s" % folder)
    mine = [os.path.realpath(s["path"]) for s in state["sources"] if s.get("stored")]
    for p in mine:
        if os.path.dirname(p) == folder:
            try:
                os.remove(p)
            except OSError:
                pass
    sidecar = os.path.join(folder, "labels.json")
    try:
        with open(sidecar, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and any(os.path.basename(p) in data for p in mine):
            for p in mine:
                data.pop(os.path.basename(p), None)
            if data:
                tmp = sidecar + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2)
                os.replace(tmp, sidecar)
            else:
                os.remove(sidecar)
    except (OSError, ValueError):
        pass
    try:
        os.rmdir(folder)                          # only when empty: another upload may share it
    except OSError:
        pass


def finish(state, added, n_files):
    """Step 3: the description (new products), then the manifest on disk (json.dump, indent 2).
    -> {"ok": True, "manifest": <path>} or {"ok": False, "error": ...} when nothing was added."""
    if not added:
        return {"ok": False, "error": "None of the files contained solid geometry."}
    man = state["man"]
    if not state["into"]:
        n = len(man["parts"])
        man["description"] = "Imported design: %d part%s from %d file%s." % (n, "" if n == 1 else "s",
                                                                          n_files, "" if n_files == 1 else "s")
    mp = _manifest_path(state["pid"])
    with open(mp, "w", encoding="utf-8") as fh:
        json.dump(man, fh, indent=2)
    return {"ok": True, "manifest": mp}


def result_of(state, added, n_parts, n_interfaces, missing):
    return {"ok": True, "product_id": state["pid"], "name": state["title"], "parts": n_parts,
            "interfaces": n_interfaces, "added": added, "not_catalogued": len(missing)}


def add(paths, name=None, into=None, ref=None, move=False, single_part=False, material=None):
    """Generator of (label, state) progress tuples; the last item is ("result", dict). The in-process path, the kernel's
    alone: the same step functions as the `upload.build` recipe, with the reads and the viewer mesh done here."""
    state = prepare(paths, name, into, ref, move, single_part, material)
    if not state["ok"]:
        yield ("result", state); return
    from framework.kernel import io as kio
    added, k_files = 0, []
    for i, src in enumerate(state["sources"]):
        yield ("Reading %s (%d of %d)" % (src["stem"], i + 1, len(paths)), "run")
        try:
            k_file = kernel_file(kio.load(src["path"]))
        except Exception:
            k_file = None
        k_files.append(k_file)
        n_new, labels = entries_kernel(state, i, k_file)
        added += n_new
        yield from labels
        if state.get("rejected"):
            discard(state)
            yield ("result", {"ok": False, "error": state["rejected"]}); return
    if state.get("single_part") and added:
        yield (catalogue_material(state, 0, k_files[0])[1], "done")
    fin = finish(state, added, len(paths))
    if not fin["ok"]:
        discard(state)                            # no geometry: the stored file goes too (no-op for `into`)
        yield ("result", fin); return
    mp = fin["manifest"]
    pid = state["pid"]
    yield ("Measuring parts and finding fits", "run")
    try:
        n_parts, n_interfaces, missing = product.load(mp)
    except Exception as e:
        yield ("result", {"ok": False, "error": "Measuring failed: %s" % str(e)[-300:]}); return
    yield ("Measured %d parts, %d fits and contacts" % (n_parts, n_interfaces), "done")
    yield ("Preparing the 3D view", "run")
    from framework.common import derived
    mesh = derived.mesh_path(pid)
    try:
        from framework.kernel import partview
        from framework.kernel.shapes import product_parts
        partview.write_design(product_parts(mp), mesh, header=pid)
    except Exception as e:
        print("  mesh failed: %s" % e)
    yield ("3D view ready", "done")
    yield ("result", result_of(state, added, n_parts, n_interfaces, missing))


def upload(paths, name=None, into=None, ref=None, move=False):
    """STEP files -> product, as `add()` yields it. With BC_SERVICES=1 (the services studio)
    the files are handed to the orchestrator's `upload.build` recipe (inbox copy, one job, its steps shown in the same
    words as the studio shows them); otherwise `add()` runs in-process, kernel only."""
    from framework.common import cliclient
    if not cliclient.wanted():
        yield from add(paths, name=name, into=into, ref=ref, move=move)
        return
    import time
    from framework.common import jobs, pg
    from framework.common import upload_follow          # the shared follower (nothing of another service)
    inbox = os.path.join(UPLOADS, "_incoming")
    os.makedirs(inbox, exist_ok=True)
    stamp, files = int(time.time() * 1000), []
    for i, p in enumerate(paths):
        base = re.sub(r"[^A-Za-z0-9._+-]+", "_", os.path.basename(p)) or "design.step"
        dst = os.path.join(inbox, "%d_%s" % (stamp + i, base))
        (shutil.move if move else shutil.copyfile)(p, dst)
        files.append("models/uploads/_incoming/" + os.path.basename(dst))
    payload = {"files": files, "move": True}
    for key, val in (("name", name), ("into", into), ("ref", ref)):
        if val:
            payload[key] = val
    stems = [re.sub(r"^\d{10,}_", "", re.sub(r"\.(step|stp)$", "", os.path.basename(f), flags=re.I)) for f in files]
    con = pg.connect(write=True)
    try:
        correlation_id = os.environ.get("BC_CORRELATION_ID") or None
        job_id = jobs.submit(con, "upload.build", payload, sender=os.environ.get("BC_SENDER") or ("designer" if correlation_id else "cli"),
                             correlation_id=correlation_id, parent_job_id=os.environ.get("BC_PARENT_JOB_ID") or None)
    finally:
        con.close()
    for kind, *rest in upload_follow.follow(job_id, stems):
        if kind == "status":
            yield (rest[0], rest[1])
        else:
            yield ("result", rest[0])


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__); return 2
    paths, name, into, ref = [], None, None, None
    it = iter(argv)
    for a in it:
        if a == "--name": name = next(it)
        elif a == "--into": into = next(it)
        elif a == "--ref": ref = next(it)
        else: paths.append(a)
    bad = [p for p in paths if not os.path.exists(p) or not re.search(r"\.(step|stp)$", p, re.I)]
    if bad or not paths:
        print("not a STEP file: %s" % (bad or "none given")); return 2
    result = None
    for label, state in upload(paths, name, into, ref):
        if label == "result":
            result = state
        else:
            print("  [%s] %s" % (state, label), flush=True)
    print("RESULT " + json.dumps(result))
    return 0 if result and result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
