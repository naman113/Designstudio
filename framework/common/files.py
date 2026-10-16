"""File helpers shared by catalogue, engines and geometry (also used by
framework/catalog/ingest.py)."""
import hashlib
import os
import time


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def replace_retry(tmp, dst, window_s=5.0):
    """os.replace(tmp, dst), trying again for a few seconds when Windows says access denied or sharing violation (the file was
    just written and a sync client, a virus scan or a reader holds it for a moment; without the retry, part drawings
    of a large backfill can be lost). Any other error, and a denial that lasts, is raised as before."""
    end = time.monotonic() + window_s
    delay = 0.02
    while True:
        try:
            os.replace(tmp, dst)
            return
        except OSError as exc:
            denied = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32)
            if not denied or time.monotonic() > end:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
