#!/usr/bin/env python3
"""Source-filtered projection entrypoint; never open the canonical database."""
import argparse
import sqlite3
import sys
if __package__:
    from . import projection
    from importlib import import_module
    projection_adapter = import_module(projection.__name__ + ".adapter")
else:
    import projection
    from projection import adapter as projection_adapter
CoreStore = projection_adapter.Store
Invalid, Unavailable = projection_adapter.Invalid, projection_adapter.Unavailable
arguments, bounded_int = projection_adapter.arguments, projection_adapter.bounded_int
tools, core_serve = projection_adapter.tools, projection_adapter.serve

POLICY = projection.SHARED
SCOPE, SOURCES, FIELDS = POLICY.scope, POLICY.sources, POLICY.fields
DOMAINS = projection.DOMAINS

class Store(CoreStore):
    policy = POLICY

def serve(store, stdin=sys.stdin.buffer, stdout=sys.stdout, *, tool_definitions=tools):
    return core_serve(store, stdin, stdout, tool_definitions=tool_definitions, logical_errors=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, help='Source-filtered projection, never the original Hermes DB')
    args = parser.parse_args()
    try:
        store = Store(args.db)
    except (OSError, Invalid, Unavailable, sqlite3.Error):
        print('Source-filtered projection unavailable; refusing to start', file=sys.stderr)
        return 1
    serve(store)
    return 0

if __name__ == '__main__':
    sys.exit(main())
