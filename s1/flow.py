"""Compatibility import path. Implementation: s1.integration.flow."""
from s1._compat import export_module as _export_module

_export_module("s1.integration.flow", globals())
del _export_module
