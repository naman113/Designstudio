"""The catalogue service's command handlers. The test job is `catalogue.echo` with its twin `catalogue.echo_once`
(same handler; the twin's policy never runs a redelivered command twice) and `catalogue.echo_single`: the same handler,
run one at a time across replicas by `policy.SINGLE`. The real catalogue commands are added below from their modules.

A handler is `fn(ctx, payload) -> dict | Result`.
"""
import json
import os

from framework.common.consumer import Result, ServiceError


def _effects(ctx):
    env = ctx.envelope

    def write(cur):
        cur.execute("INSERT INTO bus.echo_effects (job_id, msg_id, attempt, read_ct, pid) VALUES (%s, %s, %s, %s, %s)",
                    (env["job_id"], env["msg_id"], ctx.attempt, ctx.read_ct, os.getpid()))
    return write


# Numbers that jsonb would change (-0.0, 1e-17 ...): the exactness check sends them through the queues.
EXACT_SAMPLE = [-0.0, 1e-17, -1e-17, 1.0, 0.1 + 0.2, 5e-324, 1.7976931348623157e308, 123456789.12345679, 0.0, 1e22, 1.5e-7]
BIG_REPEAT = 1200                # the "big" case: about 200 KB of text, so the result goes by file reference


def echo(ctx, payload):
    text = str(payload.get("text", "hello"))[:200]
    sleep_s = max(0.0, min(float(payload.get("sleep_s", 0) or 0), 300.0))
    fail = payload.get("fail")
    until = int(payload.get("fail_until_attempt", 2))
    if fail == "error":
        raise ServiceError("echo_failed", "The test job failed on purpose.")
    if fail == "retryable" and ctx.attempt < until:
        raise ServiceError("echo_retry", "The test job failed on purpose and will be tried again.", retryable=True)
    if fail == "raise":
        raise RuntimeError("test poison")
    ctx.progress("echo")
    ctx.sleep(sleep_s)
    result = {"text": text, "attempt": ctx.attempt, "read_ct": ctx.read_ct, "pid": os.getpid()}
    if payload.get("numbers_json") is not None:          # numbers arrive as JSON text, come back in the result
        result["numbers"] = json.loads(payload["numbers_json"])
    elif payload.get("numbers") == "sample":
        result["numbers"] = list(EXACT_SAMPLE)
    elif payload.get("numbers") == "big":
        result["numbers"] = list(EXACT_SAMPLE) * BIG_REPEAT
    return Result(result, _effects(ctx) if payload.get("effect") else None)


HANDLERS = {"catalogue.echo": echo, "catalogue.echo_once": echo, "catalogue.echo_single": echo}

from framework.catalog import svc_trash  # noqa: E402  trash, restore, erase
HANDLERS.update(svc_trash.HANDLERS)

from framework.catalog import svc_upload  # noqa: E402  the upload's catalogue steps (prepare, add)
HANDLERS.update(svc_upload.HANDLERS)

from framework.catalog import svc_drawings  # noqa: E402  the drawing hand-over
HANDLERS.update(svc_drawings.HANDLERS)
from framework.catalog import svc_load  # noqa: E402  catalogue.load, measuring through jobs
HANDLERS.update(svc_load.HANDLERS)
