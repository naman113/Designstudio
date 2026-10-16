"""The sample designs buildcore loads on first start.

Every sample is generated from fixed build123d shapes by framework/samples/make.py (never customer data); the STEP files
themselves are not kept in the repo (*.step is gitignored) and are written again by `python -m framework.samples.make OUTDIR`.
framework/samples/load.py adds the missing ones through the studio's own upload routes.

SAMPLES: one dict per file: `file`, `name` (the product name), `kind` ("part": Add part to catalogue, one solid;
"assembly": Open assembly) and `material` (a materials.json name, or None: the file's own material, else steel).
"""

SAMPLES = (
    {"file": "flange.step", "name": "flange", "kind": "part", "material": None},
    {"file": "bushing.step", "name": "bushing", "kind": "part", "material": "brass"},
    {"file": "mounting_plate.step", "name": "mounting_plate", "kind": "part", "material": None},  # aluminium, from the STEP
    {"file": "shaft.step", "name": "shaft", "kind": "part", "material": "steel"},
    {"file": "spacer.step", "name": "spacer", "kind": "part", "material": "stainless steel"},
    {"file": "bracket_assembly.step", "name": "bracket_assembly", "kind": "assembly", "material": None},
)

PART_COUNT = 5
