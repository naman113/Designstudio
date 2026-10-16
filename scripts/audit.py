"""Pre-publish audit of the tracked files: run it before anything is published.

    python3 scripts/audit.py [--ref REF] [--names FILE]

Without --ref it reads the working-tree content of every file `git ls-files` lists. With --ref it reads the files of
that commit or branch (`git ls-tree -r REF`, content from the object store), so a branch can be audited without
checking it out. Standard library only; run it from anywhere inside the repository.

Each finding is one line:  <check> <path>:<line>: <excerpt>   (line 0 = the file as a whole)

  ids      planning ids in text or file names: review and decision ids such as "XX-07", phase and plan numbers,
           quick-task ids (any case, two or three digits), success-criterion ids, references to planning documents
           and private design documents
  vendor   names of AI vendors and models (case-insensitive)
  paths    absolute user home paths (macOS, Windows and Linux forms; the image's own service user is allowed)
  secrets  API keys and tokens (sk-, GitHub, AWS, Slack), PEM private keys, quoted password/secret/token assignments
           of a literal value (a value starting with $, %, { or < is an expansion or placeholder, not a secret), and
           passwords inside URLs (user:password@host) other than placeholders and the documented default
  entropy  long high-entropy tokens that are not plain hex: printed for review, they do not fail the run
  files    tracked files that must never be published: env files, databases, CAD sources, SSH keys, key stores,
           certificates, local planning and assistant folders
  binary   tracked binary files (a NUL byte in the first 8000 bytes); fonts, images and PDFs are allowed and listed
           for review, any other binary is a finding. The printable text runs of every binary (metadata such as
           author, title, creator and source paths) go through the vendor, paths, secrets and names checks
           (check "binary-text")
  names    with --names FILE only: every non-empty line of FILE is a case-insensitive regular expression (for example
           a list of private product or customer names kept outside the repository); hits are findings

`ids` and `vendor` skip upstream licence texts and the vendored pgmq SQL.
Ends with "audit: clean (N files)" and exit 0, or "audit: K findings" and exit 1. Bad arguments exit 2.
"""
import argparse
import math
import os
import re
import subprocess
import sys

# Planning ids. The vendor words and home paths below are written with a character class on one letter so this file
# never contains them literally and passes its own audit.
ID_RX = re.compile(
    r"\b(WR|CR|IN|BL|SF|DR|PG|RUN|REP|PUB|CAT|DRW|BOM|V3D|UI|S|T|D)-[0-9]+[a-z]?\b"
    r"|\bA-[0-9]+\b"
    r"|\b[Pp]hases? [0-9]+"
    r"|\b[Pp]lans? [0-9]{2}-[0-9]{2}\b"
    r"|(?<![0-9-])0[0-9]-[01][0-9](?![0-9-])"
    r"|\b26[01][0-9]{3}-q[0-9]+"
    r"|(?i:\bq[0-9]{2,3}\b)"
    r"|\bimpediments? i[0-9]+"
    r"|\bSC[1-5]\b"
    r"|(PLAN|SUMMARY|CONTEXT|VERIFICATION|RESEARCH|REVIEW|ROADMAP|REQUIREMENTS|[C]ONTRACTS|[S]ERVICE-MAP)\.md"
    r"|\b([C]ONTRACTS|[S]ERVICE-MAP)\b"
    r"|\bdocs/history\b"
    r"|\bdecisions? [A-Z]{1,2}-?[0-9]+")
VENDOR_RX = re.compile(r"[c]laude|[a]nthropic|\b[o]pus\b|\b[s]onnet\b|\b[h]aiku\b", re.I)
# Linux homes: any user but bc, the image's own service user (docker/Dockerfile).
PATH_RX = re.compile(r"/[U]sers/|\b[A-Za-z]:(\\+|/)[U]sers\b|/[h]ome/(?!bc/)[a-z_][a-z0-9_-]*/")
SECRET_RX = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b(gh[p]_|gh[o]_|github_[p]at_)[A-Za-z0-9_]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|secret|token|passwd|password)\s*[:=]\s*['\"](?![$%{<])[^'\"\s]{8,}"),     # a literal value, not a shell/template expansion
]
# user:password@ in a URL. Not a secret: an expansion or placeholder ($, %, {, < anywhere) or a documented default.
URL_PW_RX = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@'\"]*:([^\s/@'\"]+)@", re.I)
URL_PW_OK = {"buildcore", "password", "<password>", "pass", "secret", "changeme", "***", "..."}

TOKEN_RX = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
HEX_RX = re.compile(r"^[0-9a-fA-F]+$")
ENTROPY_MIN = 4.5

# Forbidden tracked files, matched against the path (FORBIDDEN_DIRS) or the base name (FORBIDDEN_NAMES), any case.
FORBIDDEN_NAMES = re.compile(
    r"^(\.[e]nv(\..*)?|\.[e]nvrc|.*\.(db|sqlite[0-9]*|sqlite-.*|step|stp|fcstd|pem|key|p12|pfx|kdbx|jks|keystore)"
    r"|id_([r]sa|[d]sa|[e]cdsa|[e]d25519)(\.pub)?.*|[c]redentials.*\.json|[C]LAUDE\.md)$", re.I)
