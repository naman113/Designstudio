"""The views sheet: six shaded views of a design on one sheet, with the exact sizes.

Layout: an A-series sheet 3300 x 2333 px, a header band, a 3 x 2 grid of cells. The five
orthographic views (front, right side, top, rear, bottom) share ONE scale; the three-quarter view has its own and says so.
It is a captioned picture grid, not a projection arrangement, so it carries no projection label (first angle
applies to the part and assembly drawings).

Every number printed goes through `num()`: one format rule (`%.1f`, trailing zeros kept; the part count is an integer), and
a record `{"text", "value", "what"}` of what was printed. `check.py` recomputes the expected texts from the kernel's tight
extents without reading these records' values, so a wrong number cannot be published.

Lettering uses only the font shipped in the package (`fonts/osifont-lgpl3fe.ttf`), never a system font.
"""
import io
import math
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from framework.kernel.drawings import raster

FONT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "osifont-lgpl3fe.ttf")
_FONTS = {}


def font(size):
    size = max(8, int(size))
    if size not in _FONTS:
        _FONTS[size] = ImageFont.truetype(FONT_FILE, size)
    return _FONTS[size]


def fmt(v, nd=1):
    """How every size is printed on a sheet: fixed decimals, trailing zeros kept (one rule, so the check can redo it)."""
    return ("%%.%df" % nd) % v


def compose(name, revision, n_parts, extents_by_ref, overall, parts, edges, colors, *, px=3300, ss=2, pictures=None,
            clock=None):
    """-> (png_bytes, printed, rows, id_views).

    `overall` = the design's tight extent (xmin, xmax, ymin, ymax, zmin, zmax); `extents_by_ref` is carried for the
    caller's report; `parts` = [(ref, P, N, T)] one per occurrence, `edges` = {ref: S}, `colors` = {ref: palette index}.
    `pictures` ([{name, file, px, mm_per_px, origin_mm, axes}]) lets a caller hand in finished
    pictures; when None the views are rendered here. `printed` = [{"text", "value", "what"}]; `rows` = one dict per view
    (`view`, `caption`, `sec`, `px`, `samples`, `edge_segments`) plus the header lines (`view` "header_title",
    "header_sub": `caption` = the text drawn). `clock(name)` is called after each view (the op's own timing). `id_views` (the assembly drawing's balloons reuse them, no second rasterisation) =
    {"front"|"right"|"top": {"ids": the part-id buffer at the picture's size (index into `parts`, -1 = background), "refs": the
    ref of each index, "lo": (u, v) of the picture's left and bottom edge and "hi" its right and top edge in view-plane mm,
    "s": px per mm, "off": the sub-pixel offset of the sample the id was taken at, "axes": (R, U, D)}}.
    """
    if pictures is not None:
        raise NotImplementedError("finished pictures are handed in by a later phase")
    ox0, ox1, oy0, oy1, oz0, oz1 = overall
    size = np.array([ox1 - ox0, oy1 - oy0, oz1 - oz0])
    diag = float(np.linalg.norm(size))
    SW = int(px)
    SH = int(round(SW / math.sqrt(2)))
    M = int(SW * 0.02)
    head = int(SH * 0.085)
    cap = int(SH * 0.05)
    cw = (SW - 2 * M) // 3
    ch = (SH - head - 2 * M) // 2
    inner_w, inner_h = cw * 0.86, (ch - cap) * 0.86
    corners = np.array([[x, y, z] for x in overall[:2] for y in overall[2:4] for z in overall[4:]])
    spans = {}
    for vname, word, (R, U, D) in raster.VIEWS:
        cu, cv = corners @ R, corners @ U
        spans[vname] = (cu.min(), cu.max(), cv.min(), cv.max())
    s_ortho = min(min(inner_w / (sp[1] - sp[0]), inner_h / (sp[3] - sp[2])) for n, sp in spans.items() if n != "iso")
    sp = spans["iso"]
    s_iso = min(inner_w / (sp[1] - sp[0]), inner_h / (sp[3] - sp[2]))
    sheet = Image.new("RGB", (SW, SH), "white")
    dr = ImageDraw.Draw(sheet)
    printed = []

    def num(v, what, nd=1):
        t = fmt(v, nd)
        printed.append({"text": t, "value": float(v), "what": what})
        return t

    rows = []
    id_views = {}
    names = [p[0] for p in parts]
    for k, (vname, word, (R, U, D)) in enumerate(raster.VIEWS):
        s = s_iso if vname == "iso" else s_ortho
        u0, u1, v0, v1 = spans[vname]
        pad = 0.03 * max(u1 - u0, v1 - v0)
        t0 = _now()
        img, st, ids = raster.render_view(parts, edges, R, U, D, s, (u0 - pad, v0 - pad), (u1 + pad, v1 + pad), ss,
                                          diag, colors)
        if vname in ("front", "right", "top"):
            id_views[vname] = {"ids": ids, "refs": names, "lo": (u0 - pad, v0 - pad), "hi": (u1 + pad, v1 + pad), "s": s,
                               "off": 0.5 / ss, "axes": (R, U, D)}
        col, row = k % 3, k // 3
        cx0, cy0 = M + col * cw, head + M + row * ch
        pim = Image.fromarray(img)
        sheet.paste(pim, (int(cx0 + (cw - pim.width) / 2), int(cy0 + cap + (ch - cap - pim.height) / 2)))
        dr.rectangle([cx0 + 6, cy0 + 6, cx0 + cw - 6, cy0 + ch - 6], outline=(200, 205, 212), width=2)
        if vname == "iso":
            label = "%s (not to scale)" % word
        else:
            label = "%s   %s wide x %s high mm" % (word, num(u1 - u0, vname + " width"), num(v1 - v0, vname + " height"))
        dr.text((cx0 + 24, cy0 + 18), label, fill=(20, 28, 40), font=font(cap * 0.42))
        rows.append({"view": vname, "caption": label, "sec": round(_now() - t0, 3), **st})
        if clock:
            clock("view_" + vname)
    title = str(name)
    dr.text((M, int(M * 0.6)), title, fill=(10, 20, 40), font=font(head * 0.36), stroke_width=1, stroke_fill=(10, 20, 40))
    sub = "Rev %s   %s parts   overall %s x %s x %s mm (length x width x height)   orthographic views at one scale" % (
        revision, num(n_parts, "parts", 0), num(size[0], "overall X"), num(size[1], "overall Y"),
        num(size[2], "overall Z"))
    dr.text((M, int(M * 0.6 + head * 0.46)), sub, fill=(60, 70, 85), font=font(head * 0.22))
    rows.append({"view": "header_title", "caption": title})
    rows.append({"view": "header_sub", "caption": sub})
    buf = io.BytesIO()
    sheet.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), printed, rows, id_views


def _now():
    import time
    return time.perf_counter()
