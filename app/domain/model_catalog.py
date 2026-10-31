"""Compatibility import; implementation lives in app.entity.bo.model_catalog."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module("app.entity.bo.model_catalog")
