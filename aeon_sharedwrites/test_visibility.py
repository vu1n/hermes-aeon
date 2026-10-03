"""Only synthetic notes; source and projection both live in temporary directories."""
import importlib.util
import json
import sqlite3
import os
import sys
import threading
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from aeon_sharedwrites import broker, mcp_adapter, shared_writer

spec=importlib.util.spec_from_file_location('shared_projection_publish',Path(__file__).with_name('read_adapter')/'publish.py')
publisher=importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)

class Visibility(unittest.TestCase):
    @unittest.skipUnless(sys.platform=='linux','Linux Unix peer identity')
    def test_real_mcp_client_capture_and_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source.sqlite';output=Path(temp)/'projection.sqlite';sock=Path(temp)/'broker.sock'
            db=sqlite3.connect(source,isolation_level=None)
            db.executescript(Path(__file__).with_name('base_schema_fixture.sql').read_text())
            db.executescript(Path(__file__).with_name('migration.sql').read_text());db.close()
            publisher.publish(source,output)
            server=broker.Server(str(sock),source,{os.getuid():'dot'})
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            store=mcp_adapter.Store(output,str(sock))
            try:
                args=dict(request_id='client1',domain='work',kind='idea',attribution_basis='assistant_inferred',statement='Synthetic MCP project note')
                captured=store.call('aeon_capture',args)
                self.assertTrue(captured['ok'])
                self.assertEqual(captured,store.call('aeon_capture',args))
                mid=captured['result']['memory_id']
                correction=dict(request_id='client2',memory_id=mid,expected_revision=1,kind='decision',attribution_basis='user_corrected',statement='Synthetic MCP decision')
                self.assertTrue(store.call('aeon_correct',correction)['ok'])
                correction['request_id']='client3'
                stale=store.call('aeon_correct',correction)
                self.assertFalse(stale['ok']);self.assertEqual(stale['error']['current_revision'],2)
                publisher.publish(source,output)
                self.assertEqual(store.call('aeon_get',{'id':mid})['item']['current_revision'],2)
            finally:
                store.db.close();server.shutdown();server.server_close();worker.join()

    def test_legacy_symlink_uses_one_wal_store(self):
        with tempfile.TemporaryDirectory() as temp:
            canonical=Path(temp)/'canonical';legacy=Path(temp)/'legacy'
            canonical.mkdir();legacy.mkdir()
            source=canonical/'aeon.db';link=legacy/'aeon.db'
            direct=sqlite3.connect(source,isolation_level=None)
            direct.execute('PRAGMA journal_mode=WAL')
            direct.execute('CREATE TABLE check_shared(value TEXT)')
            link.symlink_to(source)
            old=sqlite3.connect(link,isolation_level=None)
            old.execute("INSERT INTO check_shared VALUES('synthetic')")
            self.assertEqual(direct.execute('SELECT value FROM check_shared').fetchone()[0],'synthetic')
            self.assertTrue(Path(str(source)+'-wal').exists())
            self.assertFalse(Path(str(link)+'-wal').exists())
            old.close();direct.close()

    def test_canonical_note_correction_and_read_visibility(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source.sqlite'; output=Path(temp)/'projection.sqlite'
            db=sqlite3.connect(source,isolation_level=None)
            db.executescript(Path(__file__).with_name('base_schema_fixture.sql').read_text())
            db.executescript(Path(__file__).with_name('migration.sql').read_text())
            args=dict(request_id='message1-note1',domain='work',kind='idea',attribution_basis='assistant_inferred',conversation_ref='synthetic-room',statement='Consider a smaller project onboarding flow')
            result=broker.operation(db,'dot',dict(operation='capture',arguments=args))
            mid=result['memory_id']
            self.assertEqual(db.execute('SELECT content FROM memory_items WHERE id=?',(mid,)).fetchone()[0],args['statement'])
            publisher.publish(source,output)
            store=mcp_adapter.reader.Store(output)
            first=store.call('aeon_get',{'id':mid})['item']
            self.assertEqual(first['provenance']['attribution_basis'],'assistant_inferred')
            update=dict(request_id='message2-note1',memory_id=mid,expected_revision=1,kind='decision',attribution_basis='user_corrected',statement='Use the smaller onboarding flow')
            broker.operation(db,'dot',dict(operation='update',arguments=update))
            publisher.publish(source,output)
            corrected=store.call('aeon_get',{'id':mid})['item']
            self.assertEqual(corrected['current_revision'],2)
            self.assertEqual(corrected['provenance']['kind'],'decision')
            self.assertEqual(corrected['provenance']['attribution_basis'],'user_corrected')
            self.assertEqual(db.execute('SELECT count(*) FROM memory_revisions WHERE memory_id=?',(mid,)).fetchone()[0],2)
            self.assertEqual(store.call('aeon_search',{'query':'smaller onboarding'})['count'],1)
            # Hermes can revise the same canonical note; original capture source
            # stays chat:dot while the current revision identifies Hermes.
            shared_writer.update(db,consumer_id='hermes',request_id='hermes-correction',memory_id=mid,expected_revision=2,content='Shared project clarification',source='manual')
            publisher.publish(source,output)
            revised=store.call('aeon_get',{'id':mid})['item']
            self.assertEqual(revised['source'],'chat:dot')
            self.assertEqual(revised['provenance']['consumer_id'],'hermes')
            self.assertEqual(revised['current_revision'],3)
            store.db.close();db.close()

    def test_unapproved_consumer_and_missing_audit_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source.sqlite';output=Path(temp)/'projection.sqlite'
            db=sqlite3.connect(source,isolation_level=None)
            db.executescript(Path(__file__).with_name('base_schema_fixture.sql').read_text())
            db.executescript(Path(__file__).with_name('migration.sql').read_text())
            for consumer in ['dot','unapproved']:
                shared_writer.capture(db,consumer_id=consumer,request_id='r1',type='note',domain='work',source='chat:'+consumer,content='Project scope',entry_kind='idea',attribution_basis='user_explicit')
            db.execute('DELETE FROM memory_write_audit WHERE consumer_id=?',('dot',))
            self.assertEqual(publisher.publish(source,output)['accepted'],0)
            db.close()

    def test_mcp_schemas_require_identity_and_revision(self):
        tools={t['name']:t for t in mcp_adapter.tools()}
        self.assertEqual(len(tools),5)
        self.assertIn('expected_revision',tools['aeon_correct']['inputSchema']['required'])
        self.assertIn('request_id',tools['aeon_capture']['inputSchema']['required'])
        self.assertNotIn('consumer_id',tools['aeon_capture']['inputSchema']['properties'])

if __name__=='__main__':unittest.main()
