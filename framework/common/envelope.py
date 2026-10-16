"""The queue message envelope v1. Every message on a queue is one envelope, at most 256 KB.

    new(...)                      a fresh envelope (command, query, progress, reply, control)
    validate(env)                 check every field, fill a missing tenant/account with "default"; raises EnvelopeError
                                  (job_id may be None for a query, control.ping, and the reply to a page read: QUERY_TYPE_RE)
    command_for(job_row, ...)     orchestrator -> <svc>_cmd, from a job row
    progress / reply_ok / reply_error / reply_stopped    a service's answers, copied from the command it answers

`tenant_id` and `account_id` are always "default" (provisioned, copied on every hop, never read to filter).

NUMBERS: pgmq stores a message as jsonb, and jsonb normalises numbers (-0.0 becomes 0.0, 1e-17 becomes
0.00000000000000001), which changes the bytes of a measured result. So a result never travels as nested numbers:
`reply_ok` carries it as the JSON text in the string field `payload.result_json` (made by `pack`, read back by
`unpack`), or by reference in `result_ref` (a file under work/results/, written from the same text). A command
argument that must stay exact does the same with a string field of its own (for example `numbers_json`). Strings and
integers survive jsonb unchanged.
"""
import json
import os
import re
import uuid
from datetime import datetime, timezone

V = 1
KINDS = ("command", "query", "progress", "reply", "control")
DEFAULT = "default"
MAX_BYTES = 256 * 1024
TYPE_RE = re.compile(r"^[a-z]+(\.[a-z_]+)+$")
QUERY_TYPE_RE = re.compile(r"^[a-z]+\.q\.[a-z_]+$")      # a page read (catalogue.q.product): its answer has no job
SENDERS = ("gateway", "orchestrator", "catalogue", "engines", "geometry", "designer", "render", "cli")
FIELDS = ("v", "msg_id", "kind", "type", "job_id", "parent_job_id", "step", "correlation_id", "reply_to", "tenant_id",
          "account_id", "attempt", "deadline", "created_at", "sender", "in_reply_to", "read_ct", "payload")


