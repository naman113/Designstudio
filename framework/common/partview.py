"""Where a part's own 3D view and its STEP file are kept. Pure Python, no kernel: the gateway serves a
fresh file from here without a job, the geometry service writes here (framework/kernel/ops.py part_view, part_step), and
the warm-up command (framework/catalog/partviews.py) fills it ahead.

    work/partviews/<doc_id>/<object>.<key>.glb    the part's view (binary glTF, mm, Z up, the catalogue's frame)
    work/partviews/<doc_id>/<object>.<key>.json   its facts: triangles, the box, the exact extent, the mesh rule, ms
    work/partviews/<doc_id>/<object>.<key>.step   the part alone as STEP (its own key)
    work/partviews/_split/<sha12>/<object>.brep   every part of a STEP file read once for one of them (the next part of the
                                                  same file then loads only its own shape)

The key is the first 16 hex digits of sha256(kind | rule | each source's file sha256 and object): a new mesh rule, a
changed file or another source makes a new key, so a stale file is never served (the old one is removed when the new
one is written). The sources come from the catalogue (`catalogue.q.partshape`, signatures.shape_sources).
"""
import hashlib
import os
import re
import threading

from framework.common import config
from framework.common.files import sha256

FOLDER = os.path.join(config.ROOT, "work", "partviews")
SPLIT = os.path.join(FOLDER, "_split")
VIEW_RULE = 1            # bump when the view's mesh rule changes (framework/kernel/partview.py)
STEP_RULE = 1            # bump when the part STEP writer changes
PART_RX = re.compile(r"^([0-9a-f]{12}):([A-Za-z0-9_][A-Za-z0-9_.+-]{0,199})$")
KEY_RX = re.compile(r"^[0-9a-f]{16}$")
NO_SHAPE = "This part has no 3D shape kept, so it cannot be shown or downloaded."

_SHA = {}                # (path, size, mtime_ns) -> sha256: a file is hashed once per process while it is unchanged
_SHA_LOCK = threading.Lock()


def split_part(part_id):
    """(doc_id, object) of a catalogue part id, or None when it is not one (the paths below are built from it)."""
    m = PART_RX.match(part_id or "") if isinstance(part_id, str) else None
    if not m or m.group(2) in (".", ".."):
        return None
    return m.group(1), m.group(2)


def file_sha(path):
    st = os.stat(path)
    k = (os.path.normcase(os.path.abspath(path)), st.st_size, st.st_mtime_ns)
    with _SHA_LOCK:
        got = _SHA.get(k)
    if got is None:
        got = sha256(path)
        with _SHA_LOCK:
            if len(_SHA) > 5000:
                _SHA.clear()
            _SHA[k] = got
    return got


def key(kind, sources):
    """The cache key of a part's `kind` ("view" | "step") file made from `sources` [[path, object], ...]."""
    rule = VIEW_RULE if kind == "view" else STEP_RULE
    h = hashlib.sha256(("%s|%d" % (kind, rule)).encode())
    for path, obj in sources:
        h.update(("|%s|%s" % (file_sha(path), obj)).encode())
    return h.hexdigest()[:16]


def path(part_id, k, ext):
    """work/partviews/<doc_id>/<object>.<key>.<ext>; None for a bad part id or key."""
    p = split_part(part_id)
    if p is None or not KEY_RX.match(k or "") or ext not in ("glb", "json", "step"):
        return None
    return os.path.join(FOLDER, p[0], "%s.%s.%s" % (p[1], k, ext))


def fresh(part_id, k, ext):
    """The file's path when it is there (made with this key), else None."""
    p = path(part_id, k, ext)
    return p if p and os.path.isfile(p) else None


def drop_others(part_id, k, ext):
    """Remove the part's files of this kind made with another key (a new rule or a changed source)."""
    p = split_part(part_id)
    if p is None:
        return 0
    folder, n = os.path.join(FOLDER, p[0]), 0
    exts = ("glb", "json") if ext in ("glb", "json") else (ext,)
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        parts = name.rsplit(".", 2)
        if len(parts) == 3 and parts[0] == p[1] and parts[2] in exts and parts[1] != k and KEY_RX.match(parts[1]):
            try:
                os.remove(os.path.join(folder, name))
                n += 1
            except OSError:
                pass
    return n


def split_file(step_sha, obj):
    """work/partviews/_split/<sha12>/<object>.brep for one part of a STEP file (None for an unusable name)."""
    if not step_sha or not obj or obj in (".", "..") or any(c in obj for c in "\\/:*?\"<>|\x00"):
        return None
    return os.path.join(SPLIT, step_sha[:12], obj + ".brep")


def in_split(p):
    full = os.path.normcase(os.path.realpath(p))
    return full.startswith(os.path.normcase(os.path.realpath(SPLIT)) + os.sep)


_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def download_name(label, ext="step"):
    """The file name a person sees for a part's download: its label (characters a file name cannot hold replaced by
    "-"), never an internal id. "part.step" when the label is empty."""
    name = _BAD_NAME.sub("-", (label or "").strip()).strip(" .-") or "part"
    return "%s.%s" % (name[:120], ext)
