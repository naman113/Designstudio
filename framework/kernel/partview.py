"""One part on its own: the part's exact shape -> a binary glTF for the catalogue's part view, and a
STEP file of that part alone for its download. Runs only inside a geometry worker (framework/kernel/ops.py `part_view`,
`part_step`); the shape comes from the catalogue's own candidates (framework/catalog/pg/signatures.py `shape_sources`).

Why this file and not a node cut out of the design's view (measured on 541 parts, 42 design views): a node cut
from a design view covers only the 497 parts used in a design (the 44 parts in no design have none), and its box equals
the catalogue's stored box within 0.01 mm for 392 of 497 nodes (designs place some parts elsewhere: up to 6,178 mm off).
The part's exact shape is reachable for 540 of 541 parts and sits in the catalogue's own frame (the kernel's box of it
equals the stored box within 0.0088 mm for all 540), and meshing it takes 0.014 s median, 0.45 s at the 95th percentile.

The mesh rule is the design view's (`write_design` below): angular 0.15 rad, linear 0.0004 x the diagonal of
the shape's box clamped to 0.01..0.5 mm, normals averaged within each face (smooth on curved faces, crisp at real edges),
B-rep edges as lines, seams left out. A chord can cut inside a curved face, so the mesh can stop short of the shape's
true extent (measured: 74 of 457 parts more than 0.01 mm short, up to 0.10 mm). So each face that could reach past the
mesh's box is checked against its exact extent (`BRepBndLib.AddOptimal`, no triangulation, no tolerance;
framework/kernel/drawings/extents.py) and meshed again on its own, finer, until it reaches it (`mesh`). Making the whole
part finer instead cost up to 5.6 million triangles and 130 s for one part (measured). The catalogue's stored box
(`BRepBndLib.Add`, measure.py, unchanged for the golden numbers) adds the control-point hull of curved faces and can be
larger than the shape (measured: 99 of 540 parts more than 0.01 mm, up to 1,117 mm on one large curved part); the view never
copies it.
"""
import json
import os
import struct
import time

import numpy as np
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape

from framework.common.partview import VIEW_RULE   # noqa: E402,F401  (part of the cache key; bumped there with the rule)
ANGULAR = 0.15                    # rad, the design view's rule
LIN_REL, LIN_MIN, LIN_MAX = 0.0004, 0.01, 0.5
BOX_TOL = 0.004                   # mm: every side of the view's box within this of the exact extent (criterion: 0.01)
FACE_ROUNDS = 6                   # a face that gives the part its extent is meshed again at most this many times
FACE_TRI_CAP = 100_000            # ... and not when that face would pass this many triangles (each round about x4)
PART_TRI_CAP = 600_000            # ... nor when the part would pass this many (measured: one face of a cast part reached 1.1 M)


# ---- the shape ----------------------------------------------------------------------------------------------------------
def read_brep(path):
    s = TopoDS_Shape()
    if not BRepTools.Read_s(s, path, BRep_Builder()) or s.IsNull():
        raise ValueError("no shape in %s" % path)
    return s


def _write_brep(shape, path):
    from framework.common.files import replace_retry
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp%d.brep" % os.getpid()
    BRepTools.Write_s(shape, tmp)
    replace_retry(tmp, path)               # another worker may be reading the same file this moment (Windows)


SPLIT_WAIT_S = 300                         # a second worker waits this long for the first one's reading of the same file
STALE_S = 900                              # a reading mark older than this was left by a stopped worker


