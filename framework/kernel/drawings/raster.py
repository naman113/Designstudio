"""The shaded views of the views sheet: a vectorised scanline z-buffer in numpy (no GPU), per-part colour from
the studio viewer's palette, key and fill light, CAD edges from the B-rep edge segments with hidden parts removed against
the depth buffer, and outlines from depth, part and background jumps. A first
bounding-box rasterizer took 40-45 s per view; this scanline version takes 1.7-3.5 s, 20x.

Camera convention: right = up x toward_viewer; front looks along +Y with model Z up, right side
from +X, top from +Z with +Y up, rear from +Y, bottom from -Z with -Y up, and a three-quarter view.
"""
import math

import numpy as np

# keep equal with framework/web/static/index.html:692 (the viewer's palette); a part's colour = the index of its ref
# among the design's distinct refs in manifest order, modulo 8
PALETTE = [0xC9D1DC, 0x7FB3FF, 0xD9A441, 0x62C48A, 0xE0646C, 0xB39DDB, 0x80DEEA, 0xFFAB91]
EDGE_RGB = np.array([0x0A, 0x14, 0x28]) / 255.0
X, Y, Z = np.eye(3)


def view_axes(D, U):
    D = np.asarray(D, float) / np.linalg.norm(D)
    R = np.cross(U, D)
    R /= np.linalg.norm(R)
    U = np.cross(D, R)
    return R, U, D


ISO_D = np.array([math.cos(math.radians(30)) * math.cos(math.radians(-55)),
                  math.cos(math.radians(30)) * math.sin(math.radians(-55)), math.sin(math.radians(30))])
VIEWS = [  # name, caption word, (R, U, D) with D toward the viewer
    ("front", "Front", view_axes(-Y, Z)),
    ("right", "Right side", view_axes(X, Z)),
    ("top", "Top", view_axes(Z, Y)),
    ("rear", "Rear", view_axes(Y, Z)),
    ("bottom", "Bottom", view_axes(-Z, -Y)),
    ("iso", "Three-quarter", view_axes(ISO_D, Z)),
]


def shade(Pv, N, R, U, D, base):
    n = N * np.where((N @ D) < 0, -1.0, 1.0)[:, None]
    L = -0.45 * R + 0.6 * U + 0.65 * D
    L /= np.linalg.norm(L)
    L2 = 0.6 * R - 0.2 * U + 0.5 * D
    L2 /= np.linalg.norm(L2)
    H = L + D
    H /= np.linalg.norm(H)
    diff = np.clip(n @ L, 0, 1)
    fill = np.clip(n @ L2, 0, 1)
    spec = np.clip(n @ H, 0, 1) ** 24
    inten = 0.30 + 0.58 * diff + 0.22 * fill
    return np.clip(base[None, :] * inten[:, None] + 0.16 * spec[:, None], 0, 1)


