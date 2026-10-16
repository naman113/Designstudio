"""The drawing package: the sheets the geometry service draws for a design, on the CPU.

    extents   tight extents (BRepBndLib.AddOptimal): the sizes every sheet prints
    tess      the kernel's own triangles and B-rep edge segments (no 3D-view file, no GPU)
    raster    the shaded views (scanline z-buffer in numpy, CAD edges, outlines)
    views     the views sheet: layout, captions, title, and the record of every printed number
    words     the word guard for sheet text
    check     the read-back check: every printed text recomputed from the kernel's extents
    fonts/    the pinned lettering font (osifont, shipped in the package; never a system font)

Imported only by geometry worker processes (framework/kernel/ops.py). OCP and build123d appear only here and in the
rest of framework/kernel. The views sheet is made on the CPU in the geometry service; sheets print tight sizes, and the
catalogue's box is not changed.
"""
