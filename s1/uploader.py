"""Compatibility import path. Implementation: s1.publishing.publisher."""
from s1._compat import export_module as _export_module

_export_module("s1.publishing.publisher", globals())
del _export_module