FORBIDDEN_DIRS = re.compile(r"(^|/)(\.[p]lanning|\.[c]laude)/", re.I)

BINARY_OK = (".ttf", ".png", ".gif", ".pdf", ".jpg", ".webp")
UPSTREAM_NAMES = re.compile(r"^(LICENSE.*|THIRD_PARTY_LICENSES.*|NOTICE.*|.*\.license)$")
UPSTREAM_PATHS = {"vendor/pgmq/pgmq-v1.13.0.sql"}


def git(*args, data=None):
    r = subprocess.run(["git"] + list(args), input=data, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), r.stderr.decode(errors="replace").strip()))
    return r.stdout


def file_set(ref):
    """[(path, bytes)] of the tracked files: the working tree, or the files of `ref`."""
    if ref is None:
        out = []
        for path in git("ls-files", "-z").decode().split("\0"):
            if path and os.path.isfile(path) and not os.path.islink(path):
                with open(path, "rb") as fh:
                    out.append((path, fh.read()))
        return out
    paths = [p for p in git("ls-tree", "-r", "-z", "--name-only", ref).decode().split("\0") if p]
    blob = git("cat-file", "--batch", data="".join("%s:%s\n" % (ref, p) for p in paths).encode())
    out, pos = [], 0
    for p in paths:
        nl = blob.index(b"\n", pos)
        header = blob[pos:nl].decode().split()
        if len(header) < 3 or header[1] != "blob":     # a submodule or a missing object: nothing to read
            pos = nl + 1
            continue
        size = int(header[2])
        out.append((p, blob[nl + 1:nl + 1 + size]))
        pos = nl + 1 + size + 1
    return out


def entropy(token):
    counts = {}
    for ch in token:
        counts[ch] = counts.get(ch, 0) + 1
    n = float(len(token))
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def excerpt(line):
    line = line.strip()
    return line if len(line) <= 120 else line[:117] + "..."


def secret_in(line):
    if any(rx.search(line) for rx in SECRET_RX):
        return True
    for pw in URL_PW_RX.findall(line):
        if not any(c in pw for c in "$%{<") and pw.lower() not in URL_PW_OK:
            return True
    return False


def binary_text(data):
    """The printable ASCII runs of six characters or more (what `strings -n 6` prints)."""
    return [m.decode("ascii") for m in re.findall(rb"[\x20-\x7e]{6,}", data)]


def load_names(path):
    rx = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                rx.append(re.compile(raw, re.I))
    return rx


def audit(files, names):
    """Yield (check, path, line, text, fails) for every finding."""
    for path, data in files:
        base = path.rsplit("/", 1)[-1]
        upstream = path in UPSTREAM_PATHS or bool(UPSTREAM_NAMES.match(base))
        if FORBIDDEN_NAMES.match(base) or FORBIDDEN_DIRS.search(path):
            yield "files", path, 0, "forbidden tracked file", True
        if not upstream and ID_RX.search(path):
            yield "ids", path, 0, "file name carries an id", True
        if b"\0" in data[:8000]:
            ok = base.lower().endswith(BINARY_OK)
            yield "binary", path, 0, "%d bytes%s" % (len(data), " (allowed type, review)" if ok else ""), not ok
            for run in binary_text(data):
                if ((not upstream and VENDOR_RX.search(run)) or PATH_RX.search(run) or secret_in(run)
                        or any(rx.search(run) for rx in names)):
                    yield "binary-text", path, 0, excerpt(run), True
            continue
        text = data.decode("utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            if not upstream and ID_RX.search(line):
                yield "ids", path, n, excerpt(line), True
            if not upstream and VENDOR_RX.search(line):
                yield "vendor", path, n, excerpt(line), True
            if PATH_RX.search(line):
                yield "paths", path, n, excerpt(line), True
            if secret_in(line):
                yield "secrets", path, n, excerpt(line), True
            for tok in TOKEN_RX.findall(line):
                if not HEX_RX.match(tok) and entropy(tok) >= ENTROPY_MIN:
                    yield "entropy", path, n, "%s (%.2f bits/char, review)" % (tok[:60], entropy(tok)), False
            if any(rx.search(line) for rx in names):
                yield "names", path, n, excerpt(line), True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ref", help="audit this commit or branch instead of the working tree")
    ap.add_argument("--names", help="file of extra case-insensitive regular expressions, one per line")
    a = ap.parse_args(argv)
    try:
        top = git("rev-parse", "--show-toplevel").decode().strip()
    except (RuntimeError, OSError) as exc:
        print("audit: not inside a git repository (%s)" % exc, file=sys.stderr)
        return 2
    os.chdir(top)
    names = []
    if a.names:
        try:
            names = load_names(a.names)
        except (OSError, re.error) as exc:
            print("audit: cannot use --names %s: %s" % (a.names, exc), file=sys.stderr)
            return 2
    try:
        files = file_set(a.ref)
    except RuntimeError as exc:
        print("audit: %s" % exc, file=sys.stderr)
        return 2
    failing = 0
    for check, path, line, text, fails in audit(files, names):
        print("%s %s:%d: %s" % (check, path, line, text))
        failing += 1 if fails else 0
    if failing:
        print("audit: %d findings" % failing)
        return 1
    print("audit: clean (%d files)" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
