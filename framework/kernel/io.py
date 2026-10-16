"""STEP load through the XCAF reader: product names, assembly tree and repeated
placements survive; units are detected and converted to mm; optional ShapeFix healing.
Also the matching labelled AP214/mm STEP writer.

Kernel-only module (framework.kernel is the ONLY package that imports OCP/build123d).
Run with -m: `python -m framework.kernel.io` -- never
`python framework/kernel/io.py` directly (that would put framework/kernel on sys.path
and this module would shadow the stdlib `io`).
"""
import os
import time
from dataclasses import dataclass, field

import build123d
from build123d import Compound, Unit, export_step
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.gp import gp_Trsf
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.Interface import Interface_Static
from OCP.ShapeFix import ShapeFix_Shape
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TColStd import TColStd_SequenceOfAsciiString
from OCP.TDataStd import TDataStd_Name, TDataStd_TreeNode
from OCP.TDF import TDF_Label, TDF_LabelSequence, TDF_Tool
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc, XCAFDoc_DocumentTool, XCAFDoc_Material

_UNIT_MAP = {
    "MM": "mm", "MILLIMETRE": "mm", "MILLIMETER": "mm",
    "INCH": "inch", "IN": "inch", "INCHES": "inch",
    "M": "m", "METRE": "m", "METER": "m",
    "CM": "cm", "CENTIMETRE": "cm", "CENTIMETER": "cm",
    "FT": "ft", "FOOT": "ft", "FEET": "ft",
}
_UNIT_TO_MM = {"mm": 1.0, "inch": 25.4, "m": 1000.0, "cm": 10.0, "ft": 304.8}


@dataclass
class Part:
    ref: str            # unique in the model: label, suffixed _2, _3 ... only when two
                         # DIFFERENT shapes share a label
    label: str           # XCAF name of the part's referred label; "" -> "Part<n>"
    shape: TopoDS_Shape  # first occurrence, located in model coordinates, mm
    locations: list      # gp_Trsf per occurrence (model coordinates)
    n_solids: int        # solids in ONE occurrence
    _raw: object = field(default=None, repr=False, compare=False)
    # unlocated, post-healing shape (mm); internal, used by all_shapes()
    material: str = ""   # XCAF material name from the STEP file; "" when the file has none

    @property
    def qty(self):
        return len(self.locations)


@dataclass
class Model:
    path: str
    name: str            # root label name (document name); file stem when there is none
    source_units: str    # "mm", "inch", "m", "cm", "ft" ... as declared in the file
    parts: list
    valid: bool           # BRepCheck_Analyzer valid for every part after healing
    healed: list          # refs healed
    log: list              # human-readable lines (units, healing, timing)
    seconds: float

    @property
    def n_solids(self):
        return sum(p.qty * p.n_solids for p in self.parts)


def _label_name(label):
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        return attr.Get().ToExtString()
    return ""


def _label_material(label):
    """Material name the STEP file attached to `label` (XCAF material reference,
    read with SetMatMode); "" when there is none. Never raises. IsAttribute guards
    each FindAttribute: OCP crashes on FindAttribute for an attribute the label lacks."""
    try:
        if not label.IsAttribute(XCAFDoc.MaterialRefGUID_s()):
            return ""
        tn = TDataStd_TreeNode()
        if not label.FindAttribute(XCAFDoc.MaterialRefGUID_s(), tn) or not tn.HasFather():
            return ""
        mlabel = tn.Father().Label()
        if not mlabel.IsAttribute(XCAFDoc_Material.GetID_s()):
            return ""
        ma = XCAFDoc_Material()
        if not mlabel.FindAttribute(XCAFDoc_Material.GetID_s(), ma):
            return ""
        name = ma.GetName()
        return name.ToCString().strip() if name is not None else ""
    except Exception:
        return ""


def _entry(label):
    e = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, e)
    return e.ToCString()


