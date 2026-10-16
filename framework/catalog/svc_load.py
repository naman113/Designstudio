"""The catalogue service's product load: `catalogue.load`, measuring through jobs.

    catalogue.load {manifest, product_id, side: "kernel", target: "primary" | "shadow", carry?}
        -> {product_id, parts, interfaces, missing, side, target, db, ms, measured: [path, ...]} plus the keys of `carry`
           (the upload's answer: name, added, messages ...).   Errors: bad_request, no_product, measure_failed.

It is `product.load_side` with the catalogue's two injection points filled in (77-01):
  * `measure(path, side)`: a document the catalogue has no fresh copy of is measured by a CHILD JOB of this one
    (`parent_job_id` = this job, sender `catalogue`): `geometry.measure` (the kernel). The answer is the child's result
    file `work/results/<child>.measure.json` (`doc`), stored by `ingest.store` unchanged.
  * `d2_provider(targets)`: the shape signature (`d2_v1`) of the parts a load adds comes from the geometry payloads this
    load already has (their `signatures`), or from ONE `geometry.signature` job for the rest. Never from an in-process kernel.

`target` "primary" is the default catalogue (mirrored into Postgres by the writer connection); "shadow" is the second engine's
own catalogue (`derived.kernel_db()`: owned by the catalogue, never mirrored), written by the `product.load` recipe's second
step and compared with the first by its third (`engines.compare` op `measure`). A stop reaches the child that is running.

Safety: this service submits only `geometry.measure` and `geometry.signature`, with paths taken
from the product's own manifest and the catalogue's own documents; nothing from a message beyond the manifest's id and path.
"""
import json
import os
import time

from framework.common import derived, jobs, pg, policy, results
from framework.common.consumer import ServiceError, Stopped

ROOT = derived.ROOT
SLICE_S = 1.0                       # how often a waiting handler looks at its Stop flag
MAX_TARGETS = 2000                  # geometry.signature's own limit per job
_BAD = "This request could not be read."


def _bad(why):
    return ServiceError("bad_request", _BAD, detail="load payload: %s" % why)


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


class Children:
    """The child jobs of one load: one connection, one job at a time, each waited for in 1 s slices so a Stop is seen."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.con = pg.connect(write=True)
        self.ids = []

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass

    def run(self, job_type, payload, op):
        """Submit, wait, and -> the child's result file (parsed). Failure -> measure_failed; Stop -> the child is stopped."""
        ctx = self.ctx
        ctx.check()
        job_id = jobs.submit(self.con, job_type, payload, sender="catalogue", parent_job_id=ctx.job_id,
                             correlation_id=ctx.envelope.get("correlation_id"))
        self.ids.append(job_id)
        end = time.monotonic() + policy.policy(job_type).limit_s + 90
        while True:
            try:
                row = jobs.wait(self.con, job_id, timeout_s=SLICE_S)
                break
            except TimeoutError:
                pass
            try:
                ctx.check()
            except Stopped:
                try:
                    jobs.stop(self.con, job_id, sender="catalogue")
                except Exception:
                    pass
                raise
            if time.monotonic() > end:
                raise ServiceError("measure_failed", "This design could not be measured.",
                                   detail="%s %s: no answer in time" % (job_type, job_id))
        if row["state"] != "succeeded":
            raise ServiceError("measure_failed", "This design could not be measured.",
                               detail="%s %s %s: %s %s" % (job_type, job_id, row["state"], row.get("error_code"),
                                                           (row.get("user_text") or "")[:200]))
        path = os.path.join(results.DIR, "%s.%s.json" % (job_id, op))
        if not os.path.isfile(path):
            raise ServiceError("measure_failed", "This design could not be measured.",
                               detail="%s %s: no result file" % (job_type, job_id))
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)


def make_measure(ctx, kids, index, measured):
    """`measure(path, side)` for ingest_file. `index` collects {normalised path: {object: d2_v1 vector}} from geometry
    results; `measured` lists every path measured (the answer's `measured`)."""
    def measure(path, side):
        if side != "kernel":
            raise _bad("unknown side")
        ctx.progress("measure")
        res = kids.run("geometry.measure", {"path": path, "side": "kernel"}, "measure")
        doc = res.get("doc")
        index[_norm(path)] = {ref: (s or {}).get("d2_v1") for ref, s in (res.get("signatures") or {}).items()}
        if not isinstance(doc, dict) or "parts" not in doc:
            raise ServiceError("measure_failed", "This design could not be measured.", detail="the measure result has no document")
        measured.append(path)
        ctx.progress("record")
        return doc
    return measure


