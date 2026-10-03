"""Policy equivalence and isolated-install contracts use only synthetic records."""
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aeon_readonly import adapter as research, publish as research_publish
from aeon_sharedwrites import broker, mcp_adapter, shared_writer
from aeon_sharedwrites.read_adapter import adapter as shared, publish as shared_publish
from aeon_readonly.projection import RESEARCH, SHARED, screen, provenance_json

ROOT = Path(__file__).resolve().parent.parent

class ProjectionEquivalence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.sqlite'
        self.db = sqlite3.connect(self.source, isolation_level=None)
        for name in ['base_schema_fixture.sql', 'migration.sql']:
            self.db.executescript((ROOT / 'aeon_sharedwrites' / name).read_text())
        self.now = int(time.time()*1000)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def capture(self, request, source='discover:hn', **extra):
        args = dict(consumer_id='hermes', request_id=request, type='note', domain='work',
                    content='Synthetic project discovery', title='Project', source=source)
        args.update(extra)
        return shared_writer.capture(self.db, **args)['memory_id']

    def row(self, mid, policy):
        raw = self.db.execute('SELECT '+','.join(policy.source_fields)+' FROM memory_items WHERE id=?', (mid,)).fetchone()
        row = dict(zip(policy.source_fields, raw))
        row.update(scope_version=policy.scope, screened_at=self.now)
        if policy.chat_provenance:
            audit = self.db.execute('SELECT consumer_id,entry_kind,attribution_basis,conversation_ref,created_at FROM memory_write_audit WHERE memory_id=? AND revision_n=?', (mid,row['current_revision'])).fetchone()
            row['provenance_json'] = provenance_json(audit, row['current_revision']) if audit else None
        return row

    def test_shared_research_output_matches_legacy_without_added_provenance(self):
        mid = self.capture('research', content='Synthetic project discovery '*800)
        for full in [False, True]:
            old = research.Store.safe(self.row(mid,RESEARCH),full=full)
            new = shared.Store.safe(self.row(mid,SHARED),full=full)
            self.assertIsNone(new.pop('provenance'))
            self.assertEqual(old,new)
        for change in [dict(domain='health'),dict(status='deleted'),dict(type='contact'),
                       dict(source='manual'),dict(content='A password credential'),
                       dict(url='https://github.com/example?token=dummy'),dict(current_revision=0),
                       dict(screened_at=0),dict(id='invalid'),dict(content='x'*100001)]:
            with self.subTest(change=next(iter(change))):
                self.assertIsNone(screen(self.row(mid,RESEARCH)|change,RESEARCH))
                self.assertIsNone(screen(self.row(mid,SHARED)|change,SHARED))

    def test_publisher_policies_and_read_results_stay_distinct(self):
        mid = self.capture('research')
        chat = self.capture('chat',source='chat:dot')
        self.capture('excluded',domain='health')
        legacy = self.root/'research.sqlite';current = self.root/'shared.sqlite'
        with patch('aeon_readonly.projection.publish.time.time',return_value=self.now/1000):
            a = research_publish.publish(self.source,legacy);b = shared_publish.publish(self.source,current)
        self.assertEqual(a['accepted'],1);self.assertEqual(b['accepted'],2)
        readers = [research.Store(legacy),shared.Store(current)]
        try:
            old,new = readers
            for tool,args in [('aeon_get',dict(id=mid)),('aeon_search',dict(query='discovery')),('aeon_recent',{})]:
                first=old.call(tool,args);second=new.call(tool,args)
                if tool=='aeon_get':
                    self.assertIsNone(second['item'].pop('provenance'));self.assertEqual(first,second)
                else:
                    common=next(item for item in second['items'] if item['id']==mid)
                    self.assertIsNone(common.pop('provenance'));self.assertEqual(first['items'],[common])
            self.assertIsNone(old.call('aeon_get',dict(id=chat))['item'])
            self.assertEqual(new.call('aeon_get',dict(id=chat))['item']['provenance']['consumer_id'],'hermes')
            with self.assertRaises(research.Invalid):research.Store(current)
            with self.assertRaises(shared.Invalid):shared.Store(legacy)
        finally:
            for reader in readers:reader.db.close()

    def test_reader_scope_is_class_owned_and_shared_refresh_keeps_broker_socket(self):
        self.capture('research')
        legacy=self.root/'research.sqlite';current=self.root/'shared.sqlite'
        research_publish.publish(self.source,legacy);shared_publish.publish(self.source,current)
        with self.assertRaises(TypeError):research.Store(current,policy=SHARED)
        store=mcp_adapter.Store(current,'synthetic-broker.sock')
        try:
            self.capture('new',content='Synthetic new project direction')
            shared_publish.publish(self.source,current)
            self.assertEqual(store.call('aeon_search',dict(query='direction'))['count'],1)
            self.assertEqual(store.broker_socket,'synthetic-broker.sock')
            self.assertIs(store.policy,SHARED)
        finally:store.db.close()

    def test_current_revision_provenance_and_immediate_screening_match(self):
        mid=self.capture('chat',source='chat:dot')
        args=dict(id=mid)
        with patch('aeon_sharedwrites.broker.time.time',return_value=self.now/1000):
            self.assertEqual(broker.current_note(self.db,'dot',args)['item'],shared.Store.safe(self.row(mid,SHARED),full=True))
        row=self.row(mid,SHARED)
        for provenance in [None,'{}',json.dumps(dict(consumer_id='other',revision=1,kind='idea',attribution_basis='user_explicit')),
                           json.dumps(dict(consumer_id='dot',revision=0,kind='idea',attribution_basis='user_explicit'))]:
            self.assertIsNone(screen(row|dict(provenance_json=provenance),SHARED,full=True))
        self.db.execute('DELETE FROM memory_write_audit WHERE memory_id=?',(mid,))
        self.assertEqual(broker.current_note(self.db,'dot',args),dict(item=None,owned=True))

    def test_mcp_tool_definitions_are_per_session_and_error_behavior_is_preserved(self):
        class Fake:
            def call(self,*args):return dict(ok=False,error=dict(code='revision_conflict'))
        frames=[dict(jsonrpc='2.0',id=1,method='initialize',params={}),dict(jsonrpc='2.0',id=2,method='tools/list'),
                dict(jsonrpc='2.0',id=3,method='tools/call',params=dict(name='aeon_correct',arguments={}))]
        raw=''.join(json.dumps(frame)+'\n' for frame in frames).encode()
        for reader,definitions,expected in [(shared,mcp_adapter.tools,5),(shared,shared.tools,3),(research,research.tools,3)]:
            output=io.StringIO();reader.serve(Fake(),io.BytesIO(raw),output,tool_definitions=definitions)
            values=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(values[1]['result']['tools']),expected)
            self.assertEqual(values[2]['result']['isError'],reader is shared)
        self.assertEqual(len(shared.tools()),3)

    def test_atomic_failure_and_permissions_apply_to_both_publishers(self):
        self.capture('research')
        for publisher in [research_publish,shared_publish]:
            output=self.root/(publisher.__name__+'.sqlite')
            publisher.publish(self.source,output)
            original=output.read_bytes()
            self.assertEqual(output.stat().st_mode & 0o777,0o600)
            with patch('aeon_readonly.projection.publish.os.replace',side_effect=OSError('synthetic failure')):
                with self.assertRaises(OSError):publisher.publish(self.source,output)
            self.assertEqual(output.read_bytes(),original)
            self.assertFalse(list(self.root.glob('.aeon-projection-*')))

    def test_standalone_installs_require_no_repository_or_canonical_access(self):
        self.capture('research')
        env=os.environ.copy();env.pop('PYTHONPATH',None);env['PYTHONDONTWRITEBYTECODE']='1'
        for mode in ['research','shared']:
            install=self.root/mode
            src=ROOT/('aeon_readonly' if mode=='research' else 'aeon_sharedwrites')
            shutil.copytree(src,install,ignore=shutil.ignore_patterns('__pycache__'))
            if mode=='shared':
                shutil.copytree(ROOT/'aeon_readonly/projection',install/'read_adapter/projection',ignore=shutil.ignore_patterns('__pycache__'))
            publisher=install/('publish.py' if mode=='research' else 'read_adapter/publish.py')
            output=self.root/(mode+'-standalone.sqlite')
            result=subprocess.run([sys.executable,'-B',str(publisher),'--source',str(self.source),'--output',str(output)],cwd=self.root,env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            adapter=install/('adapter.py' if mode=='research' else 'mcp_adapter.py')
            command=[sys.executable,'-B',str(adapter),'--db',str(output)]
            if mode=='shared':command+=['--broker-socket',str(self.root/'absent.sock')]
            frames=''.join(json.dumps(frame)+'\n' for frame in [dict(jsonrpc='2.0',id=1,method='initialize',params={}),dict(jsonrpc='2.0',id=2,method='tools/list')])
            result=subprocess.run(command,input=frames,cwd=self.root,env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            values=[json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(values[1]['result']['tools']),3 if mode=='research' else 5)
            if mode=='shared':
                # Broker's policy import must not pull in database/MCP reader machinery.
                result=subprocess.run([sys.executable,'-B','-c','import sys; from read_adapter import projection; assert not any(k.endswith("projection.adapter") for k in sys.modules)'],cwd=install,env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()
