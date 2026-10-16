"""The parts list the drawings use: `bom(product_id)` and `readable(label, ref)`.

One row per distinct ref in manifest order (item 1, 2, ...): the readable name the title block prints ("Output shaft" for
OutputShaft / OUTPUT_SHAFT; a phrase label such as "Spring, upper 1" is kept), the catalogue's label, the quantity
(the sum of the manifest's `qty` for that ref), the material ("-" while the catalogue says "Default") and whether the part
is bought in (bearings, seals, bolts, screws, nuts, studs and washers are listed on the parts list and
not drawn). Bought-in parts are told by the word list in `bought_in.json` (rules as data, written by json.dump): a whole
word of it in the readable name or the readable ref. `families.json`' label rules use \\b and miss CamelCase labels such as
OutputSeal, so they cannot serve here. A single-part design is never bought in: its one part is the design.
"""
import json
import os
import re

from framework.common import derived

HERE = os.path.dirname(os.path.abspath(__file__))
BOUGHT_IN_JSON = os.path.join(HERE, "bought_in.json")
GENERIC = {"part__feature", "part", "solid", "compound", "compsolid", "body", "shape", "feature", "default", "unnamed"}
MATERIAL_NONE = {"", "default"}

_CAMEL_1 = re.compile(r"(?<=[a-z])(?=[A-Z])")             # lower to upper
_CAMEL_2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")        # HTMLParser -> HTML Parser
_NON = re.compile(r"[^A-Za-z0-9]+")
_LONG_SOURCE = re.compile(r"^[^\s,]{20,}$")               # a long unbroken source label ("PUMP-HOUSING-2024-REV-B-1")


def _generic(label):
    return label is None or str(label).strip().lower() in GENERIC


_WORD_NUM = re.compile(r"^([A-Za-z]{2,})([0-9]+)$")        # Wheel1, Piston1: a word then its number (not B6204ZZ)


def _split_words(text):
    """OutputShaft / OUTPUT_SHAFT / Wheel1 / BrgOutFront -> ['output', 'shaft'] / ['wheel', '1'] / ['brg', 'out', 'front'].
    A model number such as B6204ZZ stays whole."""
    out = []
    for tok in _NON.split(str(text)):
        if not tok:
            continue
        m = _WORD_NUM.match(tok)
        parts = [m.group(1), m.group(2)] if m else [tok]
        for part in parts:
            out += [w for w in _CAMEL_2.sub(" ", _CAMEL_1.sub(" ", part)).split() if w]
    return out


def _sentence(words):
    s = " ".join(w.lower() for w in words)
    return s[:1].upper() + s[1:]


def readable(label, ref):
    """The name a person reads for a part. A label with a space or a comma that is not a generic name is kept as it is, its
    first letter capitalised. Otherwise the label (the ref when the label is generic, empty or a long unbroken source name) is
    split at underscores, hyphens, lower-to-upper and letter-to-digit boundaries, lower-cased, first word capitalised."""
    lab = None if label is None else str(label).strip()
    if lab and not _generic(lab) and (" " in lab or "," in lab):
        return lab[:1].upper() + lab[1:]
    base = lab if lab and not _generic(lab) and not _LONG_SOURCE.match(lab) else str(ref or "").strip()
    words = _split_words(base)
    if not words:
        words = _split_words(str(ref or "")) or ["part"]
    return _sentence(words)


_WORDS = None


