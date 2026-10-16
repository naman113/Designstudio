"""Where a document's BREP dump lives and how it is labelled (shared with framework/kernel/brep_dump.py).

Pure Python, no kernel import: the engines service (FreeCAD only) writes the dump of a .FCStd next to its measure
pass, and framework/kernel/brep_dump.py re-imports these names (one implementation). Importing `framework.kernel` loads
build123d, which the engines service must never do.
"""
import json
import os

from framework.common import config
from framework.common.files import sha256

BREP_CACHE = os.path.join(config.ROOT, "work", "engines", "brep")
STEP_EXT = (".step", ".stp")


def is_step(path):
    return path.lower().endswith(STEP_EXT)


def is_brep(path):
    return path.lower().endswith(".brep")


def brep_dir_for(path, sha=None):
    return os.path.join(BREP_CACHE, (sha or sha256(path))[:12])


def dump_file(sha, obj):
    """One part's dump file: `<cache>/<sha[:12]>/<object>.brep` (a catalogue document's `doc_id` is its sha256[:12],
    so the catalogue finds a part's exact shape without reading the native file). None for an unusable object name."""
    if not sha or not obj or obj in (".", "..") or any(c in obj for c in "\\/:\x00"):
        return None
    return os.path.join(BREP_CACHE, sha[:12], obj + ".brep")


def in_cache(path):
    """True when `path` lies inside the dump cache (the geometry service reads a .brep candidate only from there)."""
    full = os.path.normcase(os.path.realpath(path))
    return full.startswith(os.path.normcase(os.path.realpath(BREP_CACHE)) + os.sep)


def write_brep_labels(d, fc_data):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "_labels.json"), "w", encoding="utf-8") as fh:
        json.dump({"source_doc": fc_data.get("source_doc"), "object_count": fc_data.get("object_count"),
                   "objects": {p["object"]: {"label": p.get("label"), "type_id": p.get("type_id"),
                                             "material": p.get("material")} for p in fc_data["parts"]}}, fh, indent=1)
