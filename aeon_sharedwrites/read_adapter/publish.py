#!/usr/bin/env python3
"""Publish a source-filtered snapshot without modifying the canonical store."""
import argparse
from importlib import import_module
import json
import sqlite3
from pathlib import Path
if __package__:
    from . import adapter
else:
    import adapter

projection_publisher = import_module(adapter.projection.__name__ + ".publish")

SOURCE_FIELDS = adapter.POLICY.source_fields

def publish(source, output, reader_group=None):
    return projection_publisher.publish(source, output, reader_group,
        policy=adapter.POLICY, schema=Path(__file__).with_name('projection_schema.sql'))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--reader-group', help='Explicit approved read-only group grant; root only')
    args = parser.parse_args()
    try:
        result = publish(args.source, args.output, args.reader_group)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        raise SystemExit('Projection refresh failed; previous projection retained; source unchanged')
    print(json.dumps(result))

if __name__ == '__main__':
    main()
