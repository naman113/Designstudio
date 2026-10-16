"""Stored and received paths that open on this machine.

The catalogue stores `documents.path` as the host that ingested the file spelled it: absolute, with a drive letter, backslashes
and lower case (`c:\\data\\...\\framework\\...`). A job payload may carry the same form. In a container
(Linux, a case-sensitive disk, the copy mounted at /app) such a path does not open. `local(p)` returns the path as this process
can open it; it never rewrites anything that is stored.

    BC_HOST_ROOT   the folder of the work copy on the machine that wrote the paths (any letter case, either slash); a path under
                   it maps to this process's ROOT
    BC_PATH_MAP    more prefixes, `FROM=TO` pairs separated by `;` (FreeCAD's examples, the parts catalogue, ...); a path under
                   FROM maps to TO
    (neither set)  on Windows `local(p)` is `p`, exactly; elsewhere only the case-insensitive step below applies

Then, on a case-sensitive disk (not Windows, not macOS), each component that does not exist as written is looked up
case-insensitively in its folder (one `listdir` per folder, cached for the process). A path that still does not exist is returned
as mapped, so the caller's own "not found" answer stays what it was.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_CASE_SENSITIVE = os.name != "nt" and sys.platform != "darwin"
_DIRS = {}


def _norm(p):
    return p.replace("\\", "/")


def _usable_prefix(frm):
    """A prefix worth mapping: not `/`, not a bare drive (`c:`), at least 3 characters once normalised."""
    f = frm.rstrip("/")
    if len(f) < 3 or (len(f) == 2 and f[1] == ":"):
        return False
    return True


def _maps():
    """[(from, to)] in order: BC_HOST_ROOT -> ROOT first, then BC_PATH_MAP."""
    out = []
    host = os.environ.get("BC_HOST_ROOT")
    if host and _usable_prefix(_norm(host)):
        out.append((_norm(host).rstrip("/"), ROOT))
    for pair in (os.environ.get("BC_PATH_MAP") or "").split(";"):
        frm, sep, to = pair.partition("=")
        if sep and frm.strip() and to.strip() and _usable_prefix(_norm(frm.strip())):
            out.append((_norm(frm.strip()).rstrip("/"), to.strip()))
    return out


def _map_prefix(p):
    """-> (mapped path, True) when p starts with one of the prefixes (any letter case), else (p, False). The mapped path is
    normalised and kept only when it stays under its target (a `..` cannot climb out), else p comes back unchanged."""
    n = _norm(p)
    low = n.lower()
    for frm, to in _maps():
        f = frm.lower()
        if low == f or low.startswith(f + "/"):
            rest = n[len(frm):].lstrip("/")
            mapped = os.path.normpath(os.path.join(to, *rest.split("/")) if rest else to)
            base = os.path.normpath(to)
            if mapped == base or mapped.startswith(base.rstrip(os.sep) + os.sep):
                return mapped, True
            return p, False
    return p, False


def _listdir(folder, fresh=False):
    """{lower name: [names]} of a folder, listed once and cached; `fresh` lists it again. A failed listing is never cached."""
    names = None if fresh else _DIRS.get(folder)
    if names is None:
        try:
            names = {}
            for n in os.listdir(folder):
                names.setdefault(n.lower(), []).append(n)
        except OSError:
            _DIRS.pop(folder, None)
            return {}
        _DIRS[folder] = names
    return names


def _find(folder, part):
    """-> the one existing spelling of `part` in `folder` (listed again once on a miss), or None (absent, or two names that
    differ only in letter case: the lookup is ambiguous and counts as not found)."""
    low = part.lower()
    for fresh in (False, True):
        found = _listdir(folder, fresh).get(low)
        if found:
            return found[0] if len(found) == 1 else None
    return None


def _fold_case(p):
    """The existing spelling of `p` on a case-sensitive disk (each missing component matched case-insensitively), or None."""
    cur = os.sep if os.path.isabs(p) else ""
    for part in [x for x in _norm(p).split("/") if x]:
        nxt = os.path.join(cur, part) if cur else part
        if not os.path.lexists(nxt):
            found = _find(cur or ".", part)
            if found is None:
                return None
            nxt = os.path.join(cur, found) if cur else found
        cur = nxt
    return cur or None


def basename(p):
    """The last component of a stored path, whichever separator wrote it. `os.path.basename` splits only on this OS's separator, so a
    Windows path stored by the catalogue comes back whole on Linux (the product page's `source`)."""
    if not isinstance(p, str):
        return p
    return _norm(p).rstrip("/").rsplit("/", 1)[-1]


def local(p):
    """`p` as this process can open it (see the module docstring). Non-strings, empty strings and relative paths come back unchanged."""
    if not isinstance(p, str) or not p:
        return p
    mapped, hit = _map_prefix(p)
    if not hit and not _CASE_SENSITIVE:
        return p                                   # Windows / macOS with nothing to map: the same string
    if hit and _CASE_SENSITIVE:
        mapped = mapped.replace("\\", "/")
    if not os.path.isabs(mapped):
        return mapped if hit else p
    if os.path.lexists(mapped):
        return mapped
    if _CASE_SENSITIVE:
        found = _fold_case(mapped)
        if found:
            return found
    return mapped
