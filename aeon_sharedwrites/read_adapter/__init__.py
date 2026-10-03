"""Projection entrypoints share the research core without importing its server for rules."""
try:
    from aeon_readonly import projection
except ModuleNotFoundError as error:
    if error.name != 'aeon_readonly':
        raise
    from . import projection
