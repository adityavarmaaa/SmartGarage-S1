"""Compatibility import path. Implementation: s1.video.demo_video."""
from s1._compat import export_module as _export_module

_export_module("s1.video.demo_video", globals())
del _export_module
