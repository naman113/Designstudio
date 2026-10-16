"""The geometry service: the in-process kernel as its own process.

    python -m framework.kernel.service          (started by the orchestrator: python scripts/services.py start)

The geometry jobs (measure, read_step, mesh, step_out, check, signature, topology, selftest) run on a pool
of 4 warm worker processes (`GEO_WORKERS` changes the count): framework/kernel/svc_handlers.py answers them, worker_pool.py
runs them, ops.py is the work. /health shows the pool.

The pool is created in main(), never at import time: the workers are `spawn`-ed and re-import this module as __mp_main__.
"""
import threading

from framework.common import config
from framework.common.service import Service
from framework.kernel import svc_handlers

HANDLERS = svc_handlers.HANDLERS          # command type -> function(ctx, payload) -> result


def main():
    threading.Thread(target=svc_handlers.start_pool, args=(config.ROOT,), name="geometry-pool-start",
                     daemon=True).start()          # /health is up at once; the workers are warm within about 4 s
    svc = Service("geometry", HANDLERS)
    svc.extra_health = lambda: {"pool": svc_handlers.pool_stats()}
    svc.run()


if __name__ == "__main__":
    main()
