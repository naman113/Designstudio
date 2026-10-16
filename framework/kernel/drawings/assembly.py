"""The assembly drawing: front, top and right views in FIRST ANGLE, a three-quarter view, balloons on every part
that can be seen, the exact envelope, and a parts list (item, part, quantity, material, size) above the title block.

Built on these rules, each measured on two test assemblies (a gearbox and a larger 54-part assembly):

* Hidden lines are not drawn and the hidden-line pass is the TRIANGULATION pass for every assembly, at a paper deflection of at
  most 0.1 mm (`deflection_mm = min(1.0, max(0.05, 0.1 / scale))`). An exact pass costs 1.7 s on the gearbox's front view but
  24.7 s on the larger assembly's and 17.4 s on 40 of its parts with 1,400 faces, so neither the face count nor the free-form count
  predicts it; the triangulation pass costs 2.6-36 s for the whole sheet.
* The envelope is placed between KERNEL points (tight extents, finding 2): the polylines of the triangulation pass print 396.42
  for 396.44, so a size is never read off a projected edge, never `View.overall()`.
* The balloon anchors come from the part-id buffers of the views sheet's front, right and top views (`views.compose`), so the
  assembly makes no second rasterisation. A part shows a balloon in the view where most of it is visible, at its most interior
  visible pixel (a distance transform); fewer than ANCHOR_MIN_PX pixels there and it is listed "(inside)".
* First angle: the view from above BELOW the front view, the view from the right to its LEFT, the three-quarter view
  ("NOT TO SCALE") in the free corner under the right view. The parts list keeps its column on the right above the title block,
  item 1 at the bottom; above ROWS_PER_SHEET rows it goes on to a second sheet (same frame and title block, parts list only).
* Every sheet text is written here or by the engine; `check.check_assembly` reads each number back from the finished document.

`draw_assembly(compound, ext_by_ref, overall, bom, design, id_views) -> (docs, sheets, expected, report)`.
"""
import math
import time

import numpy as np

from framework.kernel.drawings import check as dcheck
from framework.kernel.drawings import sheet as S
from framework.kernel.drawings import words as vocab

ANCHOR_MIN_PX = 40            # a part with fewer visible pixels in every view gets no balloon (measured)
ROWS_PER_SHEET = 30           # parts-list rows on one sheet; more continue on the next
ROW_H = 6.0
COLS = (11.0, 76.0, 11.0, 32.0, 56.0)             # ITEM, PART, QTY, MATERIAL, SIZE (mm): 186 mm, as wide as the title block
BOM_W = sum(COLS)
HEADER = ("ITEM", "PART", "QTY", "MATERIAL", "SIZE L x W x H (mm)")
BALLOON_R = 3.6
BALLOON_TEXT = 3.0
# sheet and scale tries, first that fits; 1:20 and 1:50 only so a very large design is refused by the fit, not by luck
TRIES = (("A2", 1 / 2), ("A2", 1 / 2.5), ("A2", 1 / 5), ("A1", 1 / 2), ("A1", 1 / 2.5), ("A1", 1 / 5), ("A1", 1 / 10),
         ("A1", 1 / 20), ("A1", 1 / 50),
         # a 45 m building frame failed "does not fit on the largest sheet" at the scales above; a general arrangement of a
         # building frame is drawn at 1:100 / 1:200 (ISO 5455). Tried last, so every assembly that fitted before is unchanged.
         ("A0", 1 / 50), ("A1", 1 / 100), ("A0", 1 / 100), ("A1", 1 / 200), ("A0", 1 / 200), ("A0", 1 / 500))
WHY_FIT = "it does not fit on the largest sheet"
LAYER_BAL, LAYER_BOM = "NOTES_BALLOON", "TITLE_BOM"      # whole tokens "notes" / "title": the engine's layer intent is "reference"
GAP_H, GAP_V = 60.0, 44.0                          # between the views: dimension rows, labels and balloons stand there
DIM_REACH = 18.0                                   # a dimension row (12 mm) and its text
NOTES = ["ALL SIZES MEASURED FROM THE MODEL.", "NO TOLERANCES ARE HELD IN THE MODEL."]

