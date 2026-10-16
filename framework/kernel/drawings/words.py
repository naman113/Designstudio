"""The word guard for sheet text: it keeps tool names, file names and paths off a sheet; the number checks live in check.py.

Names the user chose (design and part names) are scanned separately by the callers and reported as `name_words` (a
warning), never as a refusal.
"""
import re

# Kind -> literal words/phrases (matched whole-word/whole-phrase, case-insensitive).
BANNED = {
    "engine": [
        "FreeCAD", "build123d", "OpenCASCADE", "OCC", "OCP", "cadgen",
        "Python",
    ],
    "word": [
        "script", "subagent", "sub-agent", "Bash", "tool call", "run_build",
        "catalog.py", "framework/", "work/sessions",
    ],
    "internal": [
        "for the developers", "note for the developers", "the developers", "developer note",
        "traceback", "stack trace", "internal error",
    ],
}
_EXT_RE = re.compile(r"\.(step|stp|fcstd|py|json|glb|stl|dxf|dwg|ifc|pdf|csv)\b", re.IGNORECASE)
_WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"']+")
_POSIX_PATH_RE = re.compile(r"(?<![\w./])/[a-z_]+/[a-z_]+(?:/[a-z_]+)*")


def find(text):
    """[(kind, word)] for every banned word, extension or path in `text`, in order of appearance."""
    if not text:
        return []
    hits = []
    for kind, words in BANNED.items():
        for w in words:
            for m in re.finditer(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", text, re.IGNORECASE):
                hits.append((m.start(), m.end(), kind, m.group(0)))
    for rx, kind in ((_EXT_RE, "extension"), (_WIN_PATH_RE, "path"), (_POSIX_PATH_RE, "path")):
        for m in rx.finditer(text):
            hits.append((m.start(), m.end(), kind, m.group(0)))
    hits.sort(key=lambda h: h[0])
    seen, out = set(), []
    for s, e, kind, word in hits:
        if (s, e) not in seen:
            seen.add((s, e))
            out.append((kind, word))
    return out
