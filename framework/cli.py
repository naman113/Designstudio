"""buildcore: the command line over the studio's own web routes (the same functions as the web UI).

Usage:
  buildcore [--url URL] drawing (FILE | --id PID) [-o DIR] [--keep] [--timeout S]   dimensioned drawing sheets
  buildcore [--url URL] bom (FILE | --id PID) [--format json|csv] [-o DIR] [--keep]  bill of materials
  buildcore [--url URL] glb (FILE | --id PID) [-o DIR] [--keep]                     3D model for the browser (glTF binary)
  buildcore [--url URL] add FILE [--material M] [--name N] [--json]                 add one part to the catalogue
  buildcore [--url URL] find [--family F] [--material M] [--bore MIN:MAX] ... [--json] search the catalogue

The gateway is http://127.0.0.1:8800 unless --url or BUILDCORE_URL says otherwise. drawing / bom / glb on a STEP FILE open
it as a temporary design (as Open assembly does), write the outputs to ./buildcore-out/<name>/ (or -o DIR) and remove the
temporary design again through the trash, as the UI does, unless --keep. With --id PID they work on a design that is already
there; nothing is uploaded or removed.
Exit codes: 0 done, 1 the gateway refused or the work failed, 2 bad arguments, 3 the gateway is not reachable.
Standard library only: runs from a clone with plain python3 (scripts/buildcore) and inside the image (buildcore).
"""
import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8800"
RANGE_FILTERS = ("bore", "od", "length", "width", "height", "volume", "mass", "holes")
NO_ANSWER = "the upload ended without an answer"


class Unreachable(Exception):
    """The gateway did not answer at all (connection refused, no route, time-out before any answer)."""


class Refused(Exception):
    """The gateway answered with an error status; str() is its error sentence."""

    def __init__(self, status, text):
        super().__init__(text)
        self.status = status


def base_url(url=None):
    return (url or os.environ.get("BUILDCORE_URL") or DEFAULT_URL).rstrip("/")