VIEWS3 = ("front", "right", "top")


class DrawFail(Exception):
    def __init__(self, why, detail=""):
        super().__init__(why)
        self.why = why
        self.detail = detail


# ---- anchors ---------------------------------------------------------------------------------------------------------
def anchors(id_views, refs=None):
    """{ref: (view, (u, v), visible_px, depth_mm)}: per ref the view where it shows most and its most interior visible pixel,
    as view-plane millimetres (u along the view's right axis, v along its up axis). Parts with fewer than ANCHOR_MIN_PX
    visible pixels in every view are absent."""
    from scipy import ndimage
    best = {}
    for vname in VIEWS3:
        iv = id_views.get(vname)
        if iv is None:
            continue
        ids, names, lo, hi, s, off = iv["ids"], iv["refs"], iv["lo"], iv["hi"], iv["s"], iv["off"]
        index = {}
        for k, ref in enumerate(names):
            index.setdefault(ref, []).append(k)
        for ref, ks in index.items():
            if refs is not None and ref not in refs:
                continue
            m = np.isin(ids, ks) if len(ks) > 1 else (ids == ks[0])
            n = int(m.sum())
            if n < ANCHOR_MIN_PX:
                continue
            dist = ndimage.distance_transform_edt(m)
            iy, ix = np.unravel_index(int(np.argmax(dist)), dist.shape)
            u = lo[0] + (ix + off) / s
            v = hi[1] - (iy + off) / s
            if ref not in best or n > best[ref][2]:
                best[ref] = (vname, (float(u), float(v)), n, float(dist.max()) / s)
    return best


# ---- layout ----------------------------------------------------------------------------------------------------------
def _deflection(scale):
    return min(1.0, max(0.05, 0.1 / scale))


def _spans(overall):
    return (overall[1] - overall[0], overall[3] - overall[2], overall[5] - overall[4])


def _iso_extent(overall):
    """Upper bound of the three-quarter view's projected (width, height) at scale 1, from the corners of the envelope."""
    from framework.kernel.drawings import raster
    R, U, _D = dict((n, ax) for n, _w, ax in raster.VIEWS)["iso"]
    corners = np.array([[x, y, z] for x in overall[:2] for y in overall[2:4] for z in overall[4:]])
    cu, cv = corners @ R, corners @ U
    return float(cu.max() - cu.min()), float(cv.max() - cv.min())


def plan_views(size, scale, overall, n_rows_first):
    """Where the four views stand on a sheet of `size` at `scale` (view centres in sheet mm), from the envelope alone, or None
    when they do not fit. First angle: front upper right, the view from the right to its left, the view from above under the
    front, the three-quarter view in the free corner under the right view. Returns {"front": (x, y), ..., "iso": (x, y, scale),
    "bom_x0", "bom_top"}."""
    sw, sh = S.SHEET_SIZES[size]
    X, Y, Z = (v * scale for v in _spans(overall))
    m, ml = S._MARGIN, S._MARGIN_LEFT
    bom_x0 = sw - m - BOM_W
    x_lo, x_hi = ml + 8.0, bom_x0 - 12.0
    block_w = Y + GAP_H + X
    if block_w > x_hi - x_lo:
        return None
    x_r0 = x_lo + (x_hi - x_lo - block_w) / 2
    x_f0 = x_r0 + Y + GAP_H
    y_lo = m + 14.0                                  # the label under the bottom view needs about 10 mm
    y_hi = sh - m - 6.0 - DIM_REACH                  # the front view's length dimension stands above it
    block_h = Y + GAP_V + Z
    if block_h > y_hi - y_lo:
        return None
    y_t0 = y_lo + (y_hi - y_lo - block_h) / 2
    y_f0 = y_t0 + Y + GAP_V
    out = {"front": (x_f0 + X / 2, y_f0 + Z / 2), "right": (x_r0 + Y / 2, y_f0 + Z / 2), "top": (x_f0 + X / 2, y_t0 + Y / 2),
           "bom_x0": bom_x0, "bom_top": m + S._TITLE_H + (n_rows_first + 1) * ROW_H}
    # the free corner under the right view and left of the top view; the notes (two lines, about 130 mm wide) stand above the
    # title block on the left, so the corner starts above them
    note_top = m + S._TITLE_H + 4 + len(NOTES) * 5.0 + 4.0
    cx0, cx1 = ml + 6.0, x_f0 - 8.0
    cy0 = y_lo if cx0 > ml + 6 + 130.0 else max(y_lo, note_top)
    cy1 = y_f0 - 24.0
    iw, ih = _iso_extent(overall)
    aw, ah = cx1 - cx0, cy1 - cy0 - 12.0              # 12 mm for its label
    if aw < 40.0 or ah < 40.0 or iw <= 0 or ih <= 0:
        return None
    k = min(scale, 0.95 * aw / iw, 0.95 * ah / ih)
    if k * iw < 40.0 and k * ih < 40.0:
        return None
    out["iso"] = ((cx0 + cx1) / 2, cy0 + 12.0 + (cy1 - cy0 - 12.0) / 2, k)
    return out