def rasterize(x, y, z, col, T, pid, W, H, budget=4_000_000):
    """Vectorised scanline z-buffer: every triangle is expanded into its pixel rows, every row into its pixels (two
    np.repeat expansions), so the work is the covered area, not the bounding box (long slivers on cylinders cost
    nothing extra). Larger z = nearer. Returns zbuf (H*W), colour (H*W,3), part id (H*W), samples."""
    zbuf = np.full(W * H, -np.inf)
    cbuf = np.ones((W * H, 3))
    ibuf = np.full(W * H, -1, dtype=np.int32)
    tx, ty, tz = x[T], y[T], z[T]
    area = (tx[:, 1] - tx[:, 0]) * (ty[:, 2] - ty[:, 0]) - (tx[:, 2] - tx[:, 0]) * (ty[:, 1] - ty[:, 0])
    r0 = np.maximum(np.ceil(ty.min(1) - 0.5), 0).astype(np.int64)
    r1 = np.minimum(np.floor(ty.max(1) - 0.5), H - 1).astype(np.int64)
    nrow = r1 - r0 + 1
    keep = np.nonzero((np.abs(area) > 1e-12) & (nrow > 0) & (tx.max(1) >= 0) & (tx.min(1) < W))[0]
    samples = 0
    # chunks of triangles so that rows x mean span stays within the budget
    est = nrow[keep] * np.maximum(1, (tx.max(1) - tx.min(1))[keep]).astype(np.int64)
    cuts = np.searchsorted(np.cumsum(est), np.arange(budget, est.sum() + budget, budget))
    start = 0
    for stop in list(cuts) + [len(keep)]:
        if stop <= start:
            continue
        s = keep[start:stop]
        start = stop
        # rows
        rt = np.repeat(s, nrow[s])
        rr = np.arange(rt.size) - np.repeat(np.cumsum(nrow[s]) - nrow[s], nrow[s]) + np.repeat(r0[s], nrow[s])
        yc = rr + 0.5
        xl = np.full(rt.size, np.inf)
        xr = np.full(rt.size, -np.inf)
        for a, b in ((0, 1), (1, 2), (2, 0)):
            ya, yb, xa, xb = ty[rt, a], ty[rt, b], tx[rt, a], tx[rt, b]
            lo_, hi_ = np.minimum(ya, yb), np.maximum(ya, yb)
            ok = (yc >= lo_) & (yc <= hi_) & (hi_ > lo_)
            xi = xa + (yc - ya) * (xb - xa) / np.where(hi_ > lo_, yb - ya, 1.0)
            xl = np.where(ok, np.minimum(xl, xi), xl)
            xr = np.where(ok, np.maximum(xr, xi), xr)
        c0 = np.maximum(np.ceil(xl - 0.5), 0)
        c1 = np.minimum(np.floor(xr - 0.5), W - 1)
        ncol = np.where(np.isfinite(c0) & np.isfinite(c1), c1 - c0 + 1, 0).astype(np.int64)
        ncol = np.maximum(ncol, 0)
        if ncol.sum() == 0:
            continue
        tri = np.repeat(rt, ncol)
        py = np.repeat(rr, ncol)
        px = (np.arange(tri.size) - np.repeat(np.cumsum(ncol) - ncol, ncol) + np.repeat(c0.astype(np.int64), ncol))
        cx, cy = px + 0.5, py + 0.5
        x0, x1, x2 = tx[tri, 0], tx[tri, 1], tx[tri, 2]
        y0, y1, y2 = ty[tri, 0], ty[tri, 1], ty[tri, 2]
        a = area[tri]
        W0 = ((x1 - cx) * (y2 - cy) - (x2 - cx) * (y1 - cy)) / a
        W1 = ((x2 - cx) * (y0 - cy) - (x0 - cx) * (y2 - cy)) / a
        W2 = 1.0 - W0 - W1
        zz = W0 * tz[tri, 0] + W1 * tz[tri, 1] + W2 * tz[tri, 2]
        idx = py * W + px
        samples += idx.size
        order = np.lexsort((-zz, idx))
        ids = idx[order]
        first = np.empty(ids.size, bool)
        first[0] = True
        first[1:] = ids[1:] != ids[:-1]
        o = order[first]
        ci, cz = idx[o], zz[o]
        better = cz > zbuf[ci]
        o, ci, cz = o[better], ci[better], cz[better]
        t3 = T[tri[o]]
        cc = (W0[o, None] * col[t3[:, 0]] + W1[o, None] * col[t3[:, 1]] + W2[o, None] * col[t3[:, 2]])
        zbuf[ci] = cz
        cbuf[ci] = cc
        ibuf[ci] = pid[tri[o]]
    return zbuf, cbuf, ibuf, samples


def maxfilter3(a, W, H):
    g = a.reshape(H, W)
    p = np.pad(g, 1, constant_values=-np.inf)
    out = g.copy()
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            out = np.maximum(out, p[dy:dy + H, dx:dx + W])
    return out.reshape(-1)


