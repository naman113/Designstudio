"""The fingerprint of a design's drawings Stdlib only: the catalogue, the gateway and the
geometry worker import it and must compute the same value.

    sha256( b"drawings-v<N>\n" + the sha of each RULE_FILES entry + the manifest's bytes
            + for each distinct `file` entry of the manifest, in manifest order: its sha, or b"missing:<entry>" )

An entry is resolved exactly as framework/kernel/brep_dump.product_parts resolves it (relative to the manifest's folder).
File hashes are memoised by (normcase(abspath), size, mtime_ns), so a 68 MB file is hashed once per change (risk R8).
The folder of a published set is named `set_id(fp)` (the first 12 hex digits).
"""
import hashlib
import json
import os
import threading

DRAW_RULES_VERSION = 3      # bump when a drawing rule or the drawing engine changes (2: part drawings, first angle, the bought-in list; 3: assembly drawings)
RULE_FILES = ("framework/catalog/bought_in.json",)   # rule files whose bytes are hashed in (relative to the project root)

_SHA = {}
_LOCK = threading.Lock()
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def file_sha(path):
    """sha256 hex of a file, memoised by (path, size, mtime_ns); None when it does not exist."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.normcase(os.path.abspath(path)), st.st_size, st.st_mtime_ns)
    with _LOCK:
        hit = _SHA.get(key)
    if hit:
        return hit
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    with _LOCK:
        for k in [k for k in _SHA if k[0] == key[0]]:
            del _SHA[k]
        _SHA[key] = h.hexdigest()
    return _SHA[key]


def fingerprint(manifest_path):
    manifest_path = os.path.abspath(manifest_path)
    with open(manifest_path, "rb") as fh:
        raw = fh.read()
    m = json.loads(raw.decode("utf-8"))
    h = hashlib.sha256()
    h.update(b"drawings-v%d\n" % DRAW_RULES_VERSION)
    for rf in RULE_FILES:
        h.update((file_sha(os.path.join(_ROOT, rf)) or "missing:" + rf).encode() + b"\n")
    h.update(raw)
    base = os.path.dirname(manifest_path)
    seen = set()
    for e in m.get("parts", []):
        entry = e.get("file")
        if not entry or entry in seen:
            continue
        seen.add(entry)
        path = entry if os.path.isabs(entry) else os.path.join(base, entry)
        h.update((file_sha(path) or "missing:" + entry).encode("utf-8") + b"\n")
    return h.hexdigest()


def set_id(fp):
    return fp[:12]