def _sheet(size, scale, design, notes):
    return S.Sheet(size, scale=scale, title=design["name"], part_number=design["product_id"], revision=design["revision"],
                   author="MADE FROM THE MODEL", notes=notes, projection="FIRST ANGLE", hlr="triangulation",
                   iso_hlr="triangulation", deflection_mm=_deflection(scale), doc_type="ASSEMBLY DRAWING")


def _place(sheet, b, plan, ext, fault):
    """Put the four views and the three dimensions on the sheet. -> views {name: View}."""
    from_x = ext
    v = {}
    v["front"] = sheet.view(b, "front", at=plan["front"], hidden=False, centre_marks=False)
    v["right"] = sheet.view(b, "right", at=plan["right"], hidden=False, centre_marks=False)
    v["top"] = sheet.view(b, "top", at=plan["top"], hidden=False, centre_marks=False)
    ix, iy, ik = plan["iso"]
    v["iso"] = sheet.view(b, "iso", at=(ix, iy), hidden=False, centre_marks=False, scale=ik, label="NOT TO SCALE")
    x0, x1, y0, y1, z0, z1 = from_x
    # the envelope: between kernel points, never read off a projected edge (finding 2). The engine measures "<>" between the
    # projected points; the proof of the read-back check (`fault == "number"`) prints the length 0.02 mm too large.
    ltxt = None if fault != "number" else S._fmt((x1 - x0) + 0.02)
    v["front"].dim((x0, y0, z1), (x1, y0, z1), orientation="h", offset=12.0, text=ltxt)
    v["front"].dim((x0, y0, z0), (x0, y0, z1), orientation="v", offset=-12.0)
    v["right"].dim((x1, y0, z1), (x1, y1, z1), orientation="h", offset=12.0)
    return v


# ---- the parts list ---------------------------------------------------------------------------------------------------
def rows_for(bom, ext_by_ref, anchored):
    """[{"item", "ref", "name", "marks": [..], "qty", "material", "size", "size_vals"}] in item order."""
    rows = []
    for r in bom["rows"]:
        e = ext_by_ref.get(r["ref"])
        vals = None if e is None else [e[1] - e[0], e[3] - e[2], e[5] - e[4]]
        marks = []
        if r["bought_in"]:
            marks.append("(bought in)")
        if r["ref"] not in anchored:
            marks.append("(inside)")
        rows.append({"item": r["item"], "ref": r["ref"], "name": r["name"], "marks": marks, "qty": r["qty"],
                     "material": r["material"], "size_vals": vals,
                     "size": "-" if vals is None else " x ".join(S._fmt(v) for v in vals)})
    return rows


