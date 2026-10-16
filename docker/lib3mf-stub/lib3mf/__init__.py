"""Stand-in for the lib3mf package inside the Linux image.

build123d imports lib3mf when it is imported (`from lib3mf import Lib3MF` in build123d/mesher.py), but Buildcore never
exports 3MF. The real lib3mf 2.5.0 has no Linux aarch64 wheel and no source package, so the image installs everything
from the lock except lib3mf and puts this folder in its place. It provides only the names build123d reads at import time;
building a 3MF Mesher raises an error.
"""
