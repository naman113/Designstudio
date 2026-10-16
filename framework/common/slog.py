"""The structured log: one JSON line per step of a job, the same way in every process.

A line says WHEN, WHERE (service, process), WHAT happened (an event from a closed list) and WHICH job (job_id,
correlation_id, tenant_id, account_id, type, step), so `svc_check.py logs --job <id>` and the Activity page's Log trail can
follow one request through the gateway, the orchestrator, the catalogue and the geometry service, merged by time.

There is NO free-text field. Only the whitelisted keys of `FIELDS` reach a line, and every value must match its rule (an id
is a uuid, a name is a lowercase code, a number is a number). A message, a reply, a payload or a key cannot be
written through this module. A process's own prints stay in its plain log (`work/logs/services/<name>.log`).

File: `work/logs/services/<service>/<UTC date>-<pid>.jsonl`. One file per process per day, appended by that process only, so
lines of different processes never interleave. One line is at most 1,024 bytes (optional keys are dropped to fit).

Bounds: a file stops growing at MAX_FILE_BYTES (64 MB): later lines are dropped and counted, and one
`svc.dropped n=` line is written when the day rolls or the process ends. `setup()` deletes `*.jsonl` older than
config.retention_days() (7 days; BC_RETENTION_DAYS overrides, 0 keeps everything) and writes `svc.prune n=`; the
orchestrator's daily retention run calls `prune()` again for processes that stay up for days. The page's polling read (`catalogue.q.state`) is logged only when it is
slow (over 1 s) or failed: it is one line per page per 8 s and says nothing a healthy poll needs said.

    setup(service, proc)      the process identity (Reporter / Dispatcher / Supervisor call it)
    log(event, kind=, level=, **fields)     never raises
    context(env, **fields)    job context (contextvars: set it inside the thread that handles the job)
    deferred()                lines logged inside are written when the block ends normally and dropped when it raises
    validate_line(d)          the problems of one parsed line ([] = valid)
    files / read / trail      read the logs back (a bad line is skipped and counted)
    stats()                   what was refused, dropped or failed

Only `framework.common.config` is imported from the framework: every service shares this file.
"""
import atexit
import contextvars
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from framework.common import config

LOG_DIR = os.path.join(config.ROOT, "work", "logs", "services")
MAX_LINE_BYTES = 1024
MAX_FILE_BYTES = 64 * 1024 * 1024      # a file stops growing here; later lines are dropped and counted (svc.dropped)
QUIET_QUERY_TYPES = ("catalogue.q.state",)   # the page's polling read: logged only when slow or failed
QUIET_SLOW_MS = 1000
DEFAULT = "default"        # tenant and account (provisioned, never read to filter)

SERVICES = tuple(config.ORDER) + ("cli",)
PROCS = ("service", "tool", "worker", "cli")
LEVELS = ("info", "warn", "error")
KINDS = ("job", "query", "svc")

EVENTS = frozenset("""
svc.start svc.ready svc.drain svc.error db.reconnect msg.bad svc.child_start svc.child_exit svc.child_restart
svc.child_failed svc.prune svc.dropped retention.pruned
cmd.read cmd.started cmd.progress cmd.done cmd.nack cmd.dead_letter cmd.withdrawn cmd.refused ctl.stop ctl.ping
cmd.sent reply.read reply.late
client.submit client.answer client.stop
chat.submit chat.stream_open chat.stream_end upload.received query.sent query.answered
query.done
designer.start designer.step designer.end designer.stop designer.limit proc.start proc.exit
fc.run fc.cache
pool.run pool.recycle
render.run
""".split())
ORCH_EVENT_RX = re.compile(r"^(job|message|turn)\.[a-z_]+$")      # every event type the orchestrator writes to orch.job_events

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_CODE = re.compile(r"^[a-z0-9_.:-]{1,64}$")
_TENANT = re.compile(r"^[a-z0-9_-]{1,40}$")
_TYPE = re.compile(r"^[a-z]+(\.[a-z_]+)+$")
_PRODUCT = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_REF = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_EXC = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,80}$")
_VERSION = re.compile(r"^([0-9a-f]{4,40}|unknown)$")


