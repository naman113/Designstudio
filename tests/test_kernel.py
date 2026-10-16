"""Kernel tests on the generated samples: no Postgres, no network, no running stack.

measure (volume of the flange), the parts list's identical-part key and grouping (framework/catalog/drawing_bom.py),
the design GLB (framework/kernel/partview.py) and one part drawing (framework/kernel/ops.py draw_parts, read back from
the finished PDF).
"""
import json
import math
import struct

from framework.catalog import drawing_bom
from framework.kernel import io as kio
from framework.kernel import measure as kmeasure
from framework.kernel import ops, partview


def _bom_key(rec, material=""):
    """The identical-part key drawing_bom builds from the catalogue's measured columns, fed from the kernel's record."""
    return drawing_bom.shape_key(rec["volume_mm3"], rec["area_mm2"], rec["moments"], rec["n_faces"], material)


def _glb_json(data):
    magic, version, _length = struct.unpack_from("<4sII", data, 0)
    assert magic == b"glTF" and version == 2
    clen, ctype = struct.unpack_from("<I4s", data, 12)
    assert ctype == b"JSON"
    return json.loads(data[20:20 + clen])


def test_measure_flange_volume(samples):
    model = kio.load(str(samples / "flange.step"))
    assert len(model.parts) == 1
    rec = kmeasure.measure(model.parts[0].shape, "flange")
    want = math.pi * (40 ** 2 - 15 ** 2) * 10 - 4 * math.pi * 4 ** 2 * 10      # about 41,186 mm3
    assert abs(rec["volume_mm3"] - want) / want < 0.001
    assert rec["n_solids"] == 1


def test_bom_identical_parts_key_and_grouping(samples):
    model = kio.load(str(samples / "bracket_assembly.step"))
    assert len(model.parts) == 3
    keys, rows = {}, []
    for i, p in enumerate(model.parts, start=1):
        rec = kmeasure.measure(p.shape, p.ref)
        keys[p.ref] = _bom_key(rec, p.material)
        rows.append({"item": i, "ref": p.ref, "name": drawing_bom.readable(p.label, p.ref), "label": p.label,
                     "qty": p.qty, "material": "-", "bought_in": False})
    plates = [r for r in keys if r.startswith("side_plate")]
    assert len(plates) == 2
    assert keys[plates[0]] == keys[plates[1]]                      # the second plate is rotated, the key is the same
    assert keys["pin"] != keys[plates[0]]
    grouped = drawing_bom._grouped(rows, keys)
    assert len(grouped) == 2
    qty = {r["name"].lower(): r["qty"] for r in grouped}
    assert qty == {"side plate": 2, "pin": 1}
    assert [r["item"] for r in grouped] == [1, 2]


def test_glb_design(samples, tmp_path):
    model = kio.load(str(samples / "bracket_assembly.step"))
    out = tmp_path / "bracket_assembly.glb"
    stats = partview.write_design(kio.all_shapes(model), str(out), header="bracket_assembly")
    assert stats["parts"] == 3 and not stats["skipped"] and stats["triangles"] > 0
    data = out.read_bytes()
    assert data[:4] == b"glTF"
    names = {n.get("name") for n in _glb_json(data)["nodes"]}
    assert {p.ref for p in model.parts} <= names


def test_drawing_part_without_postgres(samples, tmp_path):
    step = samples / "flange.step"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"product_id": "flange_test", "name": "flange", "revision": "A",
                                    "parts": [{"ref": "flange", "file": str(step), "object": "flange", "qty": 1}]}))
    bom = {"product_id": "flange_test", "name": "flange", "revision": "A",
           "rows": [{"item": 1, "ref": "flange", "name": "Flange", "label": "flange", "qty": 1, "material": "steel",
                     "bought_in": False}]}
    out_dir = tmp_path / "drawings"
    res = ops.draw_parts(str(manifest), "flange_test", str(out_dir), bom, None, None, str(out_dir / "result.json"))
    assert res["sheets"] == 1 and res["failed"] == 0
    report = json.loads((out_dir / "report.json").read_text())
    assert report["failed"] == []
    assert [s["ref"] for s in report["sheets"]] == ["flange"]
    pdf = (out_dir / "part-flange.pdf").read_bytes()
    assert pdf[:4] == b"%PDF"