def _split_step(path, sha, folder, listing):
    """Read the STEP file once and keep each of its parts as its own shape file (`_parts.json` names them, written last).
    Two workers asked for parts of the same file at once: the first one reads it (an exclusive `.reading` mark); the
    second waits for its `_parts.json` (seen: two parallel first views of one file met a Windows sharing error when both
    wrote the same files). -> the model when this worker read the file, else None (the files are there, or the wait ended)."""
    from framework.common.files import replace_retry
    os.makedirs(folder, exist_ok=True)
    mark = os.path.join(folder, ".reading")
    end = time.monotonic() + SPLIT_WAIT_S
    while True:
        try:
            os.close(os.open(mark, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(mark) > STALE_S:
                    os.remove(mark)
                    continue
            except OSError:
                continue
            if os.path.isfile(listing) or time.monotonic() > end:
                return None
            time.sleep(0.2)
    try:
        if os.path.isfile(listing):
            return None
        from framework.kernel import io as kio
        from framework.common import partview as cpv
        model = kio.load(path)
        names = []
        for q in model.parts:
            dst = cpv.split_file(sha, q.ref)
            if dst is None or q.ref in names:
                continue
            _write_brep(q.shape, dst)
            names.append(q.ref)
        tmp = listing + ".tmp%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"file_sha256": sha, "parts": names}, fh)
        replace_retry(tmp, listing)
        return model
    finally:
        try:
            os.remove(mark)
        except OSError:
            pass


def load_source(sources):
    """The part's exact shape from its candidates [(path, object), ...] in order (signatures.shape_sources): a shape file
    (`.brep`, that part alone) or a STEP file and the part's object in it. A STEP file is read once: every part in it is
    kept as its own shape file under work/partviews/_split/<sha12>/ (with `_parts.json` naming them), so the next part of
    the same file, or an object the file does not hold, needs no second read of the file. -> (shape, index of the source
    used, "dump" | "step") or (None, None, None)."""
    from framework.common import partview as cpv
    for i, (path, obj) in enumerate(sources):
        if not os.path.isfile(path):
            continue
        if path.lower().endswith(".brep"):
            return read_brep(path), i, "dump"
        sha = cpv.file_sha(path)
        mine = cpv.split_file(sha, obj)
        folder = os.path.join(cpv.SPLIT, sha[:12])
        listing = os.path.join(folder, "_parts.json")
        if not os.path.isfile(listing):
            model = _split_step(path, sha, folder, listing)
            if model is not None:
                for q in model.parts:
                    if q.ref == obj:
                        return q.shape, i, "step"
                continue                               # the file does not hold this object
        if mine and os.path.isfile(mine):
            return read_brep(mine), i, "step"
    return None, None, None


def extent(shape):
    """(xmin, xmax, ymin, ymax, zmin, zmax): the shape's exact extent (no triangulation, no tolerance)."""
    b = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, b, False, False)
    x0, y0, z0, x1, y1, z1 = b.Get()
    return (x0, x1, y0, y1, z0, z1)


def loose_box(shape):
    """The quick box (`BRepBndLib.Add` without triangulation: what the catalogue stores); it holds the exact extent."""
    b = Bnd_Box()
    BRepBndLib.Add_s(shape, b, False)
    x0, y0, z0, x1, y1, z1 = b.Get()
    return (x0, x1, y0, y1, z0, z1)


def deflection(ext):
    diag = ((ext[1] - ext[0]) ** 2 + (ext[3] - ext[2]) ** 2 + (ext[5] - ext[4]) ** 2) ** 0.5
    return min(LIN_MAX, max(LIN_MIN, diag * LIN_REL))


# ---- the mesh -----------------------------------------------------------------------------------------------------------
def _faces_and_edges(shape):
    """[faces], [edges drawn as lines]: every edge of a face except its seams (an edge closed on that face, e.g. the join
    line of a cylinder) and degenerate edges, each once."""
    faces, emap = [], TopTools_IndexedMapOfShape()
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        f = TopoDS.Face_s(ex.Current())
        faces.append(f)
        ee = TopExp_Explorer(f, TopAbs_EDGE)
        while ee.More():
            e = TopoDS.Edge_s(ee.Current())
            if not BRep_Tool.Degenerated_s(e) and not BRep_Tool.IsClosed_s(e, f):
                emap.Add(e)
            ee.Next()
        ex.Next()
    return faces, [TopoDS.Edge_s(emap.FindKey(i)) for i in range(1, emap.Extent() + 1)]


def _face_mesh(f):
    """-> (P (n,3) f64, T (m,3) i64, normals (n,3) f64) of one face's current triangulation (outward winding), or None."""
    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation_s(f, loc)
    if tri is None or tri.NbTriangles() == 0:
        return None
    n = tri.NbNodes()
    if loc.IsIdentity():
        P = np.array([tri.Node(i).Coord() for i in range(1, n + 1)], dtype=np.float64)
    else:
        tr = loc.Transformation()
        P = np.array([tri.Node(i).Transformed(tr).Coord() for i in range(1, n + 1)], dtype=np.float64)
    T = np.array([tri.Triangle(i).Get() for i in range(1, tri.NbTriangles() + 1)], dtype=np.int64) - 1
    if f.Orientation() == TopAbs_REVERSED:
        T = T[:, ::-1]
    fn = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])          # area-weighted, averaged within the face
    vn = np.zeros_like(P)
    for k in range(3):
        np.add.at(vn, T[:, k], fn)
    vn /= np.maximum(np.linalg.norm(vn, axis=1), 1e-20)[:, None]
    return P, T, vn