def _uuid(v):
    if isinstance(v, uuid.UUID):
        v = str(v)
    if not isinstance(v, str):
        return None
    v = v.lower()
    return v if _UUID.match(v) else None


def _rx(rx):
    def check(v):
        return v if isinstance(v, str) and rx.match(v) else None
    return check


def _int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, float) and math.isfinite(v):
        return int(round(v))
    return v if isinstance(v, int) else None


def _float(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return round(float(v), 3)


# The keys after the seven always-keys, in line order. The value is the rule: it returns the clean value or None (= refused).
FIELDS = {
    "job_id": _uuid, "parent_job_id": _uuid, "correlation_id": _uuid, "msg_id": _uuid,
    "tenant_id": _rx(_TENANT), "account_id": _rx(_TENANT),
    "type": _rx(_TYPE),
    "step": _rx(_CODE), "state": _rx(_CODE), "status": _rx(_CODE), "error_code": _rx(_CODE), "op": _rx(_CODE),
    "queue": _rx(_CODE), "target": _rx(_CODE), "reason": _rx(_CODE), "lane": _rx(_CODE),
    "product_id": _rx(_PRODUCT), "ref": _rx(_REF), "exc": _rx(_EXC), "version": _rx(_VERSION),
    "attempt": _int, "read_ct": _int, "pgmq_id": _int, "ms": _int, "n": _int, "child_pid": _int, "exit_code": _int,
    "port": _int, "bytes": _int, "files": _int, "days": _int,
    "seconds": _float,
}
FIELD_ORDER = tuple(FIELDS)
CONTEXT_KEYS = ("job_id", "parent_job_id", "correlation_id", "tenant_id", "account_id", "type", "step", "attempt",
                "read_ct", "msg_id")
ALWAYS = ("ts", "service", "proc", "pid", "level", "kind", "event")
# What a line gives up first when it is over MAX_LINE_BYTES (the optional keys, least useful first).
TRIM_ORDER = ("version", "ref", "product_id", "lane", "reason", "target", "queue", "op", "exc", "bytes", "port", "n",
              "child_pid", "exit_code", "pgmq_id", "seconds", "msg_id", "parent_job_id", "read_ct", "attempt", "state")

_ctx = contextvars.ContextVar("slog_ctx", default={})
_deferred = contextvars.ContextVar("slog_deferred", default=None)
_lock = threading.RLock()
_state = {"service": None, "proc": None, "fh": None, "path": None, "pid": None, "warned": 0.0, "bytes": 0, "dropped": 0,
          "pruned": False, "atexit": False}
_stats = {"lines": 0, "dropped_keys": 0, "rejected_values": 0, "truncated": 0, "refused_events": 0, "errors": 0,
          "read_bad": 0, "capped": 0, "quiet": 0}


def setup(service, proc="service"):
    """The identity of this process (every line carries it). A second call replaces the first."""
    if service not in SERVICES:
        service = "cli"
    _state["service"], _state["proc"] = service, proc if proc in PROCS else "service"
    if not _state["pruned"]:
        _state["pruned"] = True
        n = prune()
        if n:
            log("svc.prune", kind="svc", n=n)


def prune(keep_days=None, today=None):
    """Delete the *.jsonl files of every service folder that are older than `keep_days` (by the date in the file name; a name
    without a date by its modification time). `keep_days` None = config.retention_days(); 0 keeps everything.
    Returns how many were deleted; never raises."""
    n = 0
    if keep_days is None:
        keep_days = config.retention_days()
    if keep_days <= 0:
        return 0
    today = today or datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=keep_days)
    try:
        subs = [e.path for e in os.scandir(LOG_DIR) if e.is_dir()]
    except OSError:
        return 0
    for sub in subs:
        try:
            names = os.listdir(sub)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(sub, name)
            try:
                try:
                    day = date.fromisoformat(name[:10])
                except ValueError:
                    day = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).date()
                if day < cutoff:
                    os.remove(path)
                    n += 1
            except OSError:
                continue          # in use by a process, or gone already
    return n


