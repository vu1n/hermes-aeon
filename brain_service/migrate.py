"""Explicit, transactional preparation. Never run on import or a request."""
import argparse
import sqlite3
from pathlib import Path

def migrate(path):
    path=Path(path).resolve(strict=True)
    db=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=1)
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('SELECT id,current_revision FROM memory_items LIMIT 0')
        db.execute('SELECT request_sha256 FROM memory_write_requests LIMIT 0')
        for statement in Path(__file__).with_name('migration.sql').read_text().split(';'):
            if statement.strip():db.execute(statement)
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--db',required=True)
    migrate(parser.parse_args().db)
    print('General foundation schema prepared; no legacy admission performed')
