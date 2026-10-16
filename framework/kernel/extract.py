"""Kernel measure of one STEP document: the dict framework/catalog/ingest.py's store() takes."""
import json
import os

from framework.common.files import sha256
from framework.kernel import KERNEL_VERSION
from framework.kernel import io as kio
from framework.kernel import measure as kmeasure


def _load_sidecar(path):
    """labels.json alongside `path` (STEP metadata sidecar): FreeCAD
    Label/TypeId/material per object, keyed by this file's basename -- inputs, not
    measurements (STEP itself carries the object NAME, not the FreeCAD label the
    family rules match). A file-level "material" key is the material chosen at
    upload (catalogue part); it ranks below an object-level material and above the
    STEP file's own material. {} when there is no sidecar file, or no entry for this
    basename; kernel.measure's own defaults apply either way."""
    sidecar_path = os.path.join(os.path.dirname(os.path.abspath(path)), "labels.json")
    if not os.path.exists(sidecar_path):
        return {}
    with open(sidecar_path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get(os.path.basename(path), {})


def extract(path):
    """Load `path` through the kernel and measure every part -- the in-process
    replacement for the old external-process extract pass. Returns the same dict shape
    store() has always expected (source_file, source_doc, checksum_sha256,
    object_count, part_count, parts, skipped, plus kernel_version in place of the
    old freecad_version)."""
    model = kio.load(path)
    sidecar = _load_sidecar(path)
    sidecar_objects = sidecar.get("objects", {})

    parts, skipped = [], []
    for part in model.parts:
        meta = sidecar_objects.get(part.ref, {})
        rec = kmeasure.measure(part.shape, part.ref, label=meta.get("label", part.label),
                                type_id=meta.get("type_id", "Part::Feature"),
                                material=(meta.get("material") or sidecar.get("material")
                                          or part.material or "Default"))
        if rec is None:
            skipped.append({"object": part.ref, "label": part.label, "reason": "no solid geometry"})
            continue
        parts.append(rec)

    return {
        "source_file": os.path.abspath(path),
        "source_doc": sidecar.get("doc_name") or model.name,
        "checksum_sha256": sha256(path),
        "object_count": len(model.parts),
        "part_count": len(parts),
        "parts": parts,
        "skipped": skipped,
        "kernel_version": KERNEL_VERSION,
        "log": model.log,
    }