def _identity():
    if _state["service"] is None:
        name = os.environ.get("BC_SERVICE", "")
        if name in config.ORDER:
            _state["service"], _state["proc"] = name, "tool"
        else:
            _state["service"], _state["proc"] = "cli", "cli"
    return _state["service"], _state["proc"]


def _now():
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


def _stat(key, n=1):
    with _lock:
        _stats[key] += n


def stats():
    with _lock:
        return dict(_stats)


def _warn_once(exc):
    now = time.monotonic()
    if now - _state["warned"] > 60:
        _state["warned"] = now
        try:
            sys.stderr.write("slog: %s\n" % type(exc).__name__)
        except Exception:
            pass


# ---- context --------------------------------------------------------------------------------------------------------------
@contextmanager
def context(env=None, **fields):
    """Job context for the lines logged inside (this thread only: set it inside each thread). `env` is an envelope or a job
    row (only the CONTEXT_KEYS are taken); `fields` are any other whitelisted keys (queue, pgmq_id, ...). Nested
    contexts merge."""
    merged = dict(_ctx.get())
    if env:
        for k in CONTEXT_KEYS:
            v = env.get(k)
            if v is not None:
                merged[k] = v
    for k, v in fields.items():
        if v is not None:
            merged[k] = v
    token = _ctx.set(merged)
    try:
        yield
    finally:
        _ctx.reset(token)


# ---- writing --------------------------------------------------------------------------------------------------------------
def _clean(fields):
    out = {}
    for k, v in fields.items():
        if v is None:
            continue
        rule = FIELDS.get(k)
        if rule is None:
            _stat("dropped_keys")
            continue
        good = rule(v)
        if good is None:
            _stat("rejected_values")
            out[k] = "?"
        else:
            out[k] = good
    return out


def _dumps(d):
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


def _build(event, kind, level, fields):
    service, proc = _identity()
    proc = fields.pop("proc", None) or proc
    if proc not in PROCS:
        proc = "service"
    merged = {}
    if kind != "svc":
        merged.update(_ctx.get())
    merged.update({k: v for k, v in fields.items() if v is not None})
    clean = _clean(merged)
    if kind in ("job", "query"):
        clean.setdefault("tenant_id", DEFAULT)
        clean.setdefault("account_id", DEFAULT)
    line = {"ts": _now(), "service": service, "proc": proc, "pid": os.getpid(), "level": level, "kind": kind,
            "event": event}
    for k in FIELD_ORDER:
        if k in clean:
            line[k] = clean[k]
    text = _dumps(line)
    if len(text.encode("utf-8")) > MAX_LINE_BYTES:
        _stat("truncated")
        for k in TRIM_ORDER:
            if k in line:
                del line[k]
                text = _dumps(line)
                if len(text.encode("utf-8")) <= MAX_LINE_BYTES:
                    break
    return line, text


def _emit_dropped_locked():
    """One `svc.dropped n=` line into the open file (past the cap on purpose: it is one short line), then reset the count."""
    n = _state["dropped"]
    fh = _state["fh"]
    if not n or fh is None:
        return
    _state["dropped"] = 0
    _line, text = _build("svc.dropped", "svc", "warn", {"n": n})
    try:
        fh.write(text + "\n")
        _state["bytes"] += len(text.encode("utf-8")) + 1
    except Exception as exc:
        _warn_once(exc)


def _at_exit():
    try:
        with _lock:
            _emit_dropped_locked()
    except Exception:
        pass


