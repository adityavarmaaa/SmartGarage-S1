"""Compatibility import path. Implementation: s1.reporting.report."""
from s1._compat import export_module as _export_module

_export_module("s1.reporting.report", globals())
del _export_module
