"""The read-back check of a views sheet: before a sheet is handed over, every number it prints is compared with a
value recomputed HERE from the kernel's tight extents, never from what `views.py` returned (a size read off a
drawn edge once printed 396.42 for 396.44).

`check_views` knows how a view's width and height come from the overall size (front: x by z, right side: y by z, top:
x by y, rear: x by z, bottom: x by y) and how the header prints the overall size and the part count, so a wrong axis, a
wrong rounding or a stale number is a "wrong". A number printed that is not expected is "unexplained"; an expected number
that is not printed is "missing". `words` runs the vocabulary guard on every text the studio wrote.
"""
from framework.kernel.drawings import words as vocab

# what a printed number is: (view caption key) -> index pair into the overall size (x, y, z)
AXES = {"front width": 0, "front height": 2, "right width": 1, "right height": 2, "top width": 0, "top height": 1,
        "rear width": 0, "rear height": 2, "bottom width": 0, "bottom height": 1,
        "overall X": 0, "overall Y": 1, "overall Z": 2}
TESS_TOL_MM = 0.05


def _fmt(v, nd=1):
    return ("%%.%df" % nd) % v


def expected(overall_size, n_parts):
    """{what: text} the sheet must print: 13 sizes and the part count."""
    want = {what: _fmt(overall_size[i]) for what, i in AXES.items()}
    want["parts"] = _fmt(n_parts, 0)
    return want


def check_views(printed, overall_size, n_parts, rows=None):
    """-> {"numbers", "wrong", "unexplained", "missing", "details"}. `printed` = views.compose's records; `rows` its
    caption rows (the strings actually drawn): each expected text must also stand inside the drawn caption."""
    want = expected(overall_size, n_parts)
    details, wrong, unexplained = [], 0, 0
    seen = set()
    for p in printed:
        what = p["what"]
        if what not in want:
            unexplained += 1
            details.append("unexplained number %r (%s)" % (p["text"], what))
            continue
        seen.add(what)
        if p["text"] != want[what]:
            wrong += 1
            details.append("%s printed %s, the kernel says %s" % (what, p["text"], want[what]))
    missing = sorted(set(want) - seen)
    for what in missing:
        details.append("missing number: %s" % what)
    if rows is not None:
        drawn = {r["view"]: r["caption"] for r in rows}
        sub0 = drawn.get("header_sub", "")
        if "%s parts" % want["parts"] not in sub0:
            wrong += 1
            details.append("the header does not say %s parts" % want["parts"])
        # the drawn header line holds the overall sizes in order, and each view caption its width then its height
        sub = drawn.get("header_sub", "")
        o = "overall %s x %s x %s mm" % (want["overall X"], want["overall Y"], want["overall Z"])
        if o not in sub:
            wrong += 1
            details.append("the header's overall size line is not %r" % o)
        for view in ("front", "right", "top", "rear", "bottom"):
            cap = drawn.get(view, "")
            w = "%s wide x %s high mm" % (want[view + " width"], want[view + " height"])
            if w not in cap:
                wrong += 1
                details.append("%s caption is not %r" % (view, w))
    return {"numbers": len(printed), "wrong": wrong, "unexplained": unexplained, "missing": len(missing),
            "details": details}


def words(texts):
    """[{"text", "kind", "word"}] for every text of the studio's own that breaks the vocabulary."""
    out = []
    for t in texts:
        for kind, word in vocab.find(t):
            out.append({"text": t, "kind": kind, "word": word})
    return out


def tess_vs_tight(part_extents, overall_extent):
    """Max |difference| in mm between the extents of the tessellation's vertices (`part_extents` = one 6-tuple per
    part, from the vertex arrays) and the tight extent of the design: tight extents and triangles must agree."""
    xs0, xs1, ys0, ys1, zs0, zs1 = zip(*part_extents)
    t = (min(xs0), max(xs1), min(ys0), max(ys1), min(zs0), max(zs1))
    return max(abs(a - b) for a, b in zip(t, overall_extent))


# ---- part drawings -------------------------------------------------------------------------------------------------------
# The same rule as the views sheet's check, for an engineering drawing: every number printed is READ BACK from the finished
# document (dimension blocks, hole callouts, title block, notes) and compared with the kernel values the rules placed.
# Measured: this caught "396.42 printed for 396.44" on an assembly drawing.
import re

NUM = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?")
_CODES = (("%%c", "\u00d8"), ("%%C", "\u00d8"), ("%%d", "\u00b0"), ("%%D", "\u00b0"), ("%%p", "\u00b1"), ("%%P", "\u00b1"))


def drawn(txt):
    """A text as the renderer draws it (the engine's %%c, %%d, %%p codes read as their glyphs)."""
    for code, glyph in _CODES:
        txt = txt.replace(code, glyph)
    return txt


def printed_numbers(doc):
    """[{"where", "layer", "text", "numbers"}] for every text of the document: the MTEXT inside each dimension block
    ("dimension"), the TEXT / MTEXT on layer DIM ("callout": hole and slot callouts), TITLE ("title") and NOTES ("notes":
    the note lines and the view labels)."""
    out = []
    msp = doc.modelspace()
    for e in msp:
        kind = e.dxftype()
        if kind == "DIMENSION":
            for be in doc.blocks.get(e.dxf.geometry):
                if be.dxftype() in ("MTEXT", "TEXT"):
                    txt = drawn(be.plain_text())
                    out.append({"where": "dimension", "layer": e.dxf.layer, "text": txt, "numbers": NUM.findall(txt)})
        elif kind in ("TEXT", "MTEXT"):
            txt = drawn(e.plain_text())
            where = {"DIM": "callout", "TITLE": "title", "NOTES": "notes", "NOTES_BALLOON": "balloon",
                     "TITLE_BOM": "bom"}.get(e.dxf.layer, "text")
            out.append({"where": where, "layer": e.dxf.layer, "text": txt, "numbers": NUM.findall(txt)})
    return out


