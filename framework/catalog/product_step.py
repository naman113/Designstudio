"""Write a catalogued product (or one of its parts) as STEP, for drawings and hand-off.

    python framework/catalog/product_step.py <product_id>              -> work/step/<id>.step
    python framework/catalog/product_step.py <product_id> --ref CRANK  -> work/step/<id>-CRANK.step

In-process (tests, offline): framework.kernel.shapes.product_parts resolves the manifest's parts
through the kernel, and framework.kernel.io.write_step writes them as one labelled
AP214/mm STEP file (parts at their assembled positions, named by reference). With BC_SERVICES=1 a whole product is the
`dual.step_out` job, the same one the web download runs (this module does not import the engines layer). Prints the
output path.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)



def write_product_step(product_id: str, out_path: str | None = None, ref: str | None = None) -> str:
    """Write `product_id` (or, when `ref` is given, just that one part) as one
    labelled AP214/mm STEP file. Occurrences of the same ref (a qty > 1 part) are
    written as REF, REF_2, REF_3, ... Returns the output path. Raises ValueError
    when the product or the requested part does not exist."""
    from framework.kernel import io as kio
    from framework.kernel.shapes import product_parts
    man = os.path.join(ROOT, "framework", "products", product_id + ".json")
    if not os.path.exists(man):
        raise ValueError("no product %s" % product_id)
    parts = product_parts(man)
    if ref:
        parts = [(r, shape) for r, shape in parts if r == ref]
        if not parts:
            raise ValueError("no part %s in %s" % (ref, product_id))
    if out_path is None:
        out_path = os.path.join(ROOT, "work", "step", "%s%s.step" % (product_id, "-" + ref if ref else ""))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    named, used = [], {}
    for r, shape in parts:
        used[r] = used.get(r, 0) + 1
        name = r if used[r] == 1 else "%s_%d" % (r, used[r])
        named.append((name, shape))
    kio.write_step(named, out_path, root_name=product_id)
    return out_path


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__); return 2
    pid = argv[0]
    ref = argv[argv.index("--ref") + 1] if "--ref" in argv else None
    from framework.common import cliclient
    if cliclient.wanted() and not ref:
        # whole product: the services make it (the first engine's writer answers, the second engine's is compared)
        man = os.path.join(ROOT, "framework", "products", pid + ".json")
        if not os.path.exists(man):
            print("no product %s" % pid); return 1
        from framework.common import jobcall
        from framework.common import policy as _policy              # the shared job client (not the studio's)
        try:
            res = jobcall.call("dual.step_out", {"product_id": pid}, sender="cli", timeout_s=_policy.wait_s("dual.step_out"))
        except (jobcall.JobError, TimeoutError):
            print("The STEP file could not be made right now."); return 1
        print(os.path.join(ROOT, *res["path"].split("/")))
        return 0
    try:
        out = write_product_step(pid, ref=ref)
    except ValueError as e:
        print(str(e)); return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