def _write(text):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    service = _identity()[0]
    pid = os.getpid()
    path = os.path.join(LOG_DIR, service, "%s-%d.jsonl" % (today, pid))
    with _lock:
        if _state["fh"] is None or _state["path"] != path or _state["pid"] != pid:
            if _state["fh"] is not None:
                if _state["pid"] == pid:
                    _emit_dropped_locked()            # the day rolled: say what the old file lost
                try:
                    _state["fh"].close()
                except Exception:
                    pass
            _state["dropped"] = 0 if _state["pid"] != pid else _state["dropped"]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _state["fh"] = open(path, "a", encoding="utf-8", buffering=1, newline="\n")
            _state["path"], _state["pid"] = path, pid
            try:
                _state["bytes"] = os.path.getsize(path)
            except OSError:
                _state["bytes"] = 0
            if not _state["atexit"]:
                _state["atexit"] = True
                atexit.register(_at_exit)
        size = len(text.encode("utf-8")) + 1
        if _state["bytes"] + size > MAX_FILE_BYTES:
            _state["dropped"] += 1
            _stats["capped"] += 1
            return
        _state["fh"].write(text + "\n")
        _state["bytes"] += size
        _stats["lines"] += 1


def _quiet(event, kind, level, fields):
    """True for the page's polling read when it went well and fast: its lines are not written (see the module note)."""
    if kind != "query":
        return False
    qtype = fields.get("type") or _ctx.get().get("type")
    if qtype not in QUIET_QUERY_TYPES:
        return False
    if event == "query.sent":
        return True
    if level != "info" or fields.get("status") not in (None, "ok"):
        return False
    ms = fields.get("ms")
    return not (isinstance(ms, (int, float)) and ms > QUIET_SLOW_MS)


def log(event, *, kind="job", level="info", **fields):
    """Write one line (never raises). An event outside the closed list is refused and counted."""
    try:
        if kind not in KINDS:
            kind = "svc"
        if level not in LEVELS:
            level = "info"
        service = _identity()[0]
        if event not in EVENTS and not (service == "orchestrator" and ORCH_EVENT_RX.match(event or "")):
            _stat("refused_events")
            return
        if _quiet(event, kind, level, fields):
            _stat("quiet")
            return
        line, text = _build(event, kind, level, fields)
        pending = _deferred.get()
        if pending is not None:
            pending.append(text)
        else:
            _write(text)
    except Exception as exc:
        _stat("errors")
        _warn_once(exc)


@contextmanager
def deferred():
    """Lines logged inside are written, in order, when the block ends normally; when it raises they are dropped and one
    `svc.error` line (the exception class only) is written instead. Use it OUTSIDE a transaction block, so a rolled-back
    transaction writes nothing and a committed one writes everything. A nested block joins the outer one."""
    if _deferred.get() is not None:
        yield
        return
    pending = []
    token = _deferred.set(pending)
    try:
        yield
    except BaseException as exc:
        _deferred.reset(token)
        log("svc.error", kind="svc", level="error", exc=type(exc).__name__)
        raise
    else:
        _deferred.reset(token)
        try:
            for text in pending:
                _write(text)
        except Exception as exc:
            _stat("errors")
            _warn_once(exc)


# ---- reading --------------------------------------------------------------------------------------------------------------
def validate_line(d):
    """The problems of one parsed line, as short sentences without any value from the line ([] = valid)."""
    if not isinstance(d, dict):
        return ["not an object"]
    bad = []
    for k in ALWAYS:
        if k not in d:
            bad.append("missing %s" % k)
    if bad:
        return bad
    if not (isinstance(d["ts"], str) and _TS.match(d["ts"])):
        bad.append("ts")
    if d["service"] not in SERVICES:
        bad.append("service")
    if d["proc"] not in PROCS:
        bad.append("proc")
    if _int(d["pid"]) is None:
        bad.append("pid")
    if d["level"] not in LEVELS:
        bad.append("level")
    if d["kind"] not in KINDS:
        bad.append("kind")
    ev = d["event"]
    if not (isinstance(ev, str) and (ev in EVENTS or (d["service"] == "orchestrator" and ORCH_EVENT_RX.match(ev)))):
        bad.append("event")
    for k, v in d.items():
        if k in ALWAYS:
            continue
        rule = FIELDS.get(k)
        if rule is None:
            bad.append("key %s" % k if isinstance(k, str) and _CODE.match(k) else "key")
        elif rule(v) is None:
            bad.append("value of %s" % k)
    if d["kind"] == "job" and "job_id" not in d:
        bad.append("job line without job_id")
    if d["kind"] == "query" and "correlation_id" not in d:
        bad.append("query line without correlation_id")
    if d["kind"] in ("job", "query") and ("tenant_id" not in d or "account_id" not in d):
        bad.append("no tenant or account")
    try:
        if len(_dumps(d).encode("utf-8")) > MAX_LINE_BYTES:
            bad.append("over %d bytes" % MAX_LINE_BYTES)
    except Exception:
        bad.append("not serialisable")
    return bad


