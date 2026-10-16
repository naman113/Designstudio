"""The hand-over checks for drawings.

The geometry service makes sheets under work/drawings/<pid>/<job_id>/; the catalogue is the only writer of the files the
studio serves. Before it copies anything it re-checks, here: ids by pattern, the job folder (realpath inside
work/drawings/<pid>/), file names by pattern, signatures and size caps, the sha256 of every file against the report's, and
the report's own read-back result (wrong, unexplained, missing and words all zero). One module serves both publish types:
`catalogue.publish_drawings` (the studio's own sheets) and `catalogue.publish_drawing` (one sheet a build session made,
`check_session_sheet` at the end): one set of checks, one writer.

Each check raises `DrawingRefused(detail)`; the handler maps it to `ServiceError("drawing_refused", ...)`.
"""
import hashlib
import json
import os
import re

from framework.common import derived

ROOT = derived.ROOT
DRAWINGS_WORK = os.path.join(ROOT, "work", "drawings")
REF_RX = re.compile(r"^[A-Za-z0-9_]+$")
FP_RX = re.compile(r"^[0-9a-f]{64}$")
SET_RX = re.compile(r"^[0-9a-f]{12}$")
PNG_SIG = b"\x89PNG\r\n\x1a\n"
CAP = {"png": 20 * 1024 * 1024, "pdf": 10 * 1024 * 1024}
SIG = {"png": PNG_SIG, "pdf": b"%PDF-"}
KINDS = ("views", "assembly", "part")


class DrawingRefused(Exception):
    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


def check_ids(product_id, ref=None):
    if not isinstance(product_id, str) or not derived.PID_RX.match(product_id):
        raise DrawingRefused("product_id does not match the id pattern")
    if ref is not None and (not isinstance(ref, str) or not REF_RX.match(ref)):
        raise DrawingRefused("ref does not match the ref pattern")
    return product_id


def check_job_dir(product_id, job_dir):
    """The hand-over folder as an absolute path: it must resolve (realpath) to a folder INSIDE work/drawings/<pid>/."""
    if not isinstance(job_dir, str) or not job_dir or len(job_dir) > 1000 or "\x00" in job_dir:
        raise DrawingRefused("job_dir is not a path")
    full = job_dir if os.path.isabs(job_dir) else os.path.join(ROOT, job_dir)
    base = os.path.realpath(os.path.join(DRAWINGS_WORK, product_id))
    real = os.path.realpath(full)
    if os.path.normcase(os.path.dirname(real)) != os.path.normcase(base):
        raise DrawingRefused("job_dir is not inside work/drawings/<product>/")
    if not os.path.isdir(real):
        raise DrawingRefused("job_dir does not exist")
    return real


def file_kind(name):
    """(kind, ref, ext) from a sheet's file name, or None: views.png, assembly.pdf, part-<REF>.pdf."""
    if name == "views.png":
        return "views", None, "png"
    if name == "assembly.pdf":
        return "assembly", None, "pdf"
    m = re.match(r"^part-([A-Za-z0-9_]+)\.pdf$", name)
    if m:
        return "part", m.group(1), "pdf"
    return None


def check_file(path, ext):
    """The sha256 of a sheet file after its signature and size cap are checked."""
    if ext not in CAP:
        raise DrawingRefused("unknown file kind")
    if not os.path.isfile(path) or os.path.islink(path):
        raise DrawingRefused("a sheet file is missing")
    size = os.path.getsize(path)
    if size > CAP[ext] or size < len(SIG[ext]):
        raise DrawingRefused("a sheet file has a wrong size")
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        head = fh.read(len(SIG[ext]))
        if head != SIG[ext]:
            raise DrawingRefused("a sheet file does not start like its kind")
        h.update(head)
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_report_sheet(sheet):
    """The read-back result the geometry service wrote for a sheet: nothing wrong, nothing unexplained, nothing missing,
    no word the studio may not show."""
    for key in ("wrong", "unexplained", "missing"):
        v = sheet.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v != 0:
            raise DrawingRefused("the sheet's %s count is not 0" % key)
    if sheet.get("words"):
        raise DrawingRefused("the sheet carries words the studio may not show")


# ---- a session's own sheet ----------------------------------------------------------------------------------------------
# `catalogue.publish_drawing` hands one PDF a build made inside its session folder over to the studio's static/files/.
# Whatever sent the request may have checked it already; the catalogue never trusts that and repeats every check here.
# Read-only reads of the session folders.
SESSIONS = os.path.join(ROOT, "work", "sessions")
GUARD = os.path.join(ROOT, "work", "guard")
SID_RX = re.compile(r"^[a-z0-9-]{1,120}$")


def _inside(path, folder):
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(folder)]) == os.path.normcase(folder)
    except ValueError:
        return False


def _builds(sid):
    path = os.path.join(GUARD, sid, "builds.jsonl")
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def check_session_sheet(product_id, ref, sid, pdf):
    """-> (absolute path of the sheet, its sha256) after every check: ids by pattern, the design has a manifest, the file
    resolves (realpath) inside work/sessions/<sid>/, is a real file under the size cap that starts with %PDF-, and the LATEST
    work/guard/<sid>/builds.jsonl record that names it (relative to the session) says ok: true."""
    check_ids(product_id, ref)
    if ref is None:
        raise DrawingRefused("ref is missing")
    if not isinstance(sid, str) or not SID_RX.match(sid):
        raise DrawingRefused("sid does not match the session pattern")
    if not os.path.isfile(os.path.join(derived.PRODUCTS, product_id + ".json")):
        raise DrawingRefused("the design has no manifest")
    if not isinstance(pdf, str) or not pdf or len(pdf) > 1000 or "\x00" in pdf or os.path.isabs(pdf):
        raise DrawingRefused("pdf is not a root-relative path")
    session = os.path.realpath(os.path.join(SESSIONS, sid))
    if not os.path.isdir(session):
        raise DrawingRefused("there is no such session")
    real = os.path.realpath(os.path.join(ROOT, pdf))
    if real == session or not _inside(real, session):
        raise DrawingRefused("the sheet is not inside the session folder")
    if not os.path.isfile(real):
        raise DrawingRefused("the sheet does not exist")
    sha = check_file(real, "pdf")            # not a link, size cap, %PDF- signature
    rel = os.path.relpath(real, session).replace(os.sep, "/")
    found = None
    for rec in _builds(sid):
        for d in rec.get("drawings") or []:
            if isinstance(d, dict) and d.get("file") == rel:
                found = rec
    if found is None or found.get("ok") is not True:
        raise DrawingRefused("no successful build run reported the sheet")
    return real, sha