def load(path: str, heal: bool = True) -> "Model":
    """Read a STEP file through XCAF: names, assembly tree, repeated placements ->
    qty, units -> mm (with a log line when the file wasn't already mm), optional
    ShapeFix healing. A part is a non-assembly label reached from the free shapes
    (the root assembly itself, when there is exactly one, is never a part -- its name
    becomes Model.name)."""
    t0 = time.time()
    Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)

    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    reader.SetMatMode(True)
    status = reader.ReadFile(path)
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise IOError("kernel.io.load: failed to read %s (status %s)" % (path, status))
    if not reader.Transfer(doc):
        raise IOError("kernel.io.load: failed to transfer %s into the XCAF document" % path)

    log = []
    length_units = TColStd_SequenceOfAsciiString()
    angle_units = TColStd_SequenceOfAsciiString()
    solidangle_units = TColStd_SequenceOfAsciiString()
    reader.Reader().FileUnits(length_units, angle_units, solidangle_units)
    raw_unit = length_units.Value(1).ToCString() if length_units.Length() > 0 else "MM"
    source_units = _UNIT_MAP.get(raw_unit.upper(), raw_unit.lower())
    if source_units != "mm":
        factor = _UNIT_TO_MM.get(source_units)
        if factor:
            log.append("units: %s in file -> converted to mm (x%s)" % (raw_unit, factor))
        else:
            log.append("units: %s in file -> converted to mm" % raw_unit)

    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    free_seq = TDF_LabelSequence()
    shape_tool.GetFreeShapes(free_seq)
    free = [free_seq.Value(i) for i in range(1, free_seq.Length() + 1)]

    order = {}    # entry -> 1-based ordinal (order of first occurrence)
    parts = {}    # entry -> {"name", "raw_shape", "locations": [gp_Trsf], "material"}

    def add_occurrence(part_label, loc, comp_label=None):
        entry = _entry(part_label)
        if entry not in parts:
            raw_shape = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main()).GetShape_s(part_label)
            if raw_shape is None or raw_shape.IsNull():
                log.append("skipped %s: no solid" % (_label_name(part_label) or entry))
                return
            solid_map = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(raw_shape, TopAbs_SOLID, solid_map)
            if solid_map.Extent() == 0:
                log.append("skipped %s: no solid" % (_label_name(part_label) or entry))
                return
            order[entry] = len(order) + 1
            material = _label_material(part_label)
            if not material and comp_label is not None:
                material = _label_material(comp_label)
            parts[entry] = {"name": _label_name(part_label), "raw_shape": raw_shape, "locations": [],
                            "material": material}
        parts[entry]["locations"].append(loc.Transformation())

    def walk(node_label, parent_loc):
        comps = TDF_LabelSequence()
        shape_tool.GetComponents_s(node_label, comps)
        for i in range(1, comps.Length() + 1):
            comp = comps.Value(i)
            comp_loc = shape_tool.GetLocation_s(comp)
            combined = parent_loc.Multiplied(comp_loc)
            referred = TDF_Label()
            if not shape_tool.GetReferredShape_s(comp, referred):
                continue
            # A compound part's own solids can ALSO be catalogued as their own
            # top-level parts in the same document (e.g. a STEP file whose top-level
            # parts sit in one document: a 13-solid compound whose 13 solids are each
            # a part too gives 14 parts, not 1). No dedup:
            # every non-assembly referred label reached from the tree is a part.
            if shape_tool.IsAssembly_s(referred):
                walk(referred, combined)
            else:
                add_occurrence(referred, combined, comp)

    identity = TopLoc_Location()
    model_name = None
    if len(free) == 1 and shape_tool.IsAssembly_s(free[0]):
        root = free[0]
        model_name = _label_name(root)
        walk(root, identity)
    else:
        for lbl in free:
            if shape_tool.IsAssembly_s(lbl):
                walk(lbl, identity)
            else:
                add_occurrence(lbl, identity)

    if not model_name:
        model_name = os.path.splitext(os.path.basename(path))[0]

    healed = []
    result_parts = []
    all_valid = True
    name_counts = {}
    for entry, info in parts.items():
        raw_shape = info["raw_shape"]
        ref_name = info["name"] or ("Part%d" % order[entry])
        valid = BRepCheck_Analyzer(raw_shape).IsValid()
        if not valid and heal:
            sf = ShapeFix_Shape()
            sf.Init(raw_shape)
            sf.SetPrecision(1e-7)
            sf.SetMinTolerance(1e-7)
            sf.SetMaxTolerance(1e-7)
            # Real SolidWorks exports carry the occasional self-intersecting wire
            # (e.g. the second solid of one test file); ShapeFix_Shape's wire sub-tool
            # leaves these modes off (-1, "use its own default") unless asked.
            wire_tool = sf.FixWireTool()
            wire_tool.FixSelfIntersectingEdgeMode = 1
            wire_tool.FixIntersectingEdgesMode = 1
            wire_tool.FixNonAdjacentIntersectingEdgesMode = 1
            wire_tool.ModifyTopologyMode = 1
            sf.Perform()
            fixed = sf.Shape()
            if BRepCheck_Analyzer(fixed).IsValid():
                raw_shape, valid = fixed, True
                healed.append(ref_name)
                log.append("healed %s: invalid -> valid (precision-fix)" % ref_name)
            else:
                sf2 = ShapeFix_Shape()
                sf2.Init(info["raw_shape"])
                sf2.Perform()
                fixed2 = sf2.Shape()
                if BRepCheck_Analyzer(fixed2).IsValid():
                    raw_shape, valid = fixed2, True
                    healed.append(ref_name)
                    log.append("healed %s: invalid -> valid (default-fix)" % ref_name)
                else:
                    log.append("heal failed %s" % ref_name)
        if not valid:
            all_valid = False

        name_counts[ref_name] = name_counts.get(ref_name, 0) + 1
        ref = ref_name if name_counts[ref_name] == 1 else "%s_%d" % (ref_name, name_counts[ref_name])

        solid_map = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(raw_shape, TopAbs_SOLID, solid_map)
        n_solids = solid_map.Extent()

        first_loc = TopLoc_Location(info["locations"][0])
        located_shape = raw_shape.Moved(first_loc)

        part = Part(ref=ref, label=ref_name, shape=located_shape,
                    locations=list(info["locations"]), n_solids=n_solids, material=info["material"])
        part._raw = raw_shape
        result_parts.append(part)

    seconds = time.time() - t0
    total_solids = sum(p.qty * p.n_solids for p in result_parts)
    log.append("read %s: %d parts, %d solids, %.3f s" % (
        os.path.basename(path), len(result_parts), total_solids, seconds))

    return Model(path=path, name=model_name, source_units=source_units, parts=result_parts,
                 valid=all_valid, healed=healed, log=log, seconds=seconds)