def request(method, url, body=None, headers=None, timeout=300):
    """-> (status, body bytes). HTTP error statuses are returned, not raised; Unreachable when nothing answers."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, ConnectionError) as e:
        raise Unreachable(str(getattr(e, "reason", e)))
    except TimeoutError:
        raise Refused(0, "the gateway did not finish in %d s" % timeout)


def _error_text(status, data):
    try:
        err = json.loads(data).get("error")
    except (ValueError, AttributeError):
        err = None
    return err or (data.decode("utf-8", "replace").strip()[:200] if data else "") or "the gateway answered %d" % status


def _ok(status, data):
    if status >= 400:
        raise Refused(status, _error_text(status, data))
    return data


def get_json(base, path):
    return json.loads(_ok(*request("GET", base + path)) or b"null")


def post_json(base, path, payload, timeout=300):
    return json.loads(_ok(*request("POST", base + path, json.dumps(payload).encode(),
                                   {"Content-Type": "application/json"}, timeout)) or b"null")


def upload(base, path, single_part=False, material=None, name=None, timeout=900):
    """Add part (single_part) / Open assembly: /api/upload/file then /api/upload/build -> the `done` event dict."""
    with open(path, "rb") as fh:
        body = fh.read()
    tok = json.loads(_ok(*request("POST", "%s/api/upload/file?name=%s" % (base, urllib.parse.quote(os.path.basename(str(path)))),
                                  body, {"Content-Type": "application/octet-stream"}, 120)))
    payload = {"tokens": [tok["token"]], "name": name, "single_part": bool(single_part), "material": material}
    req = urllib.request.Request(base + "/api/upload/build", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    event = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event == "done":
                    return json.loads(line[5:])
    except urllib.error.HTTPError as e:
        raise Refused(e.code, _error_text(e.code, e.read()))
    except (urllib.error.URLError, ConnectionError) as e:
        raise Unreachable(str(getattr(e, "reason", e)))
    except TimeoutError:
        raise Refused(0, "the gateway did not finish in %d s" % timeout)
    return {"ok": False, "error": NO_ANSWER}


def products(base):
    return get_json(base, "/api/products")


def trash_items(base):
    return get_json(base, "/api/trash")


def remove(base, pid, wait_s=300):
    """Move the design to the trash, then erase it for good (retried every 2 s while an upload's trailing steps run: 409).
    -> True when erased."""
    q = urllib.parse.quote(pid, safe="")
    status, _ = request("DELETE", "%s/api/product/%s" % (base, q))
    if status >= 400 and status != 404:
        return False
    end = time.time() + wait_s
    while True:
        status, _ = request("DELETE", "%s/api/trash/mechanical/%s" % (base, q), timeout=1000)
        if status != 409 or time.time() > end:
            return status < 400
        time.sleep(2)


def drawings(base, pid, timeout=900):
    """Current drawings of a design, made first when there are none or they are stale -> the final status dict."""
    path = "/api/drawings/%s/status" % urllib.parse.quote(pid, safe="")
    st = get_json(base, path)
    if st.get("state") == "current" and not st.get("stale"):
        return st
    before = st.get("made_at")
    seen = st.get("state") == "making"
    job = None
    if not seen:
        job = (post_json(base, "/api/drawings/%s/make" % urllib.parse.quote(pid, safe=""), {}) or {}).get("job_id")
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(2)
        st = get_json(base, path)
        state = st.get("state")
        if state == "making":
            seen = True
        elif state == "failed" and (seen or (job and st.get("job_id") == job)):
            return st                                      # this make's failure, not the previous run's
        elif st.get("stopped") and seen:
            return st
        elif state == "current" and (seen or st.get("made_at") != before):
            return st
    st = dict(st, timed_out=True)
    return st


def find_request(filters, words, kind, limit, sort):
    """The catalogue page's request body (catalogue.html buildRequest()); words are the search box's plain words, sent as
    the page sends them (catphrase.js parsePhrase: a part-label `like` on the words, plus the words for the design names)."""
    where = {"part.label": {"like": " ".join(words)}} if words else {}
    return {"where": where, "filters": filters or {}, "rank": None, "words": words or None, "kind": kind or "parts",
            "same": "once", "sort": sort or "name", "offset": 0, "limit": limit}


def parse_range(text):
    """"MIN:MAX" (either side may be empty) -> {"min", "max"}."""
    lo, sep, hi = str(text).partition(":")
    if not sep:
        raise ValueError("a range is MIN:MAX, e.g. 10:40 or :0.5")
    lo, hi = lo.strip(), hi.strip()
    if not lo and not hi:
        raise ValueError("a range needs a MIN or a MAX")
    return {"min": float(lo) if lo else None, "max": float(hi) if hi else None}


def _range_arg(text):
    try:
        return parse_range(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


# ---- subcommands ------------------------------------------------------------------------------------------------------------
def _safe_name(name):
    """A server-given file name may only name a file inside the output dir."""
    name = os.path.basename(name or "")
    if name in ("", ".", "..") or "/" in name or "\\" in name:
        raise Refused(0, "the gateway gave a file name that cannot be used: %r" % name)
    return name


def _target(a):
    """-> (pid or None, label) for FILE | --id."""
    if a.id:
        return a.id, a.id
    if not a.file:
        raise Refused(0, "give a STEP file or --id PID")
    return None, Path(a.file).stem


def _left_behind(label):
    """The upload stopped before its answer, but the studio may still finish the design; the CLI cannot remove it."""
    print("warning: the studio may still add the design %r from this file; check the studio's list and trash and remove it "
          "there (a design with a sample's name keeps that sample from being added again)" % label, file=sys.stderr)


def _with_design(a, work):
    """Run work(base, pid, outdir, label) on --id or on FILE opened as a temporary design (removed after, unless --keep)."""
    base = base_url(a.url)
    pid, label = _target(a)
    outdir = Path(a.out or os.path.join("buildcore-out", label))
    created = None
    try:
        if pid is None:
            if not os.path.isfile(a.file):
                raise Refused(0, "no such file: %s" % a.file)
            try:
                done = upload(base, a.file, single_part=False, name=label)
            except (Unreachable, Refused, KeyboardInterrupt, OSError, http.client.HTTPException) as e:
                if not isinstance(e, Refused) or e.status == 0:     # cut off, timed out or stopped: the build may go on
                    _left_behind(label)
                raise
            if not done.get("ok"):
                if done.get("error") == NO_ANSWER:
                    _left_behind(label)
                raise Refused(0, done.get("error") or "the design could not be added")
            pid = created = done["product_id"]
        outdir.mkdir(parents=True, exist_ok=True)
        return work(base, pid, outdir, label)
    finally:
        if created and not a.keep:
            try:
                if not remove(base, created):
                    print("warning: the temporary design %s could not be removed; remove it in the studio's trash" % created,
                          file=sys.stderr)
            except Unreachable:
                print("warning: the temporary design %s could not be removed (gateway gone)" % created, file=sys.stderr)


def cmd_bom(a):
    def work(base, pid, outdir, label):
        data = _ok(*request("GET", "%s/api/bom/%s?format=%s" % (base, urllib.parse.quote(pid, safe=""), a.format)))
        p = outdir / ("%s-bom.%s" % (label, a.format))
        p.write_bytes(data)
        if a.format == "json":
            rows = json.loads(data).get("rows") or []
            print("%-4s %-30s %4s  %-18s %s" % ("item", "name", "qty", "material", "bought in"))
            for r in rows:
                print("%-4s %-30s %4s  %-18s %s" % (r.get("item"), str(r.get("name"))[:30], r.get("qty"),
                                                   str(r.get("material") or "")[:18], "yes" if r.get("bought_in") else ""))
        print(p)
        return 0
    return _with_design(a, work)


def cmd_glb(a):
    def work(base, pid, outdir, label):
        data = _ok(*request("GET", "%s/api/glb/%s" % (base, urllib.parse.quote(pid, safe="")), timeout=900))
        p = outdir / (label + ".glb")
        p.write_bytes(data)
        print(p)
        return 0
    return _with_design(a, work)


def cmd_drawing(a):
    def work(base, pid, outdir, label):
        st = drawings(base, pid, timeout=a.timeout)
        sheets = get_json(base, "/api/drawings/%s" % urllib.parse.quote(pid, safe=""))
        for sh in sheets:
            if sh.get("set", "current") != "current":
                continue
            url = sh.get("url") or ""
            name = _safe_name(urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]))
            p = outdir / name
            p.write_bytes(_ok(*request("GET", base + urllib.parse.urlsplit(url).path)))
            print(p)
        bad = st.get("state") in ("failed",) or st.get("stopped") or st.get("timed_out") or st.get("failed_parts")
        for f in st.get("failed_parts") or []:
            print("%s: %s" % (f.get("ref"), f.get("why")), file=sys.stderr)
        if st.get("timed_out"):
            print("the drawings were not done after %d s" % a.timeout, file=sys.stderr)
        elif st.get("state") == "failed":
            print(st.get("user_text") or "the drawings could not be made", file=sys.stderr)
        elif st.get("stopped"):
            print("the drawings were stopped", file=sys.stderr)
        return 1 if bad else 0
    return _with_design(a, work)


def cmd_add(a):
    base = base_url(a.url)
    if not os.path.isfile(a.file):
        raise Refused(0, "no such file: %s" % a.file)
    done = upload(base, a.file, single_part=True, material=a.material, name=a.name)
    if a.json:
        print(json.dumps(done, indent=2))
    if not done.get("ok"):
        if not a.json:
            print(done.get("error") or "the part could not be added", file=sys.stderr)
        return 1
    if not a.json:
        print("added %s (%s)" % (done.get("name"), done.get("product_id")))
    return 0


def _num(v, fmt="%.1f"):
    return "" if v is None else fmt % v


def cmd_find(a):
    base = base_url(a.url)
    filters = {}
    if a.family:
        filters["family"] = a.family
    if a.material:
        filters["material"] = a.material
    for k in RANGE_FILTERS:
        v = getattr(a, k)
        if v is not None:
            filters[k] = v
    res = post_json(base, "/api/catalogue/search", find_request(filters, a.words, a.kind, a.limit, a.sort))
    if a.json:
        print(json.dumps(res, indent=2))
        return 0
    if a.kind == "assemblies":
        block = res.get("assemblies") or {}
        for it in block.get("items") or []:
            print("%-30s %-24s parts %-4s pieces %s" % (str(it.get("name"))[:30], it.get("product_id"), it.get("parts"),
                                                        it.get("pieces")))
        print("%d assemblies (%s)" % (block.get("total", 0), res.get("text", "")))
        return 0
    block = res.get("parts") or {}
    for it in block.get("items") or []:
        env = " x ".join(_num(x) for x in (it.get("envelope_mm") or []) if x is not None)
        bores = ",".join(_num(b.get("dia_mm"), "%.2f") for b in it.get("bores") or [])
        designs = ",".join(d.get("product_id") for d in it.get("designs") or [])
        print("%-26s %-12s %-22s vol %-10s bore %-12s in %s" % (str(it.get("label"))[:26], str(it.get("family") or "")[:12],
                                                               env, _num(it.get("volume_mm3"), "%.0f"), bores or "-", designs))
    print("%d parts (%s)" % (block.get("total", 0), res.get("text", "")))
    return 0


def parser():
    ap = argparse.ArgumentParser(prog="buildcore", description="buildcore over the studio's web routes (same functions as the UI)")
    ap.add_argument("--url", help="the gateway (default: BUILDCORE_URL, else %s)" % DEFAULT_URL)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def design_cmd(name, helptext, f):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("file", nargs="?", help="a STEP file, opened as a temporary design")
        p.add_argument("--id", help="an existing design's product id (nothing uploaded or removed)")
        p.add_argument("-o", "--out", help="output folder (default ./buildcore-out/<name>/)")
        p.add_argument("--keep", action="store_true", help="keep the temporary design in the studio")
        p.add_argument("--url", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        p.set_defaults(f=f)
        return p

    p = design_cmd("drawing", "dimensioned drawing sheets", cmd_drawing)
    p.add_argument("--timeout", type=int, default=900, help="seconds to wait for the drawings (default 900)")
    p = design_cmd("bom", "bill of materials", cmd_bom)
    p.add_argument("--format", choices=("json", "csv"), default="json")
    design_cmd("glb", "3D model (glTF binary)", cmd_glb)

    p = sub.add_parser("add", help="add one part to the catalogue")
    p.add_argument("file", help="a STEP file holding one solid")
    p.add_argument("--material", help="a materials list name (default: the file's own, else steel)")
    p.add_argument("--name", help="the part's name (default: the file name)")
    p.add_argument("--json", action="store_true", help="print the server's answer as JSON")
    p.add_argument("--url", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p.set_defaults(f=cmd_add)

    p = sub.add_parser("find", help="search the catalogue (the catalogue page's filters)")
    p.add_argument("--family")
    p.add_argument("--material")
    for k in RANGE_FILTERS:
        p.add_argument("--" + k, type=_range_arg, metavar="MIN:MAX")
    p.add_argument("--words", nargs="+", help="name words")
    p.add_argument("--kind", choices=("parts", "assemblies"), default="parts")
    p.add_argument("--sort", choices=("name", "volume_desc", "volume_asc", "length_desc", "used"), default="name")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--json", action="store_true", help="print the server's answer as JSON")
    p.add_argument("--url", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p.set_defaults(f=cmd_find)
    return ap


def main(argv):
    a = parser().parse_args(argv)
    try:
        return a.f(a)
    except Unreachable:
        print("the buildcore gateway is not reachable at %s (start it with: docker compose up -d)" % base_url(a.url),
              file=sys.stderr)
        return 3
    except Refused as e:
        print(str(e), file=sys.stderr)
        return 1
    except (ValueError, KeyError, OSError, http.client.HTTPException) as e:
        # an answer that is not the gateway's (not JSON, a key missing, cut short) or an output that cannot be written
        print("buildcore: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
