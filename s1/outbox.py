"""Compatibility import path. Implementation: s1.publishing.outbox."""
from s1._compat import export_module as _export_module

_export_module("s1.publishing.outbox", globals())
del _export_module