def files(day_from, day_to, modified_since=None):
    """Every *.jsonl of the UTC days `day_from` .. `day_to` (dates, inclusive), all services. With `modified_since` (a
    datetime) only the files written to since then: a file last written before a window began has no line in it."""
    floor = modified_since.timestamp() - 1.0 if isinstance(modified_since, datetime) else None
    if isinstance(day_from, datetime):
        day_from = day_from.astimezone(timezone.utc).date()
    if isinstance(day_to, datetime):
        day_to = day_to.astimezone(timezone.utc).date()
    days = []
    d = day_from
    while d <= day_to:
        days.append(d.strftime("%Y-%m-%d") + "-")
        d += timedelta(days=1)
    out = []
    try:
        subs = sorted(e.path for e in os.scandir(LOG_DIR) if e.is_dir())
    except OSError:
        return out
    for sub in subs:
        try:
            for name in sorted(os.listdir(sub)):
                if name.endswith(".jsonl") and name.startswith(tuple(days)):
                    path = os.path.join(sub, name)
                    if floor is not None:
                        try:
                            if os.path.getmtime(path) < floor:
                                continue
                        except OSError:
                            continue
                    out.append(path)
        except OSError:
            continue
    return out


def _iter(paths, needles=None):
    """(parsed line, line number, path) for every line; a line that does not parse is skipped and counted."""
    for p in paths:
        try:
            fh = open(p, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for n, raw in enumerate(fh, 1):
                raw = raw.rstrip("\r\n")
                if not raw:
                    continue
                if needles is not None and not any(s in raw for s in needles):
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    _stat("read_bad")
                    continue
                if isinstance(d, dict):
                    yield d, n, p
                else:
                    _stat("read_bad")


def read(paths):
    """The parsed lines of these files (tolerant: a bad line is skipped and counted as read_bad)."""
    for d, _n, _p in _iter(paths):
        yield d


def _stamp(t):
    t = t.astimezone(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


def trail(job_ids, correlation_ids, since, until, modified_since=None):
    """Every line whose job_id or parent_job_id is in `job_ids`, or whose correlation_id is in `correlation_ids`, between
    `since` and `until` (datetimes), in time order: (ts, service order, pid, line number)."""
    jobs = {str(j).lower() for j in (job_ids or ())}
    corrs = {str(c).lower() for c in (correlation_ids or ())}
    needles = jobs | corrs
    if not needles:
        return []
    lo, hi = _stamp(since), _stamp(until)
    order = {s: i for i, s in enumerate(SERVICES)}
    rows = []
    for d, n, _p in _iter(files(since, until, modified_since), needles):
        ts = d.get("ts")
        if not isinstance(ts, str) or ts < lo or ts > hi:
            continue
        if (d.get("job_id") in jobs or d.get("parent_job_id") in jobs or d.get("correlation_id") in corrs):
            rows.append(((ts, order.get(d.get("service"), 99), d.get("pid") if isinstance(d.get("pid"), int) else 0, n), d))
    rows.sort(key=lambda r: r[0])
    return [d for _k, d in rows]