def check_part(docs, expected, furniture):
    """The read-back check of a part sheet.

    expected   [(text the sheet must print, kernel value, what)]: the numbers the rules placed (each a multiset entry)
    furniture  {"numbers": {...}, "names": [...]}: numbers allowed in the title block and note lines (design name, ref,
               revision, sheet size, scale, "SHEET n OF m", the notes' own numbering) and the names the user chose

    A number printed in a dimension or a callout must match one expected entry (each entry once): otherwise it is
    `unexplained`. An expected entry nothing matched is `missing`. `wrong` = an expected text that is not the engine's
    format of its kernel value. `words` = vocabulary violations in the studio's own text (names are scanned apart:
    `name_words`, a warning only, as on the views sheet)."""
    from framework.kernel.drawings import sheet as S
    printed = []
    for d in docs:
        printed += printed_numbers(d)
    remaining = [e[0] for e in expected]
    allowed = set(furniture.get("numbers", ()))
    details, unexplained, count = [], 0, 0
    for p in printed:
        for n in p["numbers"]:
            count += 1
            if p["where"] in ("dimension", "callout"):
                if n in remaining:
                    remaining.remove(n)
                else:
                    unexplained += 1
                    details.append("unexplained number %s in %s %r" % (n, p["where"], p["text"][:60]))
            elif n not in allowed:
                unexplained += 1
                details.append("unexplained number %s in the %s %r" % (n, p["where"], p["text"][:60]))
    for text in remaining:
        details.append("missing number %s (placed by the rules, not printed)" % text)
    wrong = [e for e in expected if e[0] != S._fmt(e[1])]
    for e in wrong:
        details.append("%s: text %s is not the format of the kernel value %r" % (e[2], e[0], e[1]))
    names = [str(n).lower() for n in furniture.get("names", ()) if n]
    studio, name_texts = [], []
    for p in printed:
        t = p["text"]
        if p["where"] == "title" and (t.endswith("\u2026") or any(n in t.lower() for n in names)):
            name_texts.append(t)
        else:
            studio.append(t)
    return {"numbers": count, "expected": len(expected), "unexplained": unexplained, "missing": len(remaining),
            "wrong": len(wrong), "words": words(studio), "name_words": words(name_texts), "details": details,
            "texts": [p["text"] for p in printed]}


# ---- assembly drawings ------------------------------------------------------------------------------------------------------
def check_assembly(docs, expected, furniture):
    """The read-back check of an assembly drawing (all pages): as `check_part`, with three kinds of expected number kept apart
    so a wrong one cannot hide behind another's value.

    expected   {"dimension": [...], "bom": [...], "balloon": [...]}, each entry (text, kernel value or None, what):
               dimension  the three envelope sizes, read from the dimension blocks and callouts
               bom        every item, quantity and size of the parts list (and the digits of a part's name or material)
               balloon    the item number of every balloon
    furniture  as for `check_part` (numbers allowed in the title block and note lines; names the user chose)

    A printed number that matches no expected entry of its kind (each entry once) is `unexplained`; an expected entry nothing
    matched is `missing`; `wrong` = an expected text that is not the engine's format of its kernel value."""
    from framework.kernel.drawings import sheet as S
    printed = []
    for d in docs:
        printed += printed_numbers(d)
    kinds = {"dimension": "dimension", "callout": "dimension", "bom": "bom", "balloon": "balloon"}
    remaining = {k: [e[0] for e in v] for k, v in expected.items()}
    allowed = set(furniture.get("numbers", ()))
    details, unexplained, count = [], 0, 0
    for p in printed:
        for n in p["numbers"]:
            count += 1
            kind = kinds.get(p["where"])
            if kind is not None:
                if n in remaining.get(kind, []):
                    remaining[kind].remove(n)
                else:
                    unexplained += 1
                    details.append("unexplained number %s in %s %r" % (n, p["where"], p["text"][:60]))
            elif n not in allowed:
                unexplained += 1
                details.append("unexplained number %s in the %s %r" % (n, p["where"], p["text"][:60]))
    missing = 0
    for kind, left in remaining.items():
        for text in left:
            missing += 1
            details.append("missing number %s (%s, placed by the rules, not printed)" % (text, kind))
    wrong = [e for v in expected.values() for e in v if e[1] is not None and e[0] != S._fmt(e[1])]
    for e in wrong:
        details.append("%s: text %s is not the format of the kernel value %r" % (e[2], e[0], e[1]))
    names = [str(n).lower() for n in furniture.get("names", ()) if n]
    studio, name_texts = [], []
    for p in printed:
        t = p["text"]
        low = t.lower()
        if (p["where"] == "title" and (t.endswith("…") or any(n in low for n in names))) or                 (p["where"] == "bom" and any(n and n in low for n in names)):
            name_texts.append(t)
        else:
            studio.append(t)
    return {"numbers": count, "expected": sum(len(v) for v in expected.values()), "unexplained": unexplained,
            "missing": missing, "wrong": len(wrong), "words": words(studio), "name_words": words(name_texts),
            "details": details, "texts": [p["text"] for p in printed]}
