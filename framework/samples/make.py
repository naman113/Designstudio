"""Write the sample STEP files from fixed build123d shapes.

Usage: python -m framework.samples.make OUTDIR   ->  OUTDIR/<file> for every entry of framework.samples.SAMPLES
  flange.step            one solid: OD 80 x 10, bore d30, 4 holes d8 on PCD 55
  bushing.step           one solid: OD 30 x 25, bore d20
  mounting_plate.step    one solid: 100 x 60 x 8 with 4 holes d6.6, written with the STEP material "Aluminium 6061" (2700 kg/m3)
  shaft.step             one solid: stepped shaft d20 x 90 with a d15 x 30 journal
  spacer.step            one solid: OD 24 x 10, bore d13
  bracket_assembly.step  two identical side plates 60 x 40 x 5 (the second at Pos(0, 0, 40) * Rot(0, 0, 90)) and a pin d8 x 30
Fixed dimensions, no randomness: the same files every run.
"""
import math
import sys
from pathlib import Path

from build123d import Box, Compound, Cylinder, Pos, Rot, export_step
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.Interface import Interface_Static
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_StepModelType
from OCP.TCollection import TCollection_ExtendedString, TCollection_HAsciiString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool

from framework.samples import SAMPLES


def flange():
    r = Cylinder(40, 10) - Cylinder(15, 10)
    for k in range(4):
        a = math.radians(45 + 90 * k)
        r = r - Pos(27.5 * math.cos(a), 27.5 * math.sin(a), 0) * Cylinder(4, 10)
    return r


def bushing():
    return Cylinder(15, 25) - Cylinder(10, 25)


def mounting_plate():
    p = Box(100, 60, 8)
    for x in (-40, 40):
        for y in (-20, 20):
            p = p - Pos(x, y, 0) * Cylinder(3.3, 8)
    return p


def shaft():
    return Cylinder(10, 90) + Pos(0, 0, 60) * Cylinder(7.5, 30)


def spacer():
    return Cylinder(12, 10) - Cylinder(6.5, 10)


def write_part(shape, label, path):
    shape.label = label
    export_step(shape, str(path))


def write_with_material(shape, label, material, density, path):
    """One solid written through OCP XCAF with its name and a STEP material (name, density)."""
    Interface_Static.SetCVal_s("write.step.schema", "AP214IS")
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    lbl = st.AddShape(shape.wrapped, False)
    TDataStd_Name.Set_s(lbl, TCollection_ExtendedString(label))
    mt = XCAFDoc_DocumentTool.MaterialTool_s(doc.Main())
    ml = mt.AddMaterial(TCollection_HAsciiString(material), TCollection_HAsciiString("sample material"),
                        float(density), TCollection_HAsciiString("density"), TCollection_HAsciiString("POSITIVE_RATIO_MEASURE"))
    mt.SetMaterial(lbl, ml)
    w = STEPCAFControl_Writer()
    w.SetNameMode(True)
    w.SetMaterialMode(True)
    if not w.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs):
        raise RuntimeError("%s: transfer failed" % label)
    if w.Write(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError("%s: write failed" % label)


def bracket_assembly(path):
    plate1 = Box(60, 40, 5)
    plate1.label = "side_plate_1"
    plate2 = Pos(0, 0, 40) * Rot(0, 0, 90) * Box(60, 40, 5)
    plate2.label = "side_plate_2"
    pin = Pos(0, 0, 20) * Cylinder(4, 30)
    pin.label = "pin"
    export_step(Compound(label="bracket_assembly", children=[plate1, plate2, pin]), str(path))


WRITERS = {
    "flange": lambda p: write_part(flange(), "flange", p),
    "bushing": lambda p: write_part(bushing(), "bushing", p),
    "mounting_plate": lambda p: write_with_material(mounting_plate(), "mounting_plate", "Aluminium 6061", 2700, p),
    "shaft": lambda p: write_part(shaft(), "shaft", p),
    "spacer": lambda p: write_part(spacer(), "spacer", p),
    "bracket_assembly": bracket_assembly,
}


def main(argv):
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    out = Path(argv[0]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for s in SAMPLES:
        p = out / s["file"]
        WRITERS[s["name"]](p)
        print(p, p.stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
