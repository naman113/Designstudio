"""Results that cross the bus without nested numbers.

pgmq messages and `orch.jobs.result` are jsonb, and jsonb turns -0.0 into 0. So a handler returns
`results.pack(job_id, data)`, which is the data as one JSON string, or, above 64 KB, a file under `work/results/`.
The caller turns it back with `results.unpack(job["result"])`.

    {"json": "<text>"}                       up to 64 KB
    {"ref": "work/results/<job_id>.json", "bytes": n}

Copy-safe: the file's path is a function of the job id and it is written as a temp file
then replaced, so a redelivered job or a second replica writes the same bytes to the same place.
"""
import json
import os
import threading
import time

from framework.common import config

INLINE_MAX = 64 * 1024
DIR = os.path.join(config.ROOT, "work", "results")
PRUNE_EVERY_S = 600
PRUNE_AFTER_S = 24 * 3600

_last_prune = 0.0
_prune_lock = threading.Lock()


def _prune():
    """Delete result files older than 24 h, at most every 10 minutes (an idempotent delete: a missing file is ignored)."""
    global _last_prune
    with _prune_lock:
        now = time.time()
        if now - _last_prune < PRUNE_EVERY_S:
            return
        _last_prune = now
    try:
        for name in os.listdir(DIR):
            path = os.path.join(DIR, name)
            try:
                if name.endswith(".json") and now - os.path.getmtime(path) > PRUNE_AFTER_S:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def pack(job_id, data):
    text = json.dumps(data)
    if len(text) <= INLINE_MAX:
        return {"json": text}
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, "%s.json" % job_id)
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)
    _prune()
    return {"ref": "work/results/%s.json" % job_id, "bytes": len(text)}


def unpack(result):
    if result is None:
        return None
    if isinstance(result, dict):
        if "json" in result and len(result) == 1:
            return json.loads(result["json"])
        if "ref" in result:
            path = os.path.normpath(os.path.join(config.ROOT, result["ref"]))
            if os.path.dirname(path) != os.path.normpath(DIR):
                raise ValueError("a result file must be inside work/results")
            with open(path, encoding="utf-8", newline="") as fh:
                return json.load(fh)
    return result