def make_d2_provider(ctx, kids, index):
    """`d2_provider(targets)` for the mirror's signature backfill; targets = [(part_id, [(path, object), ...])]."""
    def provider(targets):
        out, need = {}, []
        for part_id, cands in targets:
            decided = False
            for path, ref in cands:
                known = index.get(_norm(path))
                if known is None:                    # a path this load did not measure with the kernel: ask the job
                    need.append([part_id, [[p, r] for p, r in cands]])
                    decided = True
                    break
                if known.get(ref) is not None:       # measured here: that part's signature
                    out[part_id] = known[ref]
                    decided = True
                    break
            if not decided:
                out[part_id] = None
        for i in range(0, len(need), MAX_TARGETS):
            chunk = need[i:i + MAX_TARGETS]
            try:
                res = kids.run("geometry.signature", {"targets": chunk}, "signature")
                d2 = res.get("d2_v1") or {}
            except Stopped:
                raise
            except Exception as exc:                 # never fail the load for a signature: `sync --full` fills it later
                print("catalogue.load %s: geometry.signature failed: %s" % (str(ctx.job_id)[:8], str(exc)[:200]), flush=True)
                d2 = {}
            for part_id, _cands in chunk:
                out[part_id] = d2.get(part_id)
        return out
    return provider


REFRESH_TRIES = 5                   # the loaded parts' derived facts; a sync holding the lock -> 1 s, try again


def _refresh_part_records(pid, n_parts=0):
    """The derived facts (mass, hole count, bore / outside diameters ...) of the parts of the product just loaded, written
    now, so the catalogue page's filters match a part the moment its upload answers instead of after the next
    10-minute partcards pass. Best effort: never fails the load (the pass catches up). `n_parts`: the parts the load
    measured, so "no parts" is told apart from "not mirrored yet". -> refresh's counts, or {"stale": True, "why"} when
    the records were not written now (logged as a warning)."""
    con = None
    try:
        from framework.catalog.pg import partcards
        con = partcards.connect()
        partcards.ensure(con)
        ids = [r[0] for r in con.execute("SELECT DISTINCT part_id FROM occurrence WHERE product_id = %s",
                                         (pid,)).fetchall()]
        if not ids:
            if n_parts:
                print("WARNING catalogue.load %s: %d parts measured but none in the Postgres mirror yet; their filter "
                      "facts are left for the next pass" % (pid, n_parts), flush=True)
                return {"stale": True, "why": "not_mirrored"}
            return {"parts": 0, "changed": 0, "healed": 0, "facts_written": 0}
        for attempt in range(REFRESH_TRIES):
            try:
                return partcards.refresh(con, ids, wait_s=0)
            except partcards.Busy:
                if attempt + 1 < REFRESH_TRIES:
                    time.sleep(1.0)
        print("WARNING catalogue.load %s: part records left for the next pass (a sync held the lock %d times)"
              % (pid, REFRESH_TRIES), flush=True)
        return {"stale": True, "why": "busy"}
    except Exception as exc:
        print("WARNING catalogue.load %s: part records not refreshed: %s: %s" % (pid, type(exc).__name__, str(exc)[:200]),
              flush=True)
        return {"stale": True, "why": "error"}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def load(ctx, payload):
    import product
    pid, man = payload.get("product_id"), payload.get("manifest")
    if not isinstance(pid, str) or not derived.PID_RX.match(pid) or man != "framework/products/%s.json" % pid:
        raise _bad("manifest")
    side, target = payload.get("side"), payload.get("target")
    if side != "kernel":
        raise _bad("unknown side")
    if target not in ("primary", "shadow"):
        raise _bad("side / target")
    full = man if os.path.isabs(man) else os.path.join(ROOT, *man.split("/"))
    if not os.path.isfile(full):
        raise ServiceError("no_product", "This design is not in the catalogue.")
    carry = payload.get("carry") or {}
    if not isinstance(carry, dict) or len(json.dumps(carry)) > 60000:
        raise _bad("carry")
    db = None if target == "primary" else derived.kernel_db()
    if db:
        os.makedirs(os.path.dirname(db), exist_ok=True)
    ctx.progress("load")
    kids = Children(ctx)
    index, measured = {}, []
    t0 = time.time()
    try:
        measure = make_measure(ctx, kids, index, measured)
        d2 = make_d2_provider(ctx, kids, index) if target == "primary" else None
        try:
            n_parts, n_interfaces, missing = product.load_side(full, db, side, measure, d2, progress=ctx.progress)
        except (ServiceError, Stopped):
            raise
        except Exception as exc:
            import traceback
            print("catalogue.load %s failed: %s" % (str(ctx.job_id)[:8], traceback.format_exc()[-1500:]), flush=True)
            raise ServiceError("measure_failed", "The design could not be measured.", detail=str(exc)[-300:])
        ms = (time.time() - t0) * 1000.0
        ctx.check()
    finally:
        kids.close()
    records = None
    if target == "primary":
        ctx.progress("records")
        records = _refresh_part_records(pid, n_parts)
    out = dict(carry, product_id=pid, parts=n_parts, interfaces=n_interfaces,
               missing=[list(m) if isinstance(m, (list, tuple)) else m for m in missing],
               side=side, target=target, db=derived.rel(db) if db else None, ms=ms, measured=measured, records=records)
    return results.pack(ctx.job_id, out)


HANDLERS = {"catalogue.load": load}
