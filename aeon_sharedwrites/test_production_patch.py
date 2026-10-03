"""Exercise actual staged production definitions, with synthetic storage/API stubs."""
import ast
import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT=Path(__file__).parent
PATCHED=ROOT.parent

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module
    spec.loader.exec_module(module);return module

package=types.ModuleType('fixture_store');package.__path__=[str(PATCHED/'store')]
sys.modules['fixture_store']=package
utils=load('fixture_store.utils',PATCHED/'store/utils.py')
backend=load('fixture_store.db',PATCHED/'store/db.py')
writer=load('fixture_store.shared_writer',PATCHED/'store/shared_writer.py')
queries=load('fixture_store.queries',PATCHED/'store/queries.py')

class ProductionPatch(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();path=str(Path(self.temp.name)/'synthetic.sqlite')
        # Do not instantiate AeonDB: its constructor can consult remote config.
        self.db=object.__new__(backend.AeonDB)
        self.db._turso_url=self.db._turso_token=None
        self.db.has_vector=False
        if backend.HAS_LIBSQL:
            self.db._conn=backend.libsql.connect(path)
        else:self.db._conn=sqlite3.connect(path)
        for file in ['base_schema_fixture.sql','migration.sql']:
            connection=self.db._conn
            sql=(ROOT/file).read_text()
            if hasattr(connection,'executescript'):connection.executescript(sql)
            else:
                for statement in sql.split(';'):
                    if statement.strip():connection.execute(statement)
                connection.commit()
    def tearDown(self):self.db.close();self.temp.cleanup()

    def test_installed_backend_actual_query_wrapper_and_read_revision(self):
        mid=queries.capture_memory(self.db,type='note',domain='work',content='Synthetic project direction',source='manual',request_id='capture1')
        item=queries.search_memories(self.db,query='Synthetic')[0]
        self.assertEqual(item.to_dict()['current_revision'],1)
        self.assertEqual(queries.update_memory_content(self.db,memory_id=mid,expected_revision=1,content='Synthetic revised direction',summary=None,source='manual',request_id='update1'),2)
        with self.assertRaises(writer.Conflict):
            queries.update_memory_content(self.db,memory_id=mid,expected_revision=1,content='stale',summary=None,source='manual')
        self.assertEqual(queries.search_memories(self.db,query='revised')[0].current_revision,2)

    def test_legacy_title_only_capture_keeps_compatibility(self):
        mid=queries.capture_memory(self.db,type='task',domain='work',title='Synthetic task',source='manual')
        self.assertIsInstance(mid,str)

    def test_hermes_large_capture_update_and_idempotency_keep_compatibility(self):
        content='Synthetic extracted page '+('x'*1000001)
        title='t'*301;summary='s'*2001;tags=['tag'+str(i) for i in range(101)]+['z'*129]
        args=dict(type='link',domain='learning',title=title,summary=summary,content=content,
                  tags=tags,source='manual',request_id='large')
        mid=queries.capture_memory(self.db,**args)
        self.assertEqual(mid,queries.capture_memory(self.db,**args))
        self.assertEqual(self.db.execute('SELECT title,summary,content FROM memory_items WHERE id=?',(mid,)).fetchone(),(title,summary,content))
        corrected=content+' corrected'
        self.assertEqual(queries.update_memory_content(self.db,memory_id=mid,expected_revision=1,content=corrected,summary=summary,source='manual',request_id='large-update'),2)
        self.assertEqual(self.db.execute('SELECT content FROM memory_items WHERE id=?',(mid,)).fetchone()[0],corrected)

    def test_actual_provider_schema_handler_conflict(self):
        tree=ast.parse((PATCHED/'provider.py').read_text())
        schema=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='UPDATE_SCHEMA' for t in n.targets))
        namespace={};exec(compile(ast.Module(body=[schema],type_ignores=[]),'schema','exec'),namespace)
        self.assertIn('expected_revision',namespace['UPDATE_SCHEMA']['parameters']['required'])
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_handle_update')
        # Substitute only the relative import; execute the real handler body.
        method.body=[n for n in method.body if not isinstance(n,ast.ImportFrom)]
        namespace=dict(Conflict=writer.Conflict,WriteError=writer.WriteError,q=queries,
            embed_text=lambda *a,**k:(None,None),tool_result=lambda **k:k,tool_error=lambda value:dict(error=value))
        exec(compile(ast.Module(body=[method],type_ignores=[]),'handler','exec'),namespace)
        target=types.SimpleNamespace(_db=self.db,_embed_provider='synthetic')
        mid=queries.capture_memory(self.db,type='note',domain='work',content='Synthetic',source='manual')
        args=dict(memory_id=mid,content='Correction',expected_revision=1)
        self.assertEqual(namespace['_handle_update'](target,args)['revision'],2)
        self.assertEqual(namespace['_handle_update'](target,args)['current_revision'],2)

    def test_actual_profile_upsert_passes_revision(self):
        tree=ast.parse((PATCHED/'ingest/_common.py').read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='upsert_profile')
        function.body=[n for n in function.body if not isinstance(n,ast.ImportFrom)]
        namespace=dict(q=queries,embed_text=lambda *a,**k:(None,None),os=types.SimpleNamespace(environ={}),PROFILE_DEDUP_KEY='profile:synthetic',Optional=__import__('typing').Optional)
        exec(compile(ast.Module(body=[function],type_ignores=[]),'profile','exec'),namespace)
        mid=namespace['upsert_profile'](self.db,'Synthetic profile')
        self.assertEqual(namespace['upsert_profile'](self.db,'Synthetic corrected profile'),mid)
        self.assertEqual(self.db.execute('SELECT current_revision FROM memory_items WHERE id=?',(mid,)).fetchone()[0],2)

if __name__=='__main__':unittest.main()
