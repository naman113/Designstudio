"""framework.kernel is the only package that imports the CAD kernel (OCP/build123d);
plain functions, no shelling out to another process, no env-var argv.
"""
import build123d as _build123d

__all__ = ["io", "measure", "features", "KERNEL_VERSION"]

KERNEL_VERSION = "build123d " + getattr(_build123d, "__version__", "unknown")
