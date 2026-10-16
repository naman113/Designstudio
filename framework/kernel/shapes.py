"""product_parts(manifest_path) -> list[(ref, shape)] -- a product manifest's parts,
resolved through the kernel (framework.kernel.io), in manifest-declaration order.
No OCP/build123d import here: shapes pass through as opaque TopoDS_Shape
objects the kernel produced; this module never inspects their geometry, only
routes them to framework.kernel.mesh (the web app's viewer) and
framework.kernel.io.write_step (STEP download).

    from framework.kernel.shapes import product_parts
    parts = product_parts("framework/products/bracket-assembly.json")
"""
import json
import os

from framework.kernel import io as kio

_MODEL_CACHE = {}   # (abs path, mtime) -> kio.Model, so a product spanning several
                     # manifests that share a file (or repeated calls in one process)
                     # only pays the STEP load once per file per on-disk version.


def _load_cached(path):
    abs_path = os.path.abspath(path)
    mtime = os.path.getmtime(abs_path)
    key = (abs_path, mtime)
    model = _MODEL_CACHE.get(key)
    if model is None:
        model = kio.load(abs_path)
        _MODEL_CACHE[key] = model
    return model


def product_parts(manifest_path):
    """Resolve a manifest's "parts" list into (ref, shape) tuples, one tuple per
    occurrence: a manifest entry whose object has more than one location in its
    STEP file (a repeated-placement part) yields more than one tuple, consecutive
    and sharing the manifest's own ref -- the same convention kernel.mesh.write_obj
    and kernel.io.write_step use to treat consecutive same-ref tuples as one part.
    An entry with a "placement" (4x4 row-major matrix) gets kernel.io.placed applied
    on top of the part's own model-space shape. An entry whose object is not found
    in its file is skipped with a printed warning, never raised -- one bad manifest
    row must not take down the whole product's mesh or BOM."""
    with open(manifest_path, encoding="utf-8") as fh:
        m = json.load(fh)
    base = os.path.dirname(os.path.abspath(manifest_path))

    out = []
    for e in m["parts"]:
        path = e["file"] if os.path.isabs(e["file"]) else os.path.join(base, e["file"])
        model = _load_cached(path)
        occurrences = [shape for ref, shape in kio.all_shapes(model) if ref == e["object"]]
        if not occurrences:
            print("  WARNING: object %r not found in %s" % (e["object"], path))
            continue
        placement = e.get("placement")
        for shape in occurrences:
            out.append((e["ref"], kio.placed(shape, placement) if placement else shape))
    return out