def all_shapes(model):
    """One (ref, located shape) per occurrence -- expands qty > 1 parts."""
    out = []
    for p in model.parts:
        raw = p._raw
        for trsf in p.locations:
            out.append((p.ref, raw.Moved(TopLoc_Location(trsf))))
    return out


def placed(shape: TopoDS_Shape, matrix: list) -> TopoDS_Shape:
    """Apply a 4x4 row-major placement matrix (mm) to `shape`, returning a new,
    transformed shape (BRepBuilderAPI_Transform with copy=True -- the original
    shape is never mutated, so the same raw shape can be placed more than once).
    `matrix` is a plain list of 4 rows of 4 floats each (the last row is always
    [0, 0, 0, 1] for an affine transform); only the first 3 rows are used. Used
    by framework.kernel.shapes.product_parts for a manifest entry that carries
    its own "placement" on top of the part's own model-space shape."""
    trsf = gp_Trsf()
    trsf.SetValues(
        matrix[0][0], matrix[0][1], matrix[0][2], matrix[0][3],
        matrix[1][0], matrix[1][1], matrix[1][2], matrix[1][3],
        matrix[2][0], matrix[2][1], matrix[2][2], matrix[2][3],
    )
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


def write_step(parts, out_path: str, root_name: str) -> str:
    """Write one labelled AP214/mm STEP file from (name, TopoDS_Shape) pairs --
    one labelled product per pair."""
    Interface_Static.SetCVal_s("write.step.schema", "AP214IS")
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    children = []
    for name, shape in parts:
        s = build123d.Shape.cast(shape)
        s.label = name
        children.append(s)
    root = Compound(children=children)
    root.label = root_name
    export_step(root, out_path, unit=Unit.MM)
    return out_path
