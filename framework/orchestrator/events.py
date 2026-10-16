"""Job events. `emit()` is called only by the dispatcher thread, inside the transaction that
changes the job, so event_id order is commit order and readers can follow `event_id > last` with no gaps.

`meta` is shown to pages, so only the keys listed for the event type are kept; `content` (free reply text)
is never shown.
"""
import json

from psycopg.types.json import Jsonb

from framework.common import slog

META_KEYS = {
    "job.queued": ("type", "target", "by", "parent", "after"),   # `parent` on a recipe's child; `after` on a job the orchestrator started after another (drawings after a load)
    "job.dispatched": ("queue", "pgmq_id", "deadline"),
    "job.started": ("read_ct", "pid"),
    "job.progressed": ("step", "done", "total", "eta_s"),
    "job.succeeded": ("ms",),
    "job.failed": ("ms",),
    "job.retrying": ("delay_s", "next_attempt"),
    "job.redelivered": ("read_ct",),
    "message.dead_lettered": ("queue", "dead_pgmq_id", "reads"),
    "job.timed_out": ("limit_s",),
    "job.stopping": (),
    "job.stopped": ("stop_ms", "forced"),
    "job.adopted": ("was",),
    "job.text": (),
    # a recipe parent's steps
    "job.step_dispatched": ("step", "child"),
    "job.step_finished": ("step", "child", "state"),
    "job.position": ("pos",),         # a waiting job's place in the line (1 = next in line)
    # a turn's page events; their text is in `content`, never shown on Activity
    "turn.status": ("n",),
    "turn.step": ("n",),
    "turn.text": ("n",),
    "turn.team": ("n",),
    "turn.done": (),
}


def emit(cur, job_id, type, *, state, attempt, service=None, step=None, read_ct=None, error_code=None, user_text=None,
         meta=None, content=None):
    """Write one event (and bump the job's sequence) on `cur`, which is a cursor or connection inside the caller's
    transaction. Returns (event_id, seq). Commits NOTIFY job_events with the transaction."""
    allowed = META_KEYS.get(type)
    if allowed is None:
        raise ValueError("unknown event type %r" % type)
    kept = {k: v for k, v in (meta or {}).items() if k in allowed}
    if len(kept) != len(meta or {}):
        dropped = sorted(set(meta) - set(kept))
        print("events: dropped meta keys %s of %s" % (dropped, type), flush=True)
    job = cur.execute("UPDATE orch.jobs SET last_seq = last_seq + 1, updated_at = clock_timestamp() "
                      "WHERE job_id = %s RETURNING last_seq, parent_job_id, correlation_id, type, tenant_id, account_id",
                      (job_id,)).fetchone()
    seq = job[0]
    event_id = cur.execute(
        "INSERT INTO orch.job_events (job_id, seq, type, state, service, step, attempt, read_ct, error_code, user_text, "
        "meta, content) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING event_id",
        (job_id, seq, type, state, service, step, attempt, read_ct, error_code, user_text, Jsonb(kept),
         Jsonb(content) if content is not None else None)).fetchone()[0]
    note = json.dumps({"e": event_id, "j": str(job_id), "s": seq, "t": type, "st": state}, separators=(",", ":"))
    cur.execute("SELECT pg_notify('job_events', %s)", (note,))
    # One structured-log line per event (written after the transaction commits: the dispatcher's `_tx()`). Only codes
    # and numbers, never `user_text`, `meta` text or `content`. The job's ids come from its own row.
    slog.log(type, job_id=job_id, parent_job_id=job[1], correlation_id=job[2], type=job[3], tenant_id=job[4],
             account_id=job[5], state=state, target=service, step=step, attempt=attempt, read_ct=read_ct,
             error_code=error_code, ms=kept.get("ms"))
    return event_id, seq