def bought_in_words():
    """The word list (lower case, a phrase has single spaces), read once."""
    global _WORDS
    if _WORDS is None:
        with open(BOUGHT_IN_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        _WORDS = [" ".join(w.lower().split()) for w in data["words"]]
    return _WORDS


def is_bought_in(name, ref, n_parts):
    """False for a design of one part; otherwise True when a whole word (or phrase) of the list stands in the readable
    name or the readable ref. `name` is the part's label or readable name; both are read through `readable`."""
    if n_parts <= 1:
        return False
    hay = [" " + " ".join(readable(name, ref).lower().replace(",", " ").split()) + " ",
           " " + " ".join(readable(None, ref).lower().split()) + " "]
    for w in bought_in_words():
        needle = " " + w + " "
        if any(needle in h for h in hay):
            return True
    return False


def _manifest(product_id):
    if not isinstance(product_id, str) or not derived.PID_RX.match(product_id):
        return None
    path = os.path.join(derived.PRODUCTS, product_id + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _catalogue_rows(product_id):
    """{ref: (label, material, size_mm)} from the catalogue, read-only (as svc_queries._product_sqlite reads it). size_mm =
    the part's measured box, largest first (ingest's bbox_sorted_mm), rounded to 0.001, or None when any side is missing."""
    from framework.catalog import ingest
    if not os.path.exists(ingest.DB):                       # a fresh install before the first upload
        return {}
    con = ingest.connect(readonly=True)
    try:
        out = {}
        for r in con.execute(
                """SELECT pp.ref_des, pa.label, pa.material, pa.bbox_a_mm, pa.bbox_b_mm, pa.bbox_c_mm FROM product_parts pp
                   JOIN parts pa ON pa.part_id=pp.part_id WHERE pp.product_id=?""", (product_id,)):
            box = r[3:6]
            out[r[0]] = (r[1], r[2], None if any(v is None for v in box) else [round(float(v), 3) for v in box])
        return out
    finally:
        con.close()


def shape_key(volume, area, moments, n_faces, material):
    """One part's identical-part key (see _shape_keys), or None when it is unmeasured: volume, area and the principal
    moments to 3 significant figures, the moments sorted (they come in axis order, so a rotated copy can list them
    differently), the face count and the material in lower case."""
    if volume is None or area is None:
        return None
    sig = lambda v: float("%.3g" % float(v)) if v else 0.0
    return (sig(volume), sig(area), *sorted(sig(x) for x in moments), n_faces, str(material or "").strip().lower())


def _shape_keys(product_id):
    """{ref: key} -- what makes two parts the same part on a parts list, all independent of how the part is placed: the
    catalogue's measured volume, area and principal moments of inertia (to 0.1 %), face count and material (the world box
    is not used: a bolt at an angle reads a larger box, 64.6 x 35.6 vs 62.5 x 34.6, same 25,400 mm3 and 17 faces). A parts list groups identical parts, so a large assembly does not list every bolt
    on its own row (which could exceed the drawings' 2,000-row limit)."""
    from framework.catalog import ingest
    if not os.path.exists(ingest.DB):                       # a fresh install before the first upload
        return {}
    con = ingest.connect(readonly=True)
    try:
        out = {}
        for ref, vol, area, m1, m2, m3, nf, mat in con.execute(
                """SELECT pp.ref_des, pa.volume_mm3, pa.area_mm2, pa.moment_1, pa.moment_2, pa.moment_3, pa.n_faces,
                          pa.material FROM product_parts pp JOIN parts pa ON pa.part_id=pp.part_id WHERE pp.product_id=?""",
                (product_id,)):
            key = shape_key(vol, area, (m1, m2, m3), nf, mat)
            if key is not None:                             # unmeasured: stays its own row
                out[ref] = key
        return out
    finally:
        con.close()


def _common_name(names):
    """The words every member's name shares, in the first name's order ("Frame 1 column l", "Frame 2 column l" ->
    "Frame column l"). When that leaves one word ("Assembly bay washer", "Assembly wash" -> "Assembly"), the members' most
    frequent real word (letters only, 3 or more, never an instance tag such as w1 or 2) is added: "Assembly washer"."""
    from collections import Counter
    first = names[0].split()
    rest = [set(n.lower().split()) for n in names[1:]]
    keep = [w for w in first if all(w.lower() in r for r in rest)]
    if not keep:
        return names[0]
    if len(keep) == 1:
        have = {w.lower() for w in keep}
        words = Counter(w.lower() for n in names for w in n.split() if w.isalpha() and len(w) >= 3 and w.lower() not in have)
        if words:
            keep.append(words.most_common(1)[0][0])
    return " ".join(keep)


def _grouped(rows, keys):
    """Identical parts (same `_shape_keys` key) become one row: the first member's ref (the sheet drawn for the group), the
    members' common name, the summed quantity, bought in when any member's name says so ("Assembly wash" and "Assembly
    washer" are one washer); items renumbered 1, 2, ..."""
    groups, order = {}, []
    for r in rows:
        k = keys.get(r["ref"])
        gk = k if k is not None else ("ref", r["ref"])
        if gk not in groups:
            groups[gk] = []
            order.append(gk)
        groups[gk].append(r)
    out = []
    for i, gk in enumerate(order, start=1):
        members = groups[gk]
        row = dict(members[0])
        row["item"] = i
        row["qty"] = sum(m["qty"] for m in members)
        row["bought_in"] = any(m["bought_in"] for m in members)
        if len(members) > 1:
            row["name"] = _common_name([m["name"] for m in members])[:200]
            row["refs"] = len(members)
        out.append(row)
    return out


def bom(product_id, sizes=False):
    """{"product_id", "name", "revision", "rows": [{"item", "ref", "name", "label", "qty", "material", "bought_in"}]},
    or None when there is no such design. With `sizes` (a part's measured size) every row also
    carries "size_mm" = [a, b, c] (the catalogue's measured box, largest first) or None; without it the answer is
    unchanged."""
    m = _manifest(product_id)
    if m is None:
        return None
    order, qty = [], {}
    for p in m.get("parts", []):
        ref = p.get("ref")
        if not ref:
            continue
        if ref not in qty:
            order.append(ref)
            qty[ref] = 0
        qty[ref] += int(p.get("qty") or 1)
    cat = _catalogue_rows(product_id)
    n = len(order)
    rows = []
    for i, ref in enumerate(order, start=1):
        label, material, size = cat.get(ref, (None, None, None))
        name = readable(label, ref)
        mat = "-" if str(material or "").strip().lower() in MATERIAL_NONE else str(material).strip()
        row = {"item": i, "ref": ref, "name": name, "label": label, "qty": qty[ref], "material": mat,
               "bought_in": is_bought_in(label if label else None, ref, n)}
        if sizes:
            row["size_mm"] = size
        rows.append(row)
    if not sizes:
        # the drawings' parts list groups identical parts; the video maker (sizes) keeps one row per part it moves
        rows = _grouped(rows, _shape_keys(product_id))
    return {"product_id": m.get("product_id", product_id), "name": m.get("name", product_id),
            "revision": m.get("revision", "A"), "rows": rows}
