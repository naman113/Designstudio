"""Per-type limits, retries and redelivery rules, plus the consumer constants.

Every job type the services run has a row here. An unknown type has no policy, and its submit ends `failed` with `unknown_type`.
"""
from dataclasses import dataclass

from framework.common import queues


@dataclass(frozen=True)
class Policy:
    limit_s: int            # deadline = dispatch time + limit_s (or the submit's own deadline, if earlier)
    retries: int            # orchestrator-level retries after a retryable error reply
    backoff_s: tuple        # delay before retry n (n = 1..retries)
    redeliver: str          # "run": a redelivered command runs again; "fail": it ends the job `interrupted`


# LONG_RUN_S is the one number: a long request, its commands and the jobs it waits on may each run 300 minutes. A job's policy row
# is its worker's op limit + 60 s (the inner watchdog fires first, with its own sentence); a recipe parent that waits for several
# long steps gets the sum of its steps' rows (see PARENT_STEPS below).
# The watchdogs NEST, innermost first, nothing below 300 minutes:
#   LONG_RUN_S 18000      the runner's and the workers' op limits (the inner watchdog, with its own sentence)
#   row = op + 60         a leaf job's policy row;  every wait for a job = its row + 30 (wait_s)
#   CLI_S = 18300         the CLI's command tool (LONG_RUN_S + 300): above every command it runs
#   TURN_WALL_S = 18600   an outer wall clock (LONG_RUN_S + 600): above the CLI, so a command's own limit always fires first
#   a recipe parent = wait gate + the sum of its steps' rows + 300
LONG_RUN_S = 18000
LONG_ROW_S = LONG_RUN_S + 60
JOB_WAIT_PAD_S = 30
CLI_S = LONG_RUN_S + 300
TURN_WALL_S = LONG_RUN_S + 600
CHECK_WAIT_MAX_S = LONG_RUN_S           # tools/measure.py --check: seconds the pair check may be waited for (and the guard's clamp)
CHECK_WAIT_DEFAULT_S = LONG_RUN_S
# Job types that hold an outer wall clock (TURN_WALL_S); none in this product
TURN_TYPES = ()

POLICIES = {
    "catalogue.echo": Policy(600, 2, (2, 10), "run"),
    "catalogue.echo_once": Policy(600, 0, (), "fail"),     # test type: a redelivered command fails instead of running again
    # read-only catalogue commands, safe to run again
    "catalogue.find": Policy(60, 2, (2, 10), "run"),
    "catalogue.list": Policy(60, 2, (2, 10), "run"),
    "catalogue.show": Policy(60, 2, (2, 10), "run"),
    "catalogue.echo_single": Policy(600, 2, (2, 10), "run"),   # the test twin of one-at-a-time work
    # the geometry jobs. limit_s = the worker's op time limit + 60 s (framework/kernel/svc_handlers.py
    # TIMEOUT_S: measure 900, signature 900, read_step 300 per file for the 20-file maximum, mesh 300, step_out 300,
    # check 600, topology 120); one retry after a retryable error (a lost worker), a redelivered command runs again.
    "geometry.measure": Policy(LONG_ROW_S, 1, (2,), "run"),
    "geometry.signature": Policy(LONG_ROW_S, 1, (2,), "run"),
    "geometry.read_step": Policy(LONG_ROW_S, 1, (2,), "run"),
    "geometry.mesh": Policy(LONG_ROW_S, 1, (2,), "run"),
    "geometry.step_out": Policy(LONG_ROW_S, 1, (2,), "run"),
    "geometry.topology": Policy(180, 1, (2,), "run"),
    "geometry.selftest": Policy(180, 0, (), "run"),
    # measuring commands on a file inside the copy (measure 900 s + 60, pair check 600 s + 60)
    "geometry.measure_file": Policy(LONG_ROW_S, 1, (2,), "run"),
    # one part's own 3D view and its STEP file (op budget 120 s; measured: median 0.017 s, max 4.8 s to mesh, 9.4 s to read
    # the largest STEP file once)
    "geometry.part_view": Policy(180, 1, (2,), "run"),
    "geometry.part_step": Policy(180, 1, (2,), "run"),
    # parts' exact extents and solids (op budget 900 s for up to 400 parts)
    "geometry.tight_extent": Policy(LONG_ROW_S, 1, (2,), "run"),
    # recipes (a parent is never retried; its children follow their own rows), then trash, restore, erase
    "recipe.echo_pair": Policy(600, 0, (), "run"),          # test recipe (parent)
    "product.erase": Policy(960, 0, (), "run"),             # parent: wait gate 120 s + the two steps (catalogue.erase, geometry.drop_derived)
    "catalogue.trash": Policy(60, 1, (2,), "run"),
    "catalogue.restore": Policy(60, 1, (2,), "run"),
    "catalogue.erase": Policy(600, 1, (2,), "run"),
    "geometry.drop_derived": Policy(120, 2, (2, 10), "run"),   # product.erase's second step (was the engines service's)
    "dual.mesh": Policy(LONG_ROW_S, 0, (), "run"),
    "dual.step_out": Policy(LONG_ROW_S, 0, (), "run"),
    "dual.measure": Policy(LONG_ROW_S, 0, (), "run"),
    # an upload as a recipe. add_prepare / add write the product's folder and manifest (one at a time, SINGLE);
    # load measures (a 34-solid design takes minutes); read_step reads up to 20 files; the parent is never retried (its steps follow their own rows).
    "catalogue.add_prepare": Policy(120, 1, (2,), "run"),
    "catalogue.add": Policy(120, 1, (2,), "run"),
    "catalogue.load": Policy(LONG_ROW_S, 1, (2,), "run"),      # measures through child jobs (measure plus signatures)
    "product.load": Policy(LONG_ROW_S, 0, (), "run"),          # the parent of the first engine's load, the second engine's and their comparison
    "upload.build": Policy(LONG_ROW_S, 0, (), "run"),
    # the views sheet (op 600 s + 60)
    "geometry.draw_overview": Policy(LONG_ROW_S, 1, (2,), "run"),
    # the part drawings (op budget 1800 s, a part over 240 s stopped); equal to geometry.read_step's, so the service's
    # stop grace period does not grow
    "geometry.draw_parts": Policy(LONG_ROW_S, 1, (2,), "run"),
    "catalogue.publish_drawings": Policy(120, 1, (2,), "run"),
    "catalogue.drawing_bom": Policy(60, 2, (2, 10), "run"),     # read-only parts list for the drawings
    "catalogue.publish_drawing": Policy(120, 1, (2,), "run"),    # one published sheet; an idempotent copy
    "drawings.make": Policy(LONG_ROW_S, 0, (), "run"),                # parent: its time counts from submit, the wait included
    # proposed defaults for later phases (the phase that adds a type confirms its row):
    # catalogue.record|register|remove|sync 1200, 1, (2,), run
}

