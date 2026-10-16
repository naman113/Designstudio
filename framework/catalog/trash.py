"""The trash list: one writer, the catalogue service (`catalogue.trash`, `catalogue.restore`,
`catalogue.erase`, framework/catalog/svc_trash.py); `catalogue.q.trash` and the product list read it
(framework/catalog/svc_queries.py).

Moved from framework/web/app.py unchanged except for names (no leading underscore) and imports: the JSON helpers with their
bounded retry (a reader can catch the file between the old one being removed and the new one landing, PermissionError /
WinError 5 or 32, or a half-written file; both are transient on Windows), `git_tracked`, the trash file's layout and path
(work/portfolio_trash.json, so the user's existing list carries over), and `product_removable`.
"""
import json
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CATALOG = os.path.join(ROOT, "framework", "catalog")
PRODUCTS = os.path.join(ROOT, "framework", "products")
if CATALOG not in sys.path:          # append, never insert: no catalogue module may shadow the standard library
    sys.path.append(CATALOG)


def manifest(product_id):
    p = os.path.join(PRODUCTS, product_id + ".json")
    return p if os.path.exists(p) else None


_RETRY_WINDOW_S = 3.0


def _win_denied(exc):
    return isinstance(exc, PermissionError) or (isinstance(exc, OSError) and getattr(exc, "winerror", None) in (5, 32))


def _replace_atomic(tmp, path):
    """os.replace(tmp, path) with a bounded retry on a transient Windows access-denied/sharing violation."""
    deadline = time.time() + _RETRY_WINDOW_S
    delay = 0.01
    while True:
        try:
            os.replace(tmp, path)
            return
        except OSError as e:
            if not _win_denied(e) or time.time() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def _read_json(path, default=None):
    """Bounded-retry read of a JSON file another process may be replacing. A missing file returns
    `default` at once."""
    if not os.path.exists(path):
        return default
    deadline = time.time() + _RETRY_WINDOW_S
    delay = 0.01
    while True:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, OSError) as e:
            if isinstance(e, OSError) and not isinstance(e, PermissionError) and not _win_denied(e):
                raise
            if time.time() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def _write_json(path, data):
    """Each writer gets its own temp name next to `path` (never one shared "<path>.tmp"); a failed
    write removes its own temp file."""
    tmp = "%s.%x-%x-%s.tmp" % (path, os.getpid(), threading.get_ident() & 0xFFFFFF, os.urandom(3).hex())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        _replace_atomic(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise



def git_tracked(path):
    """Is the file one the repository tracks (a reference, never erased)? Fails closed: BC_TRACKED_FILES (a list of the
    tracked files, one per line, relative to the root: the images carry /app/.tracked-files) when set; else git's own answer, where
    only "not tracked" (exit 1) counts as untracked and anything else (git missing, a .git it cannot read: exit 128) is "tracked"."""
    try:
        rel = os.path.relpath(path, ROOT).replace("\\", "/")      # across drives (Windows) this raises: answer "tracked"
    except Exception:
        return True
    listing = os.environ.get("BC_TRACKED_FILES")
    if listing:
        try:
            with open(listing, encoding="utf-8") as fh:
                names = {ln.strip() for ln in fh.read().replace("\r\n", "\n").split("\n") if ln.strip()}
        except OSError:
            return True      # the list cannot be read -> unsure
        if not names or "framework/catalog/trash.py" not in names:
            return True      # empty or cut short -> unsure
        return rel in names
    try:
        r = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=ROOT, capture_output=True, text=True, timeout=20)
        return r.returncode != 1
    except Exception:
        return True          # unsure -> treat as reference, never erase




# ---- portfolio trash: DELETE hides an item (soft); the trash restores it or erases it for good.
# Reference items (golden/built-in products) can be hidden, never erased.
TRASH = os.path.join(ROOT, "work", "portfolio_trash.json")
LOCK = threading.Lock()
GOLDEN_PRODUCTS = os.path.join(ROOT, "tests", "golden", "products.json")


def load():
    # _read_json: a bounded retry on a read that catches save() mid-replace
    # (PermissionError/WinError 5/32, or a half-written file) -- a bare open()+json.load() here
    # would turn that transient race into a permanently empty trash list for the rest of the request.
    try:
        return _read_json(TRASH, default={"entries": []}).get("entries", [])
    except Exception:
        return []


def save(entries):
    # _write_json: same JSON layout as before, its own temp name and a bounded retry on the
    # replace (a shared "portfolio_trash.json.tmp" + bare os.replace fails often when several
    # processes write at once).
    os.makedirs(os.path.dirname(TRASH), exist_ok=True)
    _write_json(TRASH, {"entries": entries})


def trashed(kind):
    return {e["id"] for e in load() if e["kind"] == kind}


def drop(kind, item_id):
    with LOCK:
        entries = load()
        keep = [e for e in entries if not (e["kind"] == kind and e["id"] == item_id)]
        if len(keep) != len(entries):
            save(keep)
        return len(keep) != len(entries)


def golden_ids():
    try:
        with open(GOLDEN_PRODUCTS, encoding="utf-8") as fh:
            return {p["product_id"] for p in json.load(fh).get("products", [])}
    except Exception:
        return set()


def product_removable(product_id):
    """Provenance: only products an upload (or the CLI through the same code) created
    carry origin "upload" (an opened assembly) or "part" (a part added to the catalogue);
    reference products are committed, frozen as golden numbers, or have no origin. Those
    can only be hidden."""
    man = manifest(product_id)
    if not man or product_id in golden_ids():
        return False
    try:
        with open(man, encoding="utf-8") as fh:
            origin = json.load(fh).get("origin")
    except Exception:
        return False
    return origin in ("upload", "register", "part") and not git_tracked(man)


def add(kind, item_id, name, removable):
    with LOCK:
        entries = load()
        for e in entries:
            if e["kind"] == kind and e["id"] == item_id:
                return e
        e = {"kind": kind, "id": item_id, "name": name, "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "removable": bool(removable)}
        entries.append(e)
        save(entries)
        return e




def product_name(product_id):
    """The product's name: the SQLite catalogue's, else its manifest's, else None."""
    try:
        import ingest
        con = ingest.connect(readonly=True)
        try:
            row = con.execute("SELECT name FROM products WHERE product_id = ?", (product_id,)).fetchone()
        finally:
            con.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    man = manifest(product_id)
    if man:
        try:
            with open(man, encoding="utf-8") as fh:
                return json.load(fh).get("name") or None
        except Exception:
            return None
    return None