class EnvelopeError(ValueError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _uuid():
    return str(uuid.uuid4())


def new(kind, type, payload=None, *, sender, job_id=None, parent_job_id=None, step=None, correlation_id=None,
        reply_to=None, attempt=1, deadline=None, in_reply_to=None, read_ct=None):
    return {"v": V, "msg_id": _uuid(), "kind": kind, "type": type, "job_id": job_id, "parent_job_id": parent_job_id,
            "step": step, "correlation_id": correlation_id or _uuid(), "reply_to": reply_to, "tenant_id": DEFAULT,
            "account_id": DEFAULT, "attempt": attempt, "deadline": deadline, "created_at": now_iso(), "sender": sender,
            "in_reply_to": in_reply_to, "read_ct": read_ct, "payload": {} if payload is None else payload}


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
        return isinstance(value, str)
    except (ValueError, AttributeError, TypeError):
        return False


def validate(env):
    """Return `env` with defaults filled, or raise EnvelopeError("<field>: <reason>")."""
    from framework.common import queues
    if not isinstance(env, dict):
        raise EnvelopeError("envelope: not an object")
    for f in FIELDS:
        if f not in env and f not in ("tenant_id", "account_id"):
            raise EnvelopeError("%s: missing" % f)
    for f in ("tenant_id", "account_id"):
        if env.get(f) in (None, ""):
            env[f] = DEFAULT
        if not isinstance(env[f], str):
            raise EnvelopeError("%s: not a string" % f)
    if env["v"] != V or isinstance(env["v"], bool):
        raise EnvelopeError("v: must be %d" % V)
    if not _is_uuid(env["msg_id"]):
        raise EnvelopeError("msg_id: not a uuid")
    if env["kind"] not in KINDS:
        raise EnvelopeError("kind: not one of %s" % ", ".join(KINDS))
    if not isinstance(env["type"], str) or not TYPE_RE.match(env["type"]):
        raise EnvelopeError("type: must look like service.verb")
    if env["job_id"] is None:
        if not (env["kind"] == "query" or env["type"] in ("control.ping", "control.pong")
                or (env["kind"] == "reply" and QUERY_TYPE_RE.match(env["type"]))):
            raise EnvelopeError("job_id: required")
    elif not _is_uuid(env["job_id"]):
        raise EnvelopeError("job_id: not a uuid")
    for f in ("parent_job_id", "in_reply_to"):
        if env[f] is not None and not _is_uuid(env[f]):
            raise EnvelopeError("%s: not a uuid" % f)
    if not _is_uuid(env["correlation_id"]):
        raise EnvelopeError("correlation_id: not a uuid")
    if env["step"] is not None and not isinstance(env["step"], str):
        raise EnvelopeError("step: not a string")
    if env["reply_to"] is not None and env["reply_to"] not in queues.REPLY_TO_ALLOWED:
        raise EnvelopeError("reply_to: not an allowed queue")
    if not isinstance(env["attempt"], int) or isinstance(env["attempt"], bool) or env["attempt"] < 1:
        raise EnvelopeError("attempt: must be an integer of 1 or more")
    for f in ("deadline", "created_at"):
        v = env[f]
        if v is None and f == "deadline":
            continue
        try:
            datetime.fromisoformat(v)
        except (TypeError, ValueError):
            raise EnvelopeError("%s: not a time" % f)
    if not isinstance(env["sender"], str) or not env["sender"]:
        raise EnvelopeError("sender: missing")
    if env["read_ct"] is not None and (not isinstance(env["read_ct"], int) or isinstance(env["read_ct"], bool)):
        raise EnvelopeError("read_ct: not an integer")
    if not isinstance(env["payload"], dict):
        raise EnvelopeError("payload: not an object")
    if len(json.dumps(env)) > MAX_BYTES:
        raise EnvelopeError("size: over %d bytes" % MAX_BYTES)
    return env


def command_for(job_row, *, step, deadline):
    """The step command the orchestrator sends to `<svc>_cmd` for a job row (dict with the orch.jobs columns)."""
    dl = deadline.isoformat(timespec="milliseconds") if isinstance(deadline, datetime) else deadline
    e = new("command", job_row["type"], job_row.get("payload") or {}, sender="orchestrator",
            job_id=str(job_row["job_id"]),
            parent_job_id=str(job_row["parent_job_id"]) if job_row.get("parent_job_id") else None, step=step,
            correlation_id=str(job_row["correlation_id"]), reply_to="orch_reply", attempt=job_row.get("attempt") or 1,
            deadline=dl)
    e["tenant_id"] = job_row.get("tenant_id") or DEFAULT
    e["account_id"] = job_row.get("account_id") or DEFAULT
    return e


def _answer(cmd, kind, payload, *, sender, read_ct, type=None):
    e = new(kind, type or cmd["type"], payload, sender=sender, job_id=cmd["job_id"], parent_job_id=cmd["parent_job_id"],
            step=cmd["step"], correlation_id=cmd["correlation_id"], reply_to=None, attempt=cmd["attempt"],
            deadline=cmd["deadline"], in_reply_to=cmd["msg_id"], read_ct=read_ct)
    e["tenant_id"] = cmd.get("tenant_id") or DEFAULT
    e["account_id"] = cmd.get("account_id") or DEFAULT
    return e


PHASES = ("started", "step", "events", "proc")     # `events` (page events) and `proc` (a running process's facts)


def progress(cmd, phase, *, sender, read_ct, step=None, extra=None):
    """A progress message. `extra` is merged into the payload: page events
    and process facts travel in it, as JSON strings (never nested numbers)."""
    payload = {"phase": phase, "step": step, "pid": os.getpid()}
    if extra:
        payload.update(extra)
    return _answer(cmd, "progress", payload, sender=sender, read_ct=read_ct)


def pack(obj):
    """The exact JSON text of `obj` (Python's float repr round-trips: -0.0 stays -0.0, 1e-17 stays 1e-17)."""
    return json.dumps(obj)


def unpack(text):
    return json.loads(text)


def reply_ok(cmd, result, *, sender, read_ct, result_ref=None):
    """`result` (any JSON-able value) goes out as the string `result_json`, never as nested jsonb numbers."""
    return _answer(cmd, "reply", {"status": "ok", "result": None,
                                  "result_json": None if result is None else pack(result), "result_ref": result_ref},
                   sender=sender, read_ct=read_ct)


def reply_error(cmd, code, user_text, *, sender, read_ct, retryable=False, detail="", dead_pgmq_id=None):
    err = {"code": code, "user_text": user_text, "retryable": bool(retryable), "detail": str(detail)[:500]}
    if dead_pgmq_id is not None:
        err["dead_pgmq_id"] = dead_pgmq_id
    return _answer(cmd, "reply", {"status": "error", "error": err}, sender=sender, read_ct=read_ct)


def reply_stopped(cmd, *, sender, read_ct, result=None):
    """`result`: what the service has to say about a job it stopped, a dict of strings."""
    payload = {"status": "stopped"}
    if result is not None:
        payload["result"] = result
    return _answer(cmd, "reply", payload, sender=sender, read_ct=read_ct)
