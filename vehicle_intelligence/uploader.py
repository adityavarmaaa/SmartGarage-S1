"""Compatibility import path. Implementation: vehicle_intelligence.publishing.publisher."""
from vehicle_intelligence._compat import export_module as _export_module

_export_module("vehicle_intelligence.publishing.publisher", globals())
del _export_module
