"""Compatibility import path. Implementation: s1.parking.logic."""
from s1._compat import export_module as _export_module

_export_module("s1.parking.logic", globals())
del _export_module