def _edge_points(edges, lin, ang):
    segs = []
    for e in edges:
        d = GCPnts_TangentialDeflection(BRepAdaptor_Curve(e), ang, lin)
        k = d.NbPoints()
        if k < 2:
            continue
        pts = np.array([d.Value(i).Coord() for i in range(1, k + 1)], dtype=np.float64)
        segs.append(np.stack([pts[:-1], pts[1:]], axis=1).reshape(-1, 3))
    return np.concatenate(segs) if segs else np.zeros((0, 3))


def _box_of(P):
    mn, mx = P.min(axis=0), P.max(axis=0)
    return (float(mn[0]), float(mx[0]), float(mn[1]), float(mx[1]), float(mn[2]), float(mx[2]))


def _short(have, want):
    """How far `have` (a mesh box) stays inside `want` (an exact box), side by side (>= 0 when inside)."""
    return [have[j] - want[j] if j % 2 == 0 else want[j] - have[j] for j in range(6)]


def _beyond(face_box, box):
    """Sides (0..5) on which `face_box` reaches past `box` by more than BOX_TOL."""
    return [j for j in range(6) if (box[j] - face_box[j] if j % 2 == 0 else face_box[j] - box[j]) > BOX_TOL]


def mesh(shape):
    """The view's mesh (module docstring). The whole part is meshed with the design view's rule. Then each face whose
    quick box reaches past the mesh's box has its exact extent computed; where that reaches past too by more than
    BOX_TOL (a chord cutting inside a curved face), the face alone is meshed again, both deflections halved each time,
    until it reaches its exact extent on those sides, at most FACE_ROUNDS times. A face whose shortfall does not shrink
    by a quarter in a round is left as it is: its exact box is not on the face (a trimmed freeform face, whose box covers
    the untrimmed surface), and so is a face that would pass FACE_TRI_CAP or take the part past PART_TRI_CAP. The part's exact extent is the mesh's box joined with the exact extents of those faces (every
    other face lies inside the mesh's box by its quick box). -> dict."""
    lin = deflection(loose_box(shape))
    faces, edges = _faces_and_edges(shape)
    BRepMesh_IncrementalMesh(shape, lin, False, ANGULAR, True)
    meshes = [_face_mesh(f) for f in faces]
    if not any(m is not None for m in meshes):
        raise ValueError("the shape has no faces to show")
    S = _edge_points(edges, lin, ANGULAR)
    box = _box_of(np.concatenate([m[0] for m in meshes if m is not None] + ([S] if len(S) else [])))
    exact, refined, rounds_max, checked = list(box), 0, 0, 0
    for i, f in enumerate(faces):
        if meshes[i] is None or not _beyond(loose_box(f), box):
            continue
        checked += 1
        fb = extent(f)
        for j in range(6):
            exact[j] = min(exact[j], fb[j]) if j % 2 == 0 else max(exact[j], fb[j])
        sides = _beyond(fb, _box_of(meshes[i][0]))
        if not sides:
            continue
        short = lambda P: max((_box_of(P)[j] - fb[j]) if j % 2 == 0 else (fb[j] - _box_of(P)[j]) for j in sides)
        gap, fl, fa, r = short(meshes[i][0]), lin, ANGULAR, 0
        while gap > BOX_TOL and r < FACE_ROUNDS:
            now = len(meshes[i][1])
            if 4 * now > FACE_TRI_CAP or sum(len(m[1]) for m in meshes if m is not None) + 3 * now > PART_TRI_CAP:
                break
            r += 1
            fl, fa = max(fl * 0.5, 1e-4), fa * 0.5
            BRepTools.Clean_s(f)
            BRepMesh_IncrementalMesh(f, fl, False, fa, True)
            m = _face_mesh(f)
            if m is None:
                break
            meshes[i] = m
            new_gap = short(m[0])
            if new_gap > 0.75 * gap:
                break
            gap = new_gap
        refined += 1
        rounds_max = max(rounds_max, r)
    if refined:
        box = _box_of(np.concatenate([m[0] for m in meshes if m is not None] + ([S] if len(S) else [])))
    for j in range(6):
        exact[j] = min(exact[j], box[j]) if j % 2 == 0 else max(exact[j], box[j])
    Ps, Ns, Ts, base = [], [], [], 0
    for m in meshes:
        if m is None:
            continue
        Ps.append(m[0])
        Ns.append(m[2])
        Ts.append(m[1] + base)
        base += len(m[0])
    gap = max((box[j] - exact[j]) if j % 2 == 0 else (exact[j] - box[j]) for j in range(6))
    return {"P": np.concatenate(Ps), "N": np.concatenate(Ns).astype(np.float32),
            "T": np.concatenate(Ts).astype(np.uint32), "S": S, "extent": tuple(exact), "box": box,
            "gap": max(gap, 0.0), "lin": lin, "ang": ANGULAR, "faces_checked": checked, "refined_faces": refined,
            "rounds": rounds_max}


