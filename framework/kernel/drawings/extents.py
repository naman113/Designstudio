"""Tight extents: the sizes a drawing prints.

Finding 1 (measured): the catalogue's box, `framework/kernel/measure.py` `_bbox6`
(`BRepBndLib.Add` without triangulation), is up to 10.11 mm too large because it adds the shape tolerance and the
control-point hull of curved faces (the gearbox CASING reads z 350.2 where the true size is 330.0; a cover of the larger
assembly +5.87 mm). `BRepBndLib.AddOptimal(shape, box, False, False)` (exact geometry, no triangulation, no tolerance) agrees with
the tessellation's vertices within 0.031 mm (gearbox) and 0.012 mm (the larger assembly) and costs 0.08-0.30 s per design.

`measure.py` is never changed: the golden suite, the part card's recording, families.json and the blend rule read the
old box. The tight size is a NEW field, shown next to it.
"""
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib


def extent(shape):
    """(xmin, xmax, ymin, ymax, zmin, zmax) of a placed shape, tight."""
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return (xmin, xmax, ymin, ymax, zmin, zmax)


def union(boxes):
    """The extent holding every extent in `boxes`."""
    xs0, xs1, ys0, ys1, zs0, zs1 = zip(*boxes)
    return (min(xs0), max(xs1), min(ys0), max(ys1), min(zs0), max(zs1))


def size(ext):
    """(x, y, z) sizes of an extent."""
    return (ext[1] - ext[0], ext[3] - ext[2], ext[5] - ext[4])
