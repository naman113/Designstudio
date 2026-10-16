"""Add the sample designs to a running buildcore, through the studio's own upload routes (Add part / Open assembly).

Usage: python -m framework.samples.load [--url URL] [--dir /app/samples] [--wait 600] [--no-drawings] [--marker-db]

Idempotent: a sample whose name is already a design, or is in the trash (the user removed it), is left alone; only the
missing ones are added. With --marker-db, the "samples loaded" flag lives in the database itself (the row
samples_loaded of the table buildcore_flags): a run that ends with every sample there sets it (whether or not the
drawings below succeed), and a later run that finds it does nothing, so a sample the user removed (even erased for good
from the trash) stays removed while the database lives. A new database (the pgdata volume removed) has no flag, so the
samples are loaded again. Delete the row to load them again by hand. When the flag cannot be read, nothing is uploaded
(exit 1), so erased samples never come back by mistake; when it cannot be written, the run exits 1 too.
Then the drawings of the sample assembly are made (unless --no-drawings), so the demo shows them.
The STEP files come from `python -m framework.samples.make DIR`. Compose runs this once per start as the `init` service.
Exit codes: 0 every sample there (and its drawings current), 1 something failed, 3 the gateway never answered.
"""
import argparse
import os
import sys
import time

from framework import cli
from framework.samples import SAMPLES

FLAG = "samples_loaded"
_DDL = ("CREATE TABLE IF NOT EXISTS public.buildcore_flags (name text PRIMARY KEY, value text NOT NULL, "
        "set_at timestamptz NOT NULL DEFAULT now())")


def _flag_connect():
    from framework.common import pg
    con = pg.connect(write=True, app="buildcore-init")
    con.execute(_DDL)
    return con


def flag_is_set():
    """True when the database already records that the samples were loaded. Raises when it cannot be read."""
    con = _flag_connect()
    try:
        return con.execute("SELECT 1 FROM public.buildcore_flags WHERE name = %s", (FLAG,)).fetchone() is not None
    finally:
        con.close()


def set_flag():
    con = _flag_connect()
    try:
        value = "samples loaded %s" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        con.execute("INSERT INTO public.buildcore_flags (name, value) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
                    (FLAG, value))
    finally:
        con.close()


def _flag_errors():
    try:
        import psycopg
        return (psycopg.Error, OSError, RuntimeError, ImportError)
    except ImportError:
        return (OSError, RuntimeError, ImportError)


def missing(specs, products, trash):
    """The specs whose name is no design's name and no mechanical trash entry's name."""
    have = {p.get("name") for p in products or []}
    have |= {t.get("name") for t in trash or [] if t.get("kind") == "mechanical"}
    return [s for s in specs if s["name"] not in have]


def _wait_ready(base, wait_s):
    end = time.time() + wait_s
    while True:
        try:
            return cli.products(base)
        except (cli.Unreachable, cli.Refused) as e:
            if isinstance(e, cli.Refused) and e.status < 500:
                raise
            if time.time() > end:
                return None
            time.sleep(3)


def main(argv):
    ap = argparse.ArgumentParser(prog="framework.samples.load", description="add the missing sample designs")
    ap.add_argument("--url", help="the gateway (default: BUILDCORE_URL, else %s)" % cli.DEFAULT_URL)
    ap.add_argument("--dir", default="/app/samples", help="the folder with the sample STEP files")
    ap.add_argument("--wait", type=float, default=600, help="seconds to wait for the gateway (default 600)")
    ap.add_argument("--no-drawings", action="store_true", help="do not make the sample assembly's drawings")
    ap.add_argument("--marker-db", action="store_true",
                    help="keep the 'samples loaded' flag in the database (table buildcore_flags); when set, do nothing")
    a = ap.parse_args(argv)
    base = cli.base_url(a.url)
    if a.marker_db:
        try:
            loaded = flag_is_set()
        except _flag_errors() as e:
            print("samples: cannot read the samples flag from the database (%s); nothing uploaded"
                  % type(e).__name__, file=sys.stderr)
            return 1
        if loaded:
            print("samples: loaded on an earlier start of this database "
                  "(delete the row samples_loaded from buildcore_flags to load them again)")
            return 0
    try:
        prods = _wait_ready(base, a.wait)
        if prods is None:
            print("samples: the buildcore gateway did not answer at %s within %d s" % (base, a.wait), file=sys.stderr)
            return 3
        todo = missing(SAMPLES, prods, cli.trash_items(base))
        ok = True
        for s in SAMPLES:
            if s not in todo:
                print("sample %s: already there" % s["name"])
                continue
            done = cli.upload(base, os.path.join(a.dir, s["file"]), single_part=(s["kind"] == "part"),
                              material=s["material"], name=s["name"])
            if done.get("ok"):
                print("sample %s: added (%s)" % (s["name"], done.get("product_id")))
            else:
                ok = False
                print("sample %s: failed: %s" % (s["name"], done.get("error")))
        uploads_ok = ok   # every sample is there now: drawing failures below do not block the flag
        if not a.no_drawings:
            names = {p.get("name"): p.get("product_id") for p in cli.products(base)}
            for s in SAMPLES:
                if s["kind"] != "assembly" or s["name"] not in names:
                    continue
                st = cli.drawings(base, names[s["name"]])
                good = st.get("state") == "current" and not st.get("failed_parts") and not st.get("timed_out")
                ok = ok and good
                if not good:
                    print("sample %s: drawings failed: %s%s" % (
                        s["name"], "timed out" if st.get("timed_out") else st.get("state"),
                        ", failed parts %s" % st.get("failed_parts") if st.get("failed_parts") else ""),
                        file=sys.stderr)
                else:
                    print("sample %s: drawings %s" % (s["name"], st.get("state")))
        if uploads_ok and a.marker_db:
            try:
                set_flag()
            except _flag_errors() as e:
                print("samples: error: could not record the samples flag in the database (%s); erased samples may "
                      "come back on the next start" % type(e).__name__, file=sys.stderr)
                return 1
        return 0 if ok else 1
    except cli.Unreachable:
        print("samples: the buildcore gateway is not reachable at %s" % base, file=sys.stderr)
        return 3
    except (cli.Refused, OSError) as e:
        print("samples: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
