"""Stack tests: a running buildcore (gateway at BUILDCORE_URL) with its samples loaded, driven through the same web routes
and helpers as the CLI (framework/cli.py).

Covered: the sample designs list, the sample assembly's drawings (PDF sheets), its parts list (JSON and CSV), its GLB,
the catalogue's find by bore, adding a part and finding it, refusing an assembly as one part, and distinct names for a
repeated upload. Every probe design has a name no sample can have and is erased for good in a `finally` block (failed
erases are reported after it, so they never hide the test's own failure); the list of designs and the trash are compared
before and after. Probes left by an interrupted run are swept by the `base` fixture (tests/conftest.py).
"""
import json
import shutil
import time
import urllib.parse

import pytest

from conftest import erase, probe_name
from framework import cli

pytestmark = pytest.mark.stack

SAMPLE_NAMES = {"flange", "bushing", "mounting_plate", "shaft", "spacer", "bracket_assembly"}
WAIT_S = 180


def _pid(base, name):
    for p in cli.products(base):
        if p.get("name") == name:
            return p["product_id"]
    raise AssertionError("no design named %r" % name)


def _get(base, path, timeout=300):
    status, body = cli.request("GET", base + path, timeout=timeout)
    assert status == 200, "%s answered %d: %s" % (path, status, body[:200])
    return body


def _state(base):
    """The designs and the trash, as comparable sets."""
    prods = {(p.get("product_id"), p.get("name")) for p in cli.products(base)}
    trash = {json.dumps(t, sort_keys=True) for t in cli.trash_items(base)}
    return prods, trash


def _find(base, filters, kind="parts", limit=100):
    return cli.post_json(base, "/api/catalogue/search", cli.find_request(filters, None, kind, limit, "name"))


def _labels(res):
    return {it.get("label") for it in (res.get("parts") or {}).get("items") or []}


def _designs(res):
    return {d.get("product_id") for it in (res.get("parts") or {}).get("items") or [] for d in it.get("designs") or []}


def _wait_for(check, what, wait_s=WAIT_S):
    end = time.time() + wait_s
    while True:
        if check():
            return
        if time.time() > end:
            raise AssertionError("%s: not within %d s" % (what, wait_s))
        time.sleep(2)


@pytest.fixture
def unchanged(base):
    """The designs and the trash are the same after the test as before it."""
    before = _state(base)
    yield
    assert _state(base) == before


def test_products_list_the_samples(base):
    names = {p.get("name") for p in cli.products(base)}
    assert SAMPLE_NAMES <= names


def test_drawings_of_the_sample_assembly(base):
    pid = _pid(base, "bracket_assembly")
    st = cli.drawings(base, pid)
    assert st.get("state") == "current"
    assert not st.get("failed_parts") and not st.get("timed_out")
    sheets = [s for s in cli.get_json(base, "/api/drawings/%s" % urllib.parse.quote(pid)) if s.get("set") == "current"]
    pdfs = [s for s in sheets if s.get("format") == "pdf"]
    assert {s.get("kind") for s in pdfs} >= {"assembly", "part"}
    assert len([s for s in pdfs if s.get("kind") == "part"]) == 2        # one sheet for the two identical side plates
    for s in sheets:
        data = _get(base, urllib.parse.urlsplit(s["url"]).path)
        if s["format"] == "pdf":
            assert data[:4] == b"%PDF", s["url"]
        elif s["format"] == "png":
            assert data[:8] == b"\x89PNG\r\n\x1a\n", s["url"]


def test_bom_of_the_sample_assembly(base):
    pid = _pid(base, "bracket_assembly")
    bom = json.loads(_get(base, "/api/bom/%s?format=json" % pid))
    qty = {r["name"]: r["qty"] for r in bom["rows"]}
    assert qty == {"Side plate": 2, "Pin": 1}
    lines = _get(base, "/api/bom/%s?format=csv" % pid).decode("utf-8").strip().splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("item,name,qty")
    assert lines[1].split(",")[1:3] == ["Side plate", "2"]
    assert lines[2].split(",")[1:3] == ["Pin", "1"]


def test_glb_of_the_sample_assembly(base):
    pid = _pid(base, "bracket_assembly")
    data = _get(base, "/api/glb/%s" % pid, timeout=900)
    assert data[:4] == b"glTF"


def test_catalogue_find_by_bore(base):
    labels = _labels(_find(base, {"bore": cli.parse_range("10:40")}))
    assert {"bushing", "flange", "spacer"} <= labels
    assert not labels & {"shaft", "mounting_plate"}


def test_catalogue_add_find_and_erase(base, samples, tmp_path, unchanged):
    name = probe_name()
    step = tmp_path / (name + ".step")
    shutil.copyfile(samples / "spacer.step", step)
    pid, left = None, []
    try:
        done = cli.upload(base, str(step), single_part=True, material="bronze", name=name)
        pid = done.get("product_id")
        assert done.get("ok"), done.get("error")
        bore = {"bore": cli.parse_range("12.9:13.1")}
        _wait_for(lambda: pid in _designs(_find(base, bore)), "the added part found by bore")
    finally:
        left = erase(base, [pid])
    assert not left, "the probe design could not be erased: %s" % left
    _wait_for(lambda: pid not in _designs(_find(base, {"bore": cli.parse_range("12.9:13.1")})),
              "the erased part gone from the catalogue")


def test_assembly_refused_as_one_part(base, samples, tmp_path, unchanged):
    name = probe_name()
    step = tmp_path / (name + ".step")
    shutil.copyfile(samples / "bracket_assembly.step", step)
    done, left = {}, []
    try:
        done = cli.upload(base, str(step), single_part=True, name=name)
        assert not done.get("ok")
        assert "upload each part on its own" in (done.get("error") or "")
    finally:
        left = erase(base, [done.get("product_id")])
    assert not left, "the probe design could not be erased: %s" % left


def test_repeated_upload_gets_a_distinct_name(base, samples, tmp_path, unchanged):
    name = probe_name()
    step = tmp_path / (name + ".step")
    shutil.copyfile(samples / "shaft.step", step)
    made, left = [], []
    try:
        for _ in range(2):
            done = cli.upload(base, str(step))
            if done.get("product_id"):
                made.append(done["product_id"])
            assert done.get("ok"), done.get("error")
        assert made[0] != made[1]
        names = {p["product_id"]: p.get("name") for p in cli.products(base)}
        first, second = names[made[0]], names[made[1]]
        assert first == name
        assert second != first and second == name + " (2)"
    finally:
        left = erase(base, made)
    assert not left, "the probe designs could not be erased: %s" % left
