import concurrent.futures
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from aeon_sharedwrites import shared_writer as w, broker, hermes_compat


class Writes(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'canonical.sqlite'
        c=sqlite3.connect(self.path,isolation_level=None)
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript(Path(__file__).with_name('base_schema_fixture.sql').read_text())
        c.executescript(Path(__file__).with_name('migration.sql').read_text());c.close()
    def tearDown(self):self.tmp.cleanup()
    def connect(self):return sqlite3.connect(self.path,timeout=.05,isolation_level=None)
    def capture(self,c,request='r1',consumer='dot',**extra):
        args=dict(consumer_id=consumer,request_id=request,type='note',domain='work',content='A passing idea for improving project discovery',source='chat:'+consumer,entry_kind='idea',attribution_basis='user_explicit',conversation_ref='thread-synthetic')
        args.update(extra);return w.capture(c,**args)
    def counts(self,c):return [c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['memory_items','memory_revisions','memory_fts','memory_write_audit','memory_write_requests']]

    def test_capture_idempotent_replay_and_attribution(self):
        c=self.connect();a=self.capture(c);b=self.capture(c);self.assertEqual(a,b)
        self.assertEqual(self.counts(c),[1]*5)
        self.assertEqual(c.execute('SELECT source,current_revision FROM memory_items').fetchone(),('chat:dot',1))
        self.assertEqual(c.execute('SELECT consumer_id,entry_kind,attribution_basis,conversation_ref FROM memory_write_audit').fetchone(),('dot','idea','user_explicit','thread-synthetic'));c.close()

    def test_idempotency_payload_change_rejected_without_mutation(self):
        c=self.connect();self.capture(c)
        with self.assertRaises(w.IdempotencyConflict):self.capture(c,content='Changed payload')
        self.assertEqual(self.counts(c),[1]*5);c.close()

    def test_request_ids_scoped_to_consumer(self):
        c=self.connect();a=self.capture(c);b=self.capture(c,consumer='other')
        self.assertNotEqual(a['memory_id'],b['memory_id']);self.assertEqual(self.counts(c),[2]*5);c.close()

    def test_parallel_dedup_across_hermes_and_dot(self):
        def do(i):
            c=self.connect()
            try:return self.capture(c,request='p'+str(i),consumer='hermes' if i%2 else 'dot',dedup_key='url:stable')
            finally:c.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(do,range(8)))
        self.assertEqual(len({r['memory_id'] for r in results}),1)
        c=self.connect();self.assertEqual(self.counts(c),[1,1,1,1,8]);c.close()

    def test_update_expected_revision_history_and_replay(self):
        c=self.connect();mid=self.capture(c)['memory_id']
        kwargs=dict(consumer_id='dot',request_id='u1',memory_id=mid,expected_revision=1,content='Confirmed decision',summary='New summary',source='chat:dot',entry_kind='decision',attribution_basis='user_corrected',conversation_ref='thread-synthetic')
        result=w.update(c,**kwargs);self.assertEqual(result,w.update(c,**kwargs))
        self.assertEqual(result['revision'],2)
        self.assertEqual(c.execute('SELECT source,content,current_revision FROM memory_items').fetchone(),('chat:dot','Confirmed decision',2))
        self.assertEqual(c.execute('SELECT revision_n FROM memory_revisions ORDER BY revision_n').fetchall(),[(1,),(2,)])
        self.assertEqual(c.execute('SELECT entry_kind,attribution_basis FROM memory_write_audit ORDER BY revision_n').fetchall(),[('idea','user_explicit'),('decision','user_corrected')])
        self.assertEqual(c.execute('SELECT count(*) FROM memory_fts').fetchone()[0],1);c.close()

    def test_parallel_revision_conflict_no_silent_overwrite(self):
        c=self.connect();mid=self.capture(c)['memory_id'];c.close()
        def do(i):
            db=self.connect()
            try:return w.update(db,consumer_id='dot',request_id='u'+str(i),memory_id=mid,expected_revision=1,content='Decision '+str(i),source='chat:dot')
            except w.Conflict as e:return ('conflict',e.current_revision)
            finally:db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:r=list(pool.map(do,[1,2]))
        self.assertEqual(sum(isinstance(x,dict) for x in r),1);self.assertIn(('conflict',2),r)
        c=self.connect();self.assertEqual(self.counts(c),[1,2,1,2,2]);c.close()

    def test_capture_rollback_at_every_stage(self):
        for stage in ['after_item','after_revision','after_fts','after_embedding','after_audit','after_mutation','after_idempotency']:
            with self.subTest(stage=stage):
                c=self.connect()
                def fail(s):
                    if s==stage:raise RuntimeError('synthetic failure')
                with self.assertRaises(RuntimeError):self.capture(c,checkpoint=fail)
                self.assertEqual(self.counts(c),[0]*5);c.close()

    def test_update_rollback_at_every_stage(self):
        c=self.connect();mid=self.capture(c)['memory_id']
        for stage in ['after_revision','after_item','after_fts','after_embedding','after_audit','after_mutation','after_idempotency']:
            def fail(s):
                if s==stage:raise RuntimeError('synthetic failure')
            with self.subTest(stage=stage),self.assertRaises(RuntimeError):
                w.update(c,consumer_id='dot',request_id='u1',memory_id=mid,expected_revision=1,content='Changed',checkpoint=fail)
            self.assertEqual(self.counts(c),[1]*5)
            self.assertEqual(c.execute('SELECT current_revision FROM memory_items').fetchone()[0],1)
        c.close()

    def test_lost_response_replay_does_not_duplicate(self):
        c=self.connect()
        def fail(stage):
            if stage=='after_commit':raise RuntimeError('synthetic lost response')
        with self.assertRaises(RuntimeError):self.capture(c,checkpoint=fail)
        self.assertEqual(self.counts(c),[1]*5)
        result=self.capture(c);self.assertEqual(result['revision'],1);self.assertEqual(self.counts(c),[1]*5);c.close()

    def test_lock_exhaustion_bounded_no_partial_write(self):
        holder=self.connect();holder.execute('BEGIN IMMEDIATE');c=self.connect()
        start=time.monotonic()
        with self.assertRaises(w.Busy):self.capture(c,retries=2)
        self.assertLess(time.monotonic()-start,1)
        holder.execute('ROLLBACK');self.assertEqual(self.counts(c),[0]*5);c.close();holder.close()

    def test_unrelated_caller_transaction_not_rolled_back(self):
        c=self.connect();c.execute('BEGIN');c.execute("INSERT INTO memory_write_requests VALUES('dummy','dummy','dummy','dummy','{}',1)")
        with self.assertRaises(sqlite3.OperationalError):self.capture(c)
        self.assertTrue(c.in_transaction)
        self.assertEqual(c.execute("SELECT count(*) FROM memory_write_requests WHERE consumer_id='dummy'").fetchone()[0],1)
        c.rollback();c.close()

    def test_no_implicit_migration(self):
        path=self.root/'unmigrated';c=sqlite3.connect(path,isolation_level=None)
        c.executescript(Path(__file__).with_name('base_schema_fixture.sql').read_text())
        with self.assertRaises(sqlite3.OperationalError):self.capture(c)
        self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='memory_write_audit'").fetchone());c.close()

    def test_hermes_compat_same_ledger_dedup_and_return_types(self):
        c=self.connect();mid=hermes_compat.capture_memory(c,type='note',domain='learning',content='Public source',source='discover:hn',dedup_key='source-key')
        self.assertIsInstance(mid,str)
        other=self.capture(c,request='other',dedup_key='source-key')
        self.assertEqual(mid,other['memory_id'])
        rev=hermes_compat.update_memory_content(c,memory_id=mid,expected_revision=1,content='Updated source',summary=None,source='cron:derive_profile')
        self.assertEqual(rev,2)
        self.assertEqual(c.execute('SELECT source FROM memory_items').fetchone()[0],'discover:hn');c.close()

    def test_broker_enforces_kind_scope_sensitive_and_actor(self):
        c=self.connect()
        args={'request_id':'b1','domain':'work','kind':'idea','attribution_basis':'assistant_inferred','statement':'Maybe use a project index'}
        r=broker.operation(c,'dot',{'operation':'capture','arguments':args});self.assertEqual(r['revision'],1)
        for extra in [{'source':'manual'},{'consumer_id':'hermes'},{'domain':'health'},{'kind':'fact'},{'statement':'My bank details'},{'statement':'credential: dummy'},{'statement':'https://example.invalid'}]:
            with self.subTest(extra=extra),self.assertRaises(w.Invalid):broker.operation(c,'dot',{'operation':'capture','arguments':args|extra})
        self.assertEqual(self.counts(c),[1]*5);c.close()

    def test_broker_cannot_update_feed_or_other_consumer(self):
        c=self.connect();a=self.capture(c,source='discover:hn')['memory_id']
        args={'request_id':'u','memory_id':a,'expected_revision':1,'kind':'decision','attribution_basis':'user_corrected','statement':'Updated note'}
        with self.assertRaises(w.NotFound):broker.operation(c,'dot',{'operation':'update','arguments':args})
        b=self.capture(c,request='other',consumer='other')['memory_id']
        with self.assertRaises(w.NotFound):broker.operation(c,'dot',{'operation':'update','arguments':args|{'memory_id':b}})
        c.close()

    @unittest.skipUnless(sys.platform.startswith('linux'),'Linux SO_PEERCRED broker integration')
    def test_unix_peer_uid_auth_and_hidden_errors(self):
        path=str(self.root/'broker.sock');server=broker.Server(path,self.path,{os.getuid():'dot'})
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        def send(value):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                sock.connect(path);sock.sendall(json.dumps(value).encode()+b'\n');return json.loads(sock.makefile('rb').readline())
        try:
            request={'operation':'capture','arguments':{'request_id':'socket','domain':'work','kind':'preference','attribution_basis':'user_explicit','statement':'Prefer concise replies'}}
            a=send(request);self.assertTrue(a['ok']);self.assertEqual(a,send(request))
            server.consumers={os.getuid()+100000:'other'}
            self.assertEqual(send(request)['error']['code'],'unauthorized_peer')
        finally:server.shutdown();server.server_close();t.join()

    def test_embedding_atomicity_with_local_vector_stub(self):
        class Vector:
            has_vector=True
            def __init__(self,c):self.c=c
            def execute(self,*args):return self.c.execute(*args)
            def commit(self):self.c.commit()
        c=self.connect();c.create_function('vector32',1,lambda v:v)
        c.execute('CREATE TABLE memory_embeddings(memory_id TEXT PRIMARY KEY,embedding TEXT,model TEXT,embedded_at INTEGER)')
        db=Vector(c)
        def fail(stage):
            if stage=='after_embedding':raise RuntimeError('synthetic failure')
        with self.assertRaises(RuntimeError):self.capture(db,embedding=[.1,.2],checkpoint=fail)
        self.assertEqual(self.counts(c),[0]*5)
        self.assertEqual(c.execute('SELECT count(*) FROM memory_embeddings').fetchone()[0],0)
        result=self.capture(db,embedding=[.1,.2]);self.assertEqual(c.execute('SELECT count(*) FROM memory_embeddings').fetchone()[0],1)
        with self.assertRaises(RuntimeError):w.update(db,consumer_id='dot',request_id='u',memory_id=result['memory_id'],expected_revision=1,content='Changed',embedding=[.3,.4],checkpoint=fail)
        self.assertIn('0.100000',c.execute('SELECT embedding FROM memory_embeddings').fetchone()[0]);c.close()

    def test_long_legacy_timeout_is_temporarily_bounded_and_restored(self):
        holder=self.connect();holder.execute('BEGIN IMMEDIATE')
        c=sqlite3.connect(self.path,timeout=5,isolation_level=None);start=time.monotonic()
        with self.assertRaises(w.Busy):self.capture(c,retries=1)
        self.assertLess(time.monotonic()-start,1)
        self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0],5000)
        holder.rollback();holder.close();c.close()

if __name__=='__main__':unittest.main()