# ---- the file -----------------------------------------------------------------------------------------------------------
class _Glb:
    """A minimal binary glTF 2.0 writer (the design view's layout)."""

    def __init__(self):
        self.bin = bytearray()
        self.j = {"asset": {"version": "2.0", "generator": "buildcore"}, "scene": 0, "scenes": [{"nodes": []}],
                  "nodes": [], "meshes": [], "accessors": [], "bufferViews": []}

    def _view(self, arr, target):
        while len(self.bin) % 4:
            self.bin.append(0)
        off = len(self.bin)
        self.bin += arr.tobytes()
        self.j["bufferViews"].append({"buffer": 0, "byteOffset": off, "byteLength": arr.nbytes, "target": target})
        return len(self.j["bufferViews"]) - 1

    def _acc(self, arr, kind, ctype, target, minmax=False):
        a = {"bufferView": self._view(arr, target), "componentType": ctype, "count": int(arr.shape[0]), "type": kind}
        if minmax:
            a["min"] = [float(x) for x in arr.min(axis=0)]
            a["max"] = [float(x) for x in arr.max(axis=0)]
        self.j["accessors"].append(a)
        return len(self.j["accessors"]) - 1

    def node(self, name, pos, normals=None, indices=None, mode=4, extras=None):
        prim = {"attributes": {"POSITION": self._acc(pos, "VEC3", 5126, 34962, True)}, "mode": mode}
        if normals is not None:
            prim["attributes"]["NORMAL"] = self._acc(normals, "VEC3", 5126, 34962)
        if indices is not None:
            prim["indices"] = self._acc(indices, "SCALAR", 5125, 34963)
        self.j["meshes"].append({"name": name, "primitives": [prim]})
        self.j["nodes"].append({"name": name, "mesh": len(self.j["meshes"]) - 1, "extras": extras or {}})
        self.j["scenes"][0]["nodes"].append(len(self.j["nodes"]) - 1)

    def write(self, path, extras):
        self.j["asset"]["extras"] = extras
        self.j["buffers"] = [{"byteLength": len(self.bin)}]
        js = json.dumps(self.j, separators=(",", ":")).encode()
        js += b" " * (-len(js) % 4)
        while len(self.bin) % 4:
            self.bin.append(0)
        total = 12 + 8 + len(js) + 8 + len(self.bin)
        tmp = path + ".tmp%d" % os.getpid()
        with open(tmp, "wb") as fh:
            fh.write(struct.pack("<III", 0x46546C67, 2, total))
            fh.write(struct.pack("<II", len(js), 0x4E4F534A))
            fh.write(js)
            fh.write(struct.pack("<II", len(self.bin), 0x004E4942))
            fh.write(bytes(self.bin))
        os.replace(tmp, path)


