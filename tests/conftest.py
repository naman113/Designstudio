"""Shared fixtures: the generated sample STEP files and the running stack's gateway.

Kernel tests (tests/test_kernel.py) need only the image's Python (build123d / OCP). Stack tests (tests/test_stack.py,
marker `stack`) talk to a running buildcore through its web routes, as the CLI does; with no gateway at BUILDCORE_URL they
are skipped, unless BC_REQUIRE_STACK=1 (CI), which makes a missing stack a failure.
"""
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from framework import cli  # noqa: E402


@pytest.fixture(scope="session")
def samples():
    """The six sample STEP files (framework/samples/make.py), written once to a temp folder."""
    from framework.samples import make
    d = tempfile.mkdtemp(prefix="buildcore-samples-")
    try:
        assert make.main([d]) == 0
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


PROBE_PREFIX = "pytest_probe_"


def sweep_probes(base):
    """Erase for good every design and trash entry left by an earlier, interrupted run (its name starts with
    PROBE_PREFIX). -> the ids that could not be erased."""
    ids = {p["product_id"] for p in cli.products(base) if str(p.get("name") or "").startswith(PROBE_PREFIX)}
    ids |= {t["id"] for t in cli.trash_items(base)
            if t.get("kind") == "mechanical" and str(t.get("name") or "").startswith(PROBE_PREFIX)}
    return sorted(pid for pid in ids if not cli.remove(base, pid))


@pytest.fixture(scope="session")
def base():
    """The gateway's base URL, once it answers /api/products. Probe designs left by an earlier run are swept before the
    first stack test (so they never enter a test's before/after comparison) and after the last one."""
    url = cli.base_url()
    try:
        cli.products(url)
    except cli.Unreachable:
        if os.environ.get("BC_REQUIRE_STACK") == "1":
            pytest.fail("no buildcore stack at %s (BC_REQUIRE_STACK=1)" % url)
        pytest.skip("no buildcore stack at %s" % url)
    left = sweep_probes(url)
    assert not left, "probe designs from an earlier run could not be erased: %s" % left
    yield url
    left = sweep_probes(url)
    assert not left, "probe designs could not be erased: %s" % left


def probe_name():
    """A design name no sample can have."""
    return PROBE_PREFIX + uuid.uuid4().hex[:8]


def erase(base, pids):
    """Erase probe designs for good (trash, then erase). -> the ids that could not be erased. Call it in a `finally`
    block and assert on its answer after the block, so a failing erase never hides the test's own failure."""
    return [pid for pid in pids if pid and not cli.remove(base, pid)]
