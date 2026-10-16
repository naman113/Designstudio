"""The names build123d's Mesher reads at import time; 3MF export itself is not available in this build."""

import enum


class ModelUnit(enum.IntEnum):
    MicroMeter = 0
    MilliMeter = 1
    CentiMeter = 2
    Inch = 3
    Foot = 4
    Meter = 5


class ObjectType(enum.IntEnum):
    Other = 0
    Model = 1
    Support = 2
    SolidSupport = 3


class Wrapper:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("3MF export is not available in this build")


class MeshObject:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("3MF export is not available in this build")