# Drawings use at most 2 of the 4 geometry workers: at most this many jobs
# of a type are dispatched, running or stopping at once; the rest stay queued (dispatcher.dispatch_ready).
TYPE_CAPS = {"geometry.draw_overview": 1, "geometry.draw_parts": 1}

MAX_READS = 5                       # a command runs at most 5 times (reads 1..5)
EXTEND_EVERY_S, EXTEND_BY_S = 10, 30
SWEEP_EVERY_S = 5
STOP_GRACE_S = 10                   # no `stopped` reply within this -> stopped anyway (forced)
INTERRUPTED_TEXT = {}               # the plain sentence of a command read again after its service died (type -> user_text)
FALLBACK_READ_S = 1.0
CAPS = {}                           # orchestrator-held caps (none in this product)
CAP_TYPES = {}                      # target -> the types its cap counts
# geometry 4: one handler thread per warm kernel worker.
POOL = {"catalogue": 3, "geometry": 4}

# Job types that run one at a time across every replica of their service (type -> resource). The
# consumer holds the lock `single:<resource>` from just after `progress started` to the end of the final transaction.
# A phase that adds a type writing a SQLite file, a manifest or a file in place adds its row here in the same commit.
SINGLE = {"catalogue.echo_single": "check.single"}
SINGLE.update({"catalogue." + v: "catalog.db" for v in (
    "load", "add", "add_prepare", "record", "register", "remove", "trash", "restore", "erase", "sync")})
SINGLE["catalogue.publish_drawings"] = "drawings.store"       # the only writer of static/files/drawings/<pid>/
SINGLE["catalogue.publish_drawing"] = "drawings.store"        # the same one writer, for a single sheet in static/files/
SINGLE["geometry.drop_derived"] = "derived.store"             # geometry scales to 4 replicas; one erase of files at a time


# A recipe parent's limit covers its wait gate (recipes.py wait_limit_s, 18000 s) and the rows of every step it can
# run one after the other, plus 300 s (the orchestrator's own hops). Computed from the rows above, so they cannot drift apart.
GATE_S = LONG_RUN_S
PARENT_STEPS = {
    "catalogue.load": (("geometry.measure", "geometry.signature"), 0),
    "dual.mesh": (("geometry.mesh",), 0),
    "dual.step_out": (("geometry.step_out",), 0),
    "dual.measure": (("geometry.measure",), 0),
    "product.load": (("catalogue.load",), GATE_S),
    "upload.build": (("catalogue.add_prepare", "geometry.read_step", "catalogue.add", "catalogue.load", "geometry.mesh"), GATE_S),
    "drawings.make": (("catalogue.drawing_bom", "geometry.draw_overview", "catalogue.publish_drawings", "geometry.draw_parts",
                       "catalogue.publish_drawings"), GATE_S),
}
PARENT_MARGIN_S = 300


def _fill_parents():
    # catalogue.load first (product.load and upload.build count its row); the rest in dependency order
    for t in ("catalogue.load", "dual.mesh", "dual.step_out", "dual.measure", "product.load", "upload.build", "drawings.make"):
        steps, gate = PARENT_STEPS[t]
        old = POLICIES[t]
        limit = gate + sum(POLICIES[s].limit_s for s in steps) + PARENT_MARGIN_S
        POLICIES[t] = Policy(limit, old.retries, old.backoff_s, old.redeliver)


def wait_s(job_type):
    """How long a caller waits for a job of this type: its row + JOB_WAIT_PAD_S, so the orchestrator's own timeout (with the
    plain sentence) always comes before the caller gives up."""
    return POLICIES[job_type].limit_s + JOB_WAIT_PAD_S


_fill_parents()


def single(job_type):
    """The resource a type must hold one at a time across replicas, or None (free on every replica)."""
    return SINGLE.get(job_type)


def nack_delay_s(read_ct):
    return min(2 ** read_ct, 30)    # 2, 4, 8, 16 s, then 30 s


def policy(job_type):
    """The Policy for a job type, or None when the type is not known."""
    return POLICIES.get(job_type)


def target(job_type):
    """The service a type goes to: the text before the first dot (catalogue, geometry)."""
    svc = job_type.split(".", 1)[0]
    if svc not in queues.SERVICES:
        raise ValueError("no service %r for type %r" % (svc, job_type))
    return svc