def _fit_text(value, height, width, min_h=1.8):
    """(text, height) that fits `width` mm: shrunk to min_h, then cut with an ellipsis."""
    w = S._text_width(value, height)
    if w <= width:
        return value, height
    h = max(min_h, height * width / w * 0.99)
    if S._text_width(value, h) <= width:
        return value, h
    while len(value) > 1 and S._text_width(value[:-1] + "…", h) > width:
        value = value[:-1]
    return value[:-1] + "…", h


def add_parts_list(doc, sheet, rows):
    """ITEM | PART | QTY | MATERIAL | SIZE, item 1 at the bottom just above the title block (ISO 7573), header on top. Returns
    {"items": [...], "texts": [...]} of what was written."""
    from ezdxf.enums import TextEntityAlignment
    msp = doc.modelspace()
    x0 = sheet.width - S._MARGIN - BOM_W
    y = S._MARGIN + S._TITLE_H
    written = []
    # draw bottom-up: the rows (item 1 first), then the header on top
    for r in list(rows) + [None]:
        cells = HEADER if r is None else (str(r["item"]), " ".join([r["name"]] + r["marks"]), str(r["qty"]), r["material"],
                                          r["size"])
        msp.add_lwpolyline([(x0, y), (x0 + BOM_W, y), (x0 + BOM_W, y + ROW_H), (x0, y + ROW_H)], close=True,
                           dxfattribs={"layer": LAYER_BOM})
        x = x0
        for ci, (wdt, val) in enumerate(zip(COLS, cells)):
            msp.add_line((x, y), (x, y + ROW_H), dxfattribs={"layer": LAYER_BOM})
            if ci == 1 and r is not None and r["marks"]:
                # the marks ("(bought in)", "(inside)") are never the part that is cut
                tail = " " + " ".join(r["marks"])
                head, h = _fit_text(r["name"] + tail, 2.5, wdt - 3.0)
                if not head.endswith(tail):
                    room = wdt - 3.0 - S._text_width(tail, 1.8)
                    h = 1.8
                    nm = r["name"]
                    while len(nm) > 1 and S._text_width(nm + "…", h) > room:
                        nm = nm[:-1]
                    head = nm + "…" + tail
                txt, height = head, h
            else:
                txt, height = _fit_text(str(val), 2.5, wdt - 3.0)
            t = msp.add_text(txt, dxfattribs={"height": height, "layer": LAYER_BOM})
            t.set_placement((x + 1.5, y + ROW_H / 2), align=TextEntityAlignment.MIDDLE_LEFT)
            written.append({"row": None if r is None else r["item"], "col": ci, "text": txt})
            x += wdt
        y += ROW_H
    return written


# ---- balloons ---------------------------------------------------------------------------------------------------------
def _sheet_map(view, sheet):
    """model point -> sheet point, exactly as the engine maps this view (its cached projection and bounds)."""
    proj = S._projection(view.shape, view.name, sheet.hlr_for(view.name), sheet.deflection_mm)
    b = S._bounds_of(proj, view.hidden)
    cx, cy = (b[0] + b[1]) / 2, (b[2] + b[3]) / 2
    sv = view.scale or sheet.scale
    box = (view.at[0] - (b[1] - b[0]) * sv / 2, view.at[0] + (b[1] - b[0]) * sv / 2,
           view.at[1] - (b[3] - b[2]) * sv / 2, view.at[1] + (b[3] - b[2]) * sv / 2)

    def f(p3):
        q = proj.point(p3)
        return (view.at[0] + (q[0] - cx) * sv, view.at[1] + (q[1] - cy) * sv)
    return f, box


def _seg_dist(p, a, b):
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _keepouts(sheet, n_rows, boxes):
    """Rectangles (x0, x1, y0, y1) where no balloon may stand: the views and what hangs on them (dimension rows, labels), the
    parts list, the title block, the notes."""
    m, ml, W = S._MARGIN, S._MARGIN_LEFT, sheet.width
    out = []
    for name, (x0, x1, y0, y1) in boxes.items():
        lab = 12.0
        left = DIM_REACH if name == "front" else 0.0
        top = DIM_REACH if name in ("front", "right") else 0.0
        out.append((x0 - left, x1, y0 - lab, y1 + top))
        cx = (x0 + x1) / 2
        out.append((cx - 22.0, cx + 22.0, y0 - lab, y0))
    out.append((W - m - BOM_W - 2.0, W - m, m, m + S._TITLE_H + (n_rows + 1) * ROW_H + 2.0))
    out.append((W - m - S._TITLE_W - 2.0, W - m, m, m + S._TITLE_H + 2.0))
    out.append((ml, ml + 140.0, m + S._TITLE_H, m + S._TITLE_H + 4.0 + len(NOTES) * 5.0 + 3.0))
    return out


