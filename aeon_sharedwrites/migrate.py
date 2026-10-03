"""Explicit additive migration; never invoked by an import or write request."""
import argparse
import sqlite3
from pathlib import Path

def migrate(path):
    path=Path(path).resolve(strict=True)
    connection=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=1)
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('SELECT id,current_revision FROM memory_items LIMIT 0')
        sql=Path(__file__).with_name('migration.sql').read_text()
        for statement in sql.split(';'):
            if statement.strip():connection.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:connection.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True)
    args=parser.parse_args()
    migrate(args.db)
    print('Shared-write schema prepared')
