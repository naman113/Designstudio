"""The kernel's own triangles for a drawing: positions, per-vertex normals, triangles and B-rep edge segments of
one placed shape, from `BRepMesh_IncrementalMesh`, so a drawing never depends on the 3D view file or a GPU.

Deflection rule = the viewer's mesh (framework/kernel/partview.py): angular 0.15 rad, linear `min(0.5, max(0.01, 0.0004 x diagonal))` mm.
Measured: gearbox 1.5 s (63,338 triangles, 43,834 edge segments), a 54-part assembly 8.0 s (531,986 /
135,898). Normals come from the face normals of the triangles (reversed faces flipped), averaged per vertex.
"""
import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS

from framework.kernel.drawings import extents

ANGULAR = 0.15


def deflection(shape):
    e = extents.extent(shape)
    diag = ((e[1] - e[0]) ** 2 + (e[3] - e[2]) ** 2 + (e[5] - e[4]) ** 2) ** 0.5
    return min(0.5, max(0.01, diag * 0.0004))


def tessellate(shape):
    """-> (P (n,3), N (n,3), T (m,3) int64, S (k,2,3)): vertices, normals, triangles, edge segments (all placed, mm)."""
    lin = deflection(shape)
    BRepMesh_IncrementalMesh(shape, lin, False, ANGULAR, True)
    Ps, Ns, Ts, base = [], [], [], 0
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        f = TopoDS.Face_s(ex.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(f, loc)
        if tri is not None:
            tr = loc.Transformation()
            n = tri.NbNodes()
            P = np.array([[p.X(), p.Y(), p.Z()] for p in (tri.Node(i).Transformed(tr) for i in range(1, n + 1))])
            T = np.array([tri.Triangle(i).Get() for i in range(1, tri.NbTriangles() + 1)], dtype=np.int64) - 1
            if f.Orientation() == TopAbs_REVERSED:
                T = T[:, ::-1]
            fn = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
            vn = np.zeros_like(P)
            for k in range(3):
                np.add.at(vn, T[:, k], fn)
            vn /= np.maximum(np.linalg.norm(vn, axis=1), 1e-12)[:, None]
            Ps.append(P)
            Ns.append(vn)
            Ts.append(T + base)
            base += n
        ex.Next()
    segs = []
    ex = TopExp_Explorer(shape, TopAbs_EDGE)
    while ex.More():
        ed = TopoDS.Edge_s(ex.Current())
        if not BRep_Tool.Degenerated_s(ed):
            c = BRepAdaptor_Curve(ed)
            d = GCPnts_TangentialDeflection(c, ANGULAR, lin)
            pts = np.array([[d.Value(i).X(), d.Value(i).Y(), d.Value(i).Z()] for i in range(1, d.NbPoints() + 1)])
            if len(pts) > 1:
                segs.append(np.stack([pts[:-1], pts[1:]], axis=1))
        ex.Next()
    return (np.concatenate(Ps) if Ps else np.zeros((0, 3)), np.concatenate(Ns) if Ns else np.zeros((0, 3)),
            np.concatenate(Ts) if Ts else np.zeros((0, 3), np.int64), np.concatenate(segs) if segs else np.zeros((0, 2, 3)))