def render_view(parts, edges, R, U, D, s, lo, hi, ss, diag, colors):
    """One view: s px/mm at final size, lo/hi the view-plane window (mm). `parts` = [(ref, P, N, T)], `edges` =
    {ref: S (k,2,3)}, `colors` = {ref: palette index}. Returns (RGB uint8 image, stats, part-id buffer at the final
    size: palette-independent index into `parts`, -1 = background; the assembly drawing reuses it for balloons)."""
    S = s * ss
    W = int(math.ceil((hi[0] - lo[0]) * S)) + 2
    H = int(math.ceil((hi[1] - lo[1]) * S)) + 2
    W += (-W) % ss
    H += (-H) % ss
    xs, ys, zs, cols, Ts, pids, base = [], [], [], [], [], [], 0
    for k, (ref, P, N, T) in enumerate(parts):
        c = PALETTE[colors.get(ref, k) % len(PALETTE)]
        rgb = np.array([(c >> sh) & 255 for sh in (16, 8, 0)]) / 255.0
        xs.append((P @ R - lo[0]) * S)
        ys.append((hi[1] - P @ U) * S)
        zs.append(P @ D)
        cols.append(shade(P, N, R, U, D, rgb))
        Ts.append(T + base)
        pids.append(np.full(len(T), k, dtype=np.int32))
        base += len(P)
    x, y, z, col = np.concatenate(xs), np.concatenate(ys), np.concatenate(zs), np.concatenate(cols)
    T, pid = np.concatenate(Ts), np.concatenate(pids)
    zbuf, cbuf, ibuf, nsamp = rasterize(x, y, z, col, T, pid, W, H)
    img = cbuf.reshape(H, W, 3).copy()
    # outlines: background boundary, part boundary, depth jump
    fg = np.isfinite(zbuf).reshape(H, W)
    zg = np.where(fg, zbuf.reshape(H, W), np.nan)
    idg = ibuf.reshape(H, W)
    jump = 0.004 * diag
    line = np.zeros((H, W), bool)
    for dy, dx in ((0, 1), (1, 0)):
        a_fg, b_fg = fg[:H - dy, :W - dx], fg[dy:, dx:]
        a_z, b_z = zg[:H - dy, :W - dx], zg[dy:, dx:]
        a_i, b_i = idg[:H - dy, :W - dx], idg[dy:, dx:]
        e = (a_fg != b_fg) | (a_fg & b_fg & ((a_i != b_i) | (np.abs(a_z - b_z) > jump)))
        line[:H - dy, :W - dx] |= e
        line[dy:, dx:] |= e
    # B-rep edges, hidden parts removed against the depth buffer (3x3 max filter, tolerance)
    zmax = maxfilter3(zbuf, W, H)
    eps = 1.5 / S + 0.0015 * diag
    nseg = 0
    for ref, Sg in edges.items():
        if not len(Sg):
            continue
        a, b = Sg[:, 0, :], Sg[:, 1, :]
        ax, ay, az = (a @ R - lo[0]) * S, (hi[1] - a @ U) * S, a @ D
        bx, by, bz = (b @ R - lo[0]) * S, (hi[1] - b @ U) * S, b @ D
        n = np.maximum(1, np.ceil(np.hypot(bx - ax, by - ay) * 1.5).astype(np.int64))
        nseg += len(n)
        rep = np.repeat(np.arange(len(n)), n + 1)
        t = (np.arange(rep.size) - np.repeat(np.cumsum(n + 1) - (n + 1), n + 1)) / np.repeat(n, n + 1)
        px = ax[rep] + (bx[rep] - ax[rep]) * t
        py = ay[rep] + (by[rep] - ay[rep]) * t
        pz = az[rep] + (bz[rep] - az[rep]) * t
        ix, iy = px.astype(np.int64), py.astype(np.int64)
        ok = (ix >= 0) & (iy >= 0) & (ix < W) & (iy < H)
        ix, iy, pz = ix[ok], iy[ok], pz[ok]
        vis = pz >= zmax[iy * W + ix] - eps
        line[iy[vis], ix[vis]] = True
    img[line] = EDGE_RGB
    img = img.reshape(H // ss, ss, W // ss, ss, 3).mean(axis=(1, 3))
    ids = idg[::ss, ::ss][:H // ss, :W // ss]
    return (np.clip(img, 0, 1) * 255).astype(np.uint8), {"px": [W // ss, H // ss], "samples": int(nsamp),
                                                          "edge_segments": nseg}, ids
