"""Measuring and placing parts from FreeCAD's exact BREP dump.

One implementation of the BREP-dump reader, kept in the kernel so the geometry service needs no FreeCAD. Nothing here
runs FreeCAD: the dump (`work/engines/brep/<sha12>/_labels.json` plus one `<object>.brep` per part) is made by a
FreeCAD pass outside this package, and a
missing dump is an error here (`FileNotFoundError("no BREP dump ...")`), never a FreeCAD run.

The kernel cannot read FreeCAD documents. It measures the exact shapes FreeCAD dumps as BREP during its own measure
pass, the same exact-BREP route the reference measurements were made from.
"""
import json
import os
import time

from framework.common import config
from framework.common.files import sha256

from framework.common.brep_paths import BREP_CACHE, STEP_EXT, is_step, is_brep, brep_dir_for, write_brep_labels  # noqa: E402,F401  (kept in framework.common)


def _read_brep(p):
    from build123d import import_brep
    return import_brep(p).wrapped


def candidate_shape(path, ref, cache):
    """The shape one signature candidate names, or None. `path` is a STEP file (its part `ref` is
    looked up) or one part's BREP dump file (`<dump dir>/<object>.brep`: that part's exact shape; `ref` is the object
    name it was dumped under). Any other file, or a missing one, is None. `cache` (a dict the caller keeps for a whole
    run) loads each file at most once. One rule for `pg/signatures.resolve_shape` (the offline path) and the geometry
    service's `geometry.signature` (ops.signature)."""
    norm = os.path.normpath(path)
    if norm not in cache:
        if not os.path.isfile(norm):
            cache[norm] = None
        elif is_step(norm):
            from framework.kernel import io as kio
            cache[norm] = kio.load(norm)
        elif is_brep(norm):
            cache[norm] = _read_brep(norm)
        else:
            cache[norm] = None
    got = cache[norm]
    if got is None or is_brep(norm):
        return got
    for q in got.parts:
        if q.ref == ref:
            return q.shape
    return None


def extract_dump(path, d, sha):
    """Measure every part of the .FCStd `path` from its BREP dump in folder `d` (`sha` = the file's sha256).
    -> ingest.extract()'s dict shape (+ "ms"). Raises FileNotFoundError("no BREP dump ...") when `d/_labels.json` is
    missing: this module never runs FreeCAD."""
    from framework.kernel import KERNEL_VERSION
    from framework.kernel import measure as kmeasure
    meta_path = os.path.join(d, "_labels.json")
    if not os.path.exists(meta_path):
        raise FileNotFoundError("no BREP dump for %s (expected %s)" % (path, meta_path))
    t0 = time.time()      # the FreeCAD dump is FreeCAD's time, not the kernel's
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    parts, skipped = [], []
    for obj, m in meta["objects"].items():
        bp = os.path.join(d, obj + ".brep")
        if not os.path.exists(bp):
            skipped.append({"object": obj, "label": m.get("label"), "reason": "no BREP dump"})
            continue
        rec = kmeasure.measure(_read_brep(bp), obj, label=m.get("label") or obj,
                               type_id=m.get("type_id") or "Part::Feature", material=m.get("material") or "Default")
        if rec is None:
            skipped.append({"object": obj, "label": m.get("label"), "reason": "no solid geometry"})
            continue
        parts.append(rec)
    return {"source_file": os.path.abspath(path), "source_doc": meta.get("source_doc"), "checksum_sha256": sha,
            "object_count": meta.get("object_count"), "part_count": len(parts), "parts": parts,
            "skipped": skipped, "kernel_version": KERNEL_VERSION, "log": ["measured from FreeCAD BREP dump %s" % d],
            "ms": (time.time() - t0) * 1000.0}


def product_parts(manifest_path, dump_for):
    """shapes.product_parts, but an entry whose "file" is a .FCStd resolves to the FreeCAD BREP dump of that object.
    `dump_for(path)` returns the dump folder of a .FCStd (a FreeCAD pass may make it first; the geometry side only
    finds it)."""
    with open(manifest_path, encoding="utf-8") as fh:
        m = json.load(fh)
    if all(is_step(e["file"]) for e in m["parts"]):
        from framework.kernel.shapes import product_parts as pp
        return pp(manifest_path)
    from framework.kernel.shapes import _load_cached
    from framework.kernel import io as kio
    base = os.path.dirname(os.path.abspath(manifest_path))
    out, dumps = [], {}
    for e in m["parts"]:
        path = e["file"] if os.path.isabs(e["file"]) else os.path.join(base, e["file"])
        placement = e.get("placement")
        if is_step(path):
            occ = [s for r, s in kio.all_shapes(_load_cached(path)) if r == e["object"]]
        else:
            if path not in dumps:
                dumps[path] = dump_for(path)
            bp = os.path.join(dumps[path], e["object"] + ".brep")
            occ = [_read_brep(bp)] if os.path.exists(bp) else []
        if not occ:
            print("  WARNING: object %r not found in %s" % (e["object"], path))
            continue
        for shape in occ:
            out.append((e["ref"], kio.placed(shape, placement) if placement else shape))
    return out
