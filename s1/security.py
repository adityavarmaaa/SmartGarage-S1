"""Compatibility import path. Implementation: s1.common.security."""
from s1._compat import export_module as _export_module

_export_module("s1.common.security", globals())
del _export_module