def write_view(shape, out_path, extras=None):
    """Mesh `shape` and write the part's view to `out_path` (a temp file, then an atomic replace). The positions are
    stored as 32-bit floats, in mm, Z up, in the catalogue's frame. -> the stats (also in the file's asset extras)."""
    t0 = time.time()
    m = mesh(shape)
    P32, S32 = m["P"].astype(np.float32), m["S"].astype(np.float32)
    g = _Glb()
    g.node("part", P32, m["N"], m["T"].ravel(), extras={"ref": "part"})
    if len(S32):
        g.node("part__edges", S32, mode=1, extras={"edgesOf": "part"})
    allp = np.concatenate([P32, S32]) if len(S32) else P32
    box32 = [float(allp[:, 0].min()), float(allp[:, 0].max()), float(allp[:, 1].min()), float(allp[:, 1].max()),
             float(allp[:, 2].min()), float(allp[:, 2].max())]
    stats = {"view_rule": VIEW_RULE, "units": "mm", "up": "Z", "triangles": int(len(m["T"])),
             "edge_segments": int(len(S32) // 2), "extent": [float(x) for x in m["extent"]], "box": box32,
             "gap_mm": float(max(abs(a - b) for a, b in zip(box32, m["extent"]))), "linear_mm": m["lin"],
             "angular_rad": m["ang"], "faces_checked": m["faces_checked"], "refined_faces": m["refined_faces"],
             "finer_rounds": m["rounds"]}
    stats.update(extras or {})
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    stats["ms"] = round((time.time() - t0) * 1000.0, 1)
    g.write(out_path, stats)
    stats["bytes"] = os.path.getsize(out_path)
    return stats


def write_design(parts, out_path, header=""):
    """A whole design as one GLB (the studio's viewer mesh and the GLB download; no FreeCAD): for each `(ref, located shape)` in `parts` (one per occurrence, brep_dump.product_parts), a node named
    `ref` with extras {"ref": ref} and, when the part has edges, a line node `ref__edges` with extras {"edgesOf": ref}.
    Several occurrences of one part each get their own node with the same name. A part with no faces, or one OCP cannot
    mesh, is skipped (one bad part never costs the design its view); ValueError when no part could be meshed (no empty
    GLB is written). The file is written to a temp file, then replaced atomically.
    -> stats {"parts", "triangles", "skipped", "ms", "bytes"}."""
    t0 = time.time()
    g, n, tris, skipped = _Glb(), 0, 0, []
    for ref, shape in parts:
        try:
            m = mesh(shape)
        except Exception as exc:                  # "no faces", or an OCP failure on a bad face
            if not isinstance(exc, ValueError):
                print("  mesh: part %s skipped (%s: %s)" % (ref, type(exc).__name__, str(exc)[:160]), flush=True)
            skipped.append(ref)
            continue
        g.node(ref, m["P"].astype(np.float32), m["N"], m["T"].ravel(), extras={"ref": ref})
        if len(m["S"]):
            g.node(ref + "__edges", m["S"].astype(np.float32), mode=1, extras={"edgesOf": ref})
        n += 1
        tris += int(len(m["T"]))
    if n == 0:
        raise ValueError("no part could be meshed (%d skipped)" % len(skipped))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    g.write(out_path, {"product": header, "units": "mm", "up": "Z"})
    return {"parts": n, "triangles": tris, "skipped": skipped, "ms": round((time.time() - t0) * 1000.0, 1),
            "bytes": os.path.getsize(out_path)}


def write_part_step(shape, out_path, name):
    """The part alone as one labelled AP214 / mm STEP file (framework/kernel/io.py write_step), via a temp file."""
    from framework.kernel import io as kio
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp%d.step" % os.getpid()
    kio.write_step([(name, shape)], tmp, root_name=name)
    os.replace(tmp, out_path)
    return {"bytes": os.path.getsize(out_path)}
