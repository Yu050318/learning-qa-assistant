"""Compatibility import; implementation lives in app.service.evaluation_judge."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module("app.service.evaluation_judge")
