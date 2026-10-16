"""Tessellate parts into one grouped Wavefront OBJ, in-process.

A direct OCP tessellation (no external-process meshing): BRepMesh_IncrementalMesh at the same deflections
(LinearDeflection=0.12, AngularDeflection=0.26, Relative=False), one `o <ref>` group per
part in the OBJ (a qty > 1 part's repeated occurrences share one group -- consecutive
(ref, shape) tuples with the same ref are the same group, one write per occurrence), `v %.4f %.4f %.4f` / `f a b c` triangle lines with a 1-based global
vertex offset, and a REVERSED face's triangle winding flipped (two indices swapped) so every
face's outward normal survives tessellation regardless of its topological orientation. No
external CAD application, no MeshPart, no child process -- framework.kernel is the only
package importing OCP.

Kernel-only module. Run with -m:
    python -m framework.kernel.mesh <out.obj> <file.step> [<file.step> ...]
"""
import os
import sys
import time

from OCP.BRep import BRep_Tool
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS


def write_obj(parts, out_path: str, linear_deflection: float = 0.12,
              angular_deflection: float = 0.26, header: str = "") -> dict:
    """Tessellate each ``(ref, TopoDS_Shape)`` in ``parts`` (already located in model
    coordinates, mm -- see framework.kernel.io.all_shapes) and write one OBJ to
    ``out_path``. Returns ``{"path", "groups", "triangles", "seconds"}``
    (seconds = tessellation + write, not counting any prior STEP load). Also writes
    ``<out_path>.log`` with ``exported <n> parts, <m> triangles in <s> s to <out_path>``,
    the log line plus timing.
    """
    t0 = time.time()
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    n_groups = n_tri = vertex_offset = 0
    current_ref = None
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("# product %s  (Z-up, mm)\n" % header)
        for ref, shape in parts:
            if ref != current_ref:
                fh.write("o %s\n" % ref)
                current_ref = ref
                n_groups += 1
            BRepMesh_IncrementalMesh(shape, linear_deflection, False, angular_deflection, True)
            exp = TopExp_Explorer(shape, TopAbs_FACE)
            while exp.More():
                face = TopoDS.Face_s(exp.Current())
                loc = TopLoc_Location()
                tri = BRep_Tool.Triangulation_s(face, loc)
                if tri is None:
                    exp.Next()
                    continue
                trsf = loc.Transformation()
                reversed_face = face.Orientation() == TopAbs_REVERSED
                for i in range(1, tri.NbNodes() + 1):
                    p = tri.Node(i).Transformed(trsf)
                    fh.write("v %.4f %.4f %.4f\n" % (p.X(), p.Y(), p.Z()))
                for i in range(1, tri.NbTriangles() + 1):
                    a, b, c = tri.Triangle(i).Get()
                    if reversed_face:
                        b, c = c, b
                    fh.write("f %d %d %d\n" % (
                        a + vertex_offset, b + vertex_offset, c + vertex_offset))
                    n_tri += 1
                vertex_offset += tri.NbNodes()
                exp.Next()

    seconds = time.time() - t0
    with open(out_path + ".log", "w", encoding="utf-8") as fh:
        fh.write("exported %d parts, %d triangles in %.3f s to %s\n" % (
            n_groups, n_tri, seconds, out_path))
    return {"path": out_path, "groups": n_groups, "triangles": n_tri, "seconds": seconds}


def _cli(argv):
    if len(argv) < 2:
        print("usage: python -m framework.kernel.mesh <out.obj> <file.step> [<file.step> ...]")
        return 2
    out_path, step_files = argv[0], argv[1:]

    from framework.kernel import io

    t0 = time.time()
    all_parts = []
    for f in step_files:
        m = io.load(f)
        all_parts.extend(io.all_shapes(m))
    load_seconds = time.time() - t0

    header = os.path.splitext(os.path.basename(out_path))[0]
    stats = write_obj(all_parts, out_path, header=header)
    total_seconds = time.time() - t0
    print("mesh: %d groups, %d triangles, load %.3f s, mesh %.3f s, total %.3f s -> %s" % (
        stats["groups"], stats["triangles"], load_seconds, stats["seconds"], total_seconds,
        out_path))
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
