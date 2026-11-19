"""Compatibility import path. Implementation: s1.multicam.multicam_runner."""
from s1._compat import export_module as _export_module

_export_module("s1.multicam.multicam_runner", globals())
del _export_module
