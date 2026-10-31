"""Compatibility import; implementation lives in app.composition.container."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module("app.composition.container")
