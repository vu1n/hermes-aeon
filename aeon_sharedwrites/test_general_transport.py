"""Synthetic neutral clients and MCP share the live general gateway."""
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from brain_service.client import Client
from brain_service.gateway import Gateway
from brain_service.policy import Principal, Invalid, Unavailable
from brain_service.server import Server, bindings
from aeon_sharedwrites.mcp_adapter import GeneralStore, general_tools
from aeon_sharedwrites.read_adapter import adapter as reader

ROOT=Path(__file__).resolve().parent.parent
CAPS=frozenset({'read','capture','revise_own','retract_own','propose'})

class GeneralTransport(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'memory.sqlite'
        self.db=sqlite3.connect(self.path,isolation_level=None)
        for name in ['aeon_sharedwrites/base_schema_fixture.sql','aeon_sharedwrites/migration.sql','brain_service/migration.sql']:
            self.db.executescript((ROOT/name).read_text())
        self.principals={name:Principal(name,'local',CAPS,'chat:'+name) for name in ['planner','researcher']}
        self.gateway=Gateway(self.db,principals=self.principals,peer_bindings={101:'planner',102:'researcher'})
        self.store=GeneralStore('/unused-synthetic.sock')
        self.store.client.call=lambda operation,arguments:self.gateway.handle_peer(101,dict(api_version='brain.general.v1',operation=operation,arguments=arguments))
    def tearDown(self):self.db.close();self.temp.cleanup()
    def args(self):return dict(request_id='one',domain='work',kind='idea',attribution_basis='user_explicit',statement='Synthetic compiler project')

    def test_mcp_neutral_contract_live_revision_replay_and_retraction(self):
        receipt=self.store.call('aeon_capture',self.args())
        self.assertTrue(receipt['ok'],receipt)
        mid=receipt['result']['memory_id']
        self.assertEqual(receipt,self.store.call('aeon_capture',self.args()))
        item=self.store.call('aeon_get',{'id':mid})['result']['item']
        self.assertEqual(item['source'],'chat:planner')
        self.assertEqual(item['verification'],'client_asserted')
        change=dict(request_id='edit',memory_id=mid,expected_revision=1,reason='Synthetic correction',patch={'statement':'Updated compiler project'})
        self.assertTrue(self.store.call('revise',change)['ok'])
        stale=dict(change,request_id='stale')
        self.assertFalse(self.store.call('revise',stale)['ok'])
        self.assertEqual(receipt,self.store.call('aeon_capture',self.args()))
        self.assertTrue(self.store.call('retract',dict(request_id='withdraw',memory_id=mid,expected_revision=2,reason='Withdraw assertion'))['ok'])
        self.assertIsNone(self.store.call('aeon_get',{'id':mid})['result']['item'])
        self.assertEqual(self.store.call('aeon_search',{'query':'compiler'})['result']['count'],0)

    def test_second_agent_proposes_without_overwriting_and_cannot_forge_identity(self):
        mid=self.store.call('aeon_capture',self.args())['result']['memory_id']
        neutral=lambda op,args:self.gateway.handle_peer(102,dict(api_version='brain.general.v1',operation=op,arguments=args))
        self.assertIsNotNone(neutral('get',{'id':mid})['result']['item'])
        self.assertFalse(neutral('revise',dict(request_id='other-edit',memory_id=mid,expected_revision=1,reason='Correction',patch={'statement':'Replacement'}))['ok'])
        proposal=neutral('propose',dict(request_id='proposal',memory_id=mid,expected_revision=1,reason='Alternative',statement='A separate compiler approach'))
        self.assertTrue(proposal['ok'],proposal)
        self.assertEqual(neutral('get',{'id':mid})['result']['item']['revision'],1)
        self.assertEqual(neutral('get',{'id':proposal['result']['memory_id']})['result']['item']['provenance']['capturing_actor'],'researcher')
        self.assertFalse(neutral('capture',dict(self.args(),actor='planner'))['ok'])
        restricted=neutral('capture',dict(self.args(),request_id='restricted',statement='Synthetic health record'))
        self.assertEqual(restricted['result']['search_visibility'],'ineligible')
        self.assertIsNone(neutral('get',{'id':restricted['result']['memory_id']})['result']['item'])
        self.assertFalse(neutral('capture',dict(self.args(),domain='health'))['ok'])

    def test_mcp_logical_errors_and_advertised_operations(self):
        requests=[dict(jsonrpc='2.0',id=1,method='initialize'),dict(jsonrpc='2.0',id=2,method='tools/call',params={'name':'aeon_capture','arguments':dict(self.args(),domain='health')})]
        output=io.StringIO()
        reader.serve(self.store,io.BytesIO(('\n'.join(json.dumps(r) for r in requests)+'\n').encode()),output,tool_definitions=general_tools)
        self.assertTrue(json.loads(output.getvalue().splitlines()[1])['result']['isError'])
        self.assertEqual({t['name'] for t in general_tools()},{'aeon_capture','aeon_correct','aeon_get','aeon_search','aeon_recent','revise','propose','retract','status','interests','consolidate_preview','consolidate_stage'})

    def test_client_fail_closed_and_bounded(self):
        with self.assertRaises(Unavailable):Client('/missing-synthetic.sock').call('recent',{})
        with self.assertRaises(Invalid):Client('/missing-synthetic.sock').call('capture',{'statement':'x'*65536})
        with patch.object(self.store.client,'call',side_effect=Unavailable('offline')):
            with self.assertRaises(reader.Unavailable):self.store.call('aeon_recent',{})

    def test_neutral_entrypoints_without_hermes_imports(self):
        subprocess.run([sys.executable,'-c',
            "import sys; from brain_service.client import Client; from brain_service.server import Server; "
            "assert 'brain_service.hermes' not in sys.modules; "
            "assert not any(name.startswith(('hermes_agent','hermes_cli')) for name in sys.modules)"],
            cwd=ROOT,check=True,timeout=5)

    def test_binding_validation(self):
        path=Path(self.temp.name)/'bindings.json'
        entry=dict(uid=101,id='planner',source_namespace='chat:planner',capabilities=sorted(CAPS))
        path.write_text(json.dumps([entry]))
        principals,peers=bindings(path)
        self.assertEqual(peers,{101:'planner'})
        self.assertEqual(principals['planner'].capabilities,CAPS)
        for entries in [[entry,entry],[dict(entry,capabilities=['administrator'])],[dict(entry,uid=True)]]:
            path.write_text(json.dumps(entries))
            with self.assertRaises(Invalid):bindings(path)

    @unittest.skipUnless(hasattr(socket,'SO_PEERCRED'),'Linux peer credentials')
    def test_real_socket_neutral_client_and_mcp(self):
        endpoint=str(Path(self.temp.name)/'general.sock')
        with Server(endpoint,self.path,self.principals,{os.getuid():'researcher'}) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                client=Client(endpoint)
                result=client.call('capture',self.args())
                self.assertTrue(result['ok'],result)
                item=GeneralStore(endpoint).call('aeon_get',{'id':result['result']['memory_id']})['result']['item']
                self.assertEqual(item['provenance']['capturing_actor'],'researcher')
                mid=result['result']['memory_id']
                self.assertEqual(client.call('capture',self.args()),result)
                revision=dict(request_id='neutral-edit',memory_id=mid,expected_revision=1,
                              reason='Neutral agent correction',patch={'statement':'Revised synthetic compiler project'})
                revised=client.call('revise',revision)
                self.assertTrue(revised['ok'],revised)
                updated=GeneralStore(endpoint).call('aeon_get',{'id':mid})['result']['item']
                self.assertEqual(updated['revision'],2)
                self.assertEqual(updated['content'],'Revised synthetic compiler project')
                self.assertEqual(updated['source'],'chat:researcher')
                self.assertEqual(updated['provenance']['capturing_actor'],'researcher')
                stale=client.call('revise',dict(revision,request_id='neutral-stale'))
                self.assertFalse(stale['ok'])
                self.assertEqual(client.call('revise',revision),revised)

                source_refs=[dict(memory_id=mid,revision=2)]
                preview=client.call('consolidate_preview',{'source_refs':source_refs})
                self.assertTrue(preview['ok'],preview)
                # Existing neutral principal has no derive capability: preview is read-only.
                stage=client.call('consolidate_stage',dict(source_refs=source_refs,expected_candidate_id=preview['result']['candidate_id']))
                self.assertEqual(stage['error']['code'],'not_found_or_denied')
                self.assertEqual(os.stat(endpoint).st_mode&0o777,0o600)
                with self.assertRaises(OSError):Server(endpoint,self.path,self.principals,{os.getuid():'researcher'})
                # A durable large record must never emit a partial/oversized wire result.
                large=dict(self.args(),request_id='large',statement='z'*64000)
                large_result=client.call('capture',large)
                self.assertTrue(large_result['ok'],large_result)
                response=client.call('get',{'id':large_result['result']['memory_id']})
                if not response['ok']:self.assertEqual(response['error']['code'],'general_unavailable')
                server.peers={}
                self.assertEqual(client.call('recent',{})['error']['code'],'not_found_or_denied')
            finally:server.shutdown();thread.join()