def place_balloons(sheet, views, anch, item_of, boxes, n_rows):
    """-> [{"ref", "item", "view", "anchor": (x, y), "at": (x, y)}] for every anchored part, or raises DrawFail when one cannot
    be placed. Greedy: per view the parts in the order of their angle around the view's centre; each balloon on the ring around
    the view nearest its anchor's direction that is free (inside the frame, clear of every keep-out rectangle, clear of the
    balloons and leaders already there)."""
    r = BALLOON_R
    keep = _keepouts(sheet, n_rows, boxes)
    m, ml = S._MARGIN, S._MARGIN_LEFT
    frame = (ml + r + 2.0, sheet.width - m - r - 2.0, m + r + 2.0, sheet.height - m - r - 2.0)
    placed, leaders = [], []

    def free(p, a):
        if not (frame[0] <= p[0] <= frame[1] and frame[2] <= p[1] <= frame[3]):
            return False
        for x0, x1, y0, y1 in keep:
            if x0 - r - 1.0 <= p[0] <= x1 + r + 1.0 and y0 - r - 1.0 <= p[1] <= y1 + r + 1.0:
                return False
        for q in placed:
            if math.hypot(p[0] - q["at"][0], p[1] - q["at"][1]) < 2 * r + 1.5:
                return False
            if _seg_dist(q["at"], a, p) < r + 0.8:             # this leader through an earlier balloon
                return False
        for (la, lb) in leaders:
            if _seg_dist(p, la, lb) < r + 0.8:                  # an earlier leader through this balloon
                return False
        return True

    for vname in VIEWS3:
        mine = [(ref, a) for ref, a in anch.items() if a[0] == vname]
        if not mine:
            continue
        to_sheet, box = _sheet_map(views[vname], sheet)
        R, U = _axes(vname)
        cxs, cys = (box[0] + box[1]) / 2, (box[2] + box[3]) / 2
        pts = []
        for ref, (_v, (u, v), _n, _d) in mine:
            p3 = tuple(u * R + v * U)
            pts.append((ref, to_sheet(p3)))
        pts.sort(key=lambda t: math.atan2(t[1][1] - cys, t[1][0] - cxs))
        for ref, (ax, ay) in pts:
            ang = math.atan2(ay - cys, ax - cxs)
            done = None
            for d in (8.0, 14.0, 20.0, 28.0, 38.0, 50.0, 64.0):
                hw, hh = (box[1] - box[0]) / 2 + d, (box[3] - box[2]) / 2 + d
                for step in range(0, 90):
                    a = ang if step == 0 else ang + ((step + 1) // 2) * 0.04 * (1 if step % 2 else -1)
                    dx, dy = math.cos(a), math.sin(a)
                    t = min(hw / abs(dx) if dx else 1e9, hh / abs(dy) if dy else 1e9)
                    p = (cxs + dx * t, cys + dy * t)
                    if free(p, (ax, ay)):
                        done = p
                        break
                if done:
                    break
            if done is None:
                raise DrawFail(WHY_FIT, "no free place for the balloon of %s in the %s view" % (ref, vname))
            placed.append({"ref": ref, "item": item_of[ref], "view": vname, "anchor": (ax, ay), "at": done})
            leaders.append(((ax, ay), done))
    return placed


def _axes(vname):
    from framework.kernel.drawings import raster
    R, U, _D = dict((n, ax) for n, _w, ax in raster.VIEWS)[vname]
    return R, U


def add_balloons(doc, placed):
    from ezdxf.enums import TextEntityAlignment
    msp = doc.modelspace()
    r = BALLOON_R
    for b in placed:
        (ax, ay), (bx, by) = b["anchor"], b["at"]
        d = math.hypot(bx - ax, by - ay) or 1.0
        ex, ey = bx - (bx - ax) / d * r, by - (by - ay) / d * r
        msp.add_line((ax, ay), (ex, ey), dxfattribs={"layer": LAYER_BAL})
        msp.add_circle((ax, ay), 0.5, dxfattribs={"layer": LAYER_BAL})
        msp.add_circle((bx, by), r, dxfattribs={"layer": LAYER_BAL})
        t = msp.add_text(str(b["item"]), dxfattribs={"height": BALLOON_TEXT, "layer": LAYER_BAL})
        t.set_placement((bx, by), align=TextEntityAlignment.MIDDLE_CENTER)


def _layers(doc):
    for name in (LAYER_BAL, LAYER_BOM):
        if name not in doc.layers:
            doc.layers.add(name, color=7, linetype="CONTINUOUS", lineweight=25)


# ---- expected numbers (what the read-back must find) ------------------------------------------------------------------
def expected_numbers(overall, rows, balloon_items):
    """{"dimension": [...], "bom": [...], "balloon": [...]} of (text, kernel value or None, what), computed here from the kernel's
    tight extents and the catalogue's parts list, not from what the drawing code printed."""
    x, y, z = _spans(overall)
    exp = {"dimension": [(S._fmt(x), x, "overall length"), (S._fmt(z), z, "overall height"),
                         (S._fmt(y), y, "overall width")], "bom": [], "balloon": []}
    for r in rows:
        exp["bom"].append((str(r["item"]), r["item"], "item"))
        exp["bom"].append((str(r["qty"]), r["qty"], "qty"))
        if r["size_vals"] is not None:
            for v, w in zip(r["size_vals"], ("length", "width", "height")):
                exp["bom"].append((S._fmt(v), v, "%s of %s" % (w, r["ref"])))
        for cell, what in ((r["name"] + " " + " ".join(r["marks"]), "name"), (r["material"], "material")):
            for n in dcheck.NUM.findall(cell):
                exp["bom"].append((n, None, "%s digits of %s" % (what, r["ref"])))
    for item in balloon_items:
        exp["balloon"].append((str(item), item, "balloon"))
    return exp


def furniture(sheets, design, rows):
    """Numbers allowed in the title block and the note lines, and the names the user chose."""
    nums = set()
    count = len(sheets)
    for i, sheet in enumerate(sheets, start=1):
        for text in (design["name"], design["product_id"], design["revision"], sheet.size, S._scale_label(sheet.scale),
                     "SHEET %d OF %d" % (i, count)):
            nums.update(dcheck.NUM.findall(str(text)))
        nums.update(str(k + 1) for k in range(len(sheet.notes)))
    names = [design["name"], design["product_id"]] + [r["name"] for r in rows] + [r["material"] for r in rows if r["material"] != "-"]
    return {"numbers": nums, "names": names}


# ---- the drawing ------------------------------------------------------------------------------------------------------
def _doc_boxes(sheet):
    from framework.kernel.drawings import autodim
    return dict(autodim.view_boxes(sheet))


def draw_assembly(compound, ext_by_ref, overall, bom, design, id_views, fault=None):
    """The assembly drawing of one design.

    compound     the design's shapes (one TopoDS compound of every placed occurrence, UNMESHED: the triangulation pass meshes at
                 the paper deflection)
    ext_by_ref   {ref: tight extent (xmin, xmax, ymin, ymax, zmin, zmax)} of one part as placed (printed in the parts list)
    overall      the design's tight extent
    bom          catalogue.drawing_bom: {"rows": [{"item", "ref", "name", "qty", "material", "bought_in"}]}
    design       {"product_id", "name", "revision"}
    id_views     views.compose's part-id buffers of the front, right and top views
    -> (docs, sheets, expected, report)  (`docs` and `sheets` have one entry per page; `report["_rows"]` are the parts-list rows
    as written, for `furniture()`, and is dropped by the caller before the report is stored)"""
    from build123d import Shape
    from framework.kernel.drawings import autodim
    t0 = time.perf_counter()
    refs = {r["ref"] for r in bom["rows"]}
    anch = anchors(id_views, refs)
    item_of = {r["ref"]: r["item"] for r in bom["rows"]}
    rows = rows_for(bom, ext_by_ref, anch)
    n = len(rows)
    first_rows = min(n, ROWS_PER_SHEET)
    t_anchor = time.perf_counter() - t0
    b = Shape.cast(compound)
    last, best, tried = None, None, []
    for size, scale in TRIES:
        plan = plan_views(size, scale, overall, first_rows)
        if plan is None:
            last = WHY_FIT
            tried.append("%s %s: the views do not fit" % (size, S._scale_label(scale)))
            continue
        pages = [rows[i:i + ROWS_PER_SHEET] for i in range(0, n, ROWS_PER_SHEET)] or [[]]
        sheets = [_sheet(size, scale, design, list(NOTES)) for _ in pages]
        t1 = time.perf_counter()
        try:
            views = _place(sheets[0], b, plan, overall, fault)
            docs, warns, lay_s = S.layout(sheets)
            why = autodim.fits(sheets[0], docs[0])
            if why:
                last = why
                tried.append("%s %s: %s" % (size, S._scale_label(scale), why))
                continue
            boxes = _doc_boxes(sheets[0])
            placed = place_balloons(sheets[0], views, anch, item_of, boxes, first_rows)
        except ValueError as exc:
            last = str(exc)
            tried.append("%s %s: %s" % (size, S._scale_label(scale), last[:120]))
            continue
        except DrawFail as exc:
            last = exc.detail or exc.why
            tried.append("%s %s: %s" % (size, S._scale_label(scale), last[:120]))
            continue
        hlr_s = time.perf_counter() - t1
        for d in docs:
            _layers(d)
        add_balloons(docs[0], placed)
        written = []
        for d, sh, page in zip(docs, sheets, pages):
            written.append(add_parts_list(d, sh, page))
        why = autodim.fits(sheets[0], docs[0], clearance=0.0)
        if why:
            last = why
            tried.append("%s %s: %s (with balloons and the parts list)" % (size, S._scale_label(scale), why))
            continue
        best = (size, scale, sheets, docs, warns, placed, written, hlr_s)
        break
    if best is None:
        raise DrawFail(WHY_FIT, str(last))
    size, scale, sheets, docs, warns, placed, written, hlr_s = best
    expected = expected_numbers(overall, rows, [p["item"] for p in placed])
    ov = [w for w in warns if w.startswith("annotation overlaps")]
    other = [w for w in warns if not w.startswith("annotation overlaps") and not w.startswith("check: [info]")]
    report = {"size": size, "scale": S._scale_label(scale), "pages": len(docs), "balloons": len(placed),
              "ballooned": [p["ref"] for p in placed], "balloon_items": sorted(p["item"] for p in placed),
              "inside": [r["ref"] for r in rows if r["ref"] not in anch],
              "inside_items": [r["item"] for r in rows if r["ref"] not in anch],
              "bought_in": [r["ref"] for r in rows if r["marks"] and "(bought in)" in r["marks"]],
              "rows": n, "rows_by_page": [len(w) // len(COLS) - 1 for w in written],
              "overlaps": len(ov), "overlap_texts": [w[:160] for w in ov[:3]], "warnings": other[:5],
              "views": {v.name: [round(v.at[0], 1), round(v.at[1], 1)] for v in sheets[0].views},
              "hlr_s": round(hlr_s, 3), "anchor_s": round(t_anchor, 3), "size_tags": [r["size"] for r in rows],
              "anchor_px": {ref: a[2] for ref, a in anch.items()}, "tried": tried, "_rows": rows}
    return docs, sheets, expected, report


def release_cache():
    S.clear_cache()
