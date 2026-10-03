import sqlite3
import tempfile
import unittest
from pathlib import Path
from aeon_sharedwrites.migrate import migrate

class Migration(unittest.TestCase):
    def test_explicit_migration_preserves_items_and_can_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'synthetic.sqlite'
            db=sqlite3.connect(path)
            db.executescript(Path(__file__).with_name('base_schema_fixture.sql').read_text())
            db.execute("INSERT INTO memory_items(id,type,domain,captured_at) VALUES('synthetic','note','work',1)")
            db.commit();db.close()
            migrate(path);migrate(path)
            db=sqlite3.connect(path)
            self.assertEqual(db.execute('SELECT count(*) FROM memory_items').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM memory_write_requests').fetchone()[0],0)
            db.close()

    def test_wrong_database_is_refused_without_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'wrong.sqlite'
            sqlite3.connect(path).close()
            with self.assertRaises(sqlite3.Error):migrate(path)
            db=sqlite3.connect(path)
            self.assertEqual(db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0],0)
            db.close()

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'missing.sqlite'
            with self.assertRaises(FileNotFoundError):migrate(path)
            self.assertFalse(path.exists())
