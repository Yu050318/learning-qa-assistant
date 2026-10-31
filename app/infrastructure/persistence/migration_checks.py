"""Compatibility import; implementation lives in app.dao.postgres.migration_checks."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module("app.dao.postgres.migration_checks")
