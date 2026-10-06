"""Synthetic durable lifecycle; no model or production store."""
import concurrent.futures
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from brain_service.policy import Principal, Denied, Invalid, Unavailable
from brain_service.service import Service
from brain_service.consolidation import Consolidation
from brain_service.review import Reviews
from store.shared_writer import Conflict, IdempotencyConflict

ROOT=Path(__file__).resolve().parent.parent
AUTHOR=Principal('writer','local',frozenset({'read','capture','derive','revise_own','retract_own'}),'chat:writer')
REVIEWER=Principal('owner-review','local',frozenset({'read','review'}),'review:owner')

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'synthetic.sqlite'
        self.db=sqlite3.connect(self.path,isolation_level=None)
        for path in ['aeon_sharedwrites/base_schema_fixture.sql','aeon_sharedwrites/migration.sql','brain_service/migration.sql']:
            self.db.executescript((ROOT/path).read_text())
        self.service=Service(self.db);self.reviews=Reviews(self.service)
        receipt=self.service.capture(AUTHOR,dict(request_id='root',domain='work',statement='Synthetic compiler claim'))
        self.source=dict(memory_id=receipt['memory_id'],revision=1)
    def tearDown(self):self.db.close();self.temp.cleanup()
    def args(self,request='attempt-one',parent=None):
        preview=Consolidation(self.service).preview(AUTHOR,{'source_refs':[self.source]})
        args=dict(request_id=request,source_refs=[dict(self.source)],expected_candidate_id=preview['candidate_id'])
        if parent:args['parent_attempt']=parent
        return args
    def stage(self,request='attempt-one',parent=None):return self.reviews.stage(AUTHOR,self.args(request,parent))
    def decide(self,attempt,request='decision',**extra):
        return self.reviews.decide(REVIEWER,dict(request_id=request,attempt_id=attempt['attempt_id'],expected_revision=1,decision='accepted',reason_code='faithful',**extra))
    def update(self,mid,revision=1):
        return self.service.revise(AUTHOR,dict(request_id='update-'+mid+'-'+str(revision),memory_id=mid,expected_revision=revision,reason='Synthetic update',patch={'statement':'Changed compiler claim'}))

    def test_atomic_stage_replay_pending_accept_history_and_no_promotion(self):
        args=self.args();attempt=self.reviews.stage(AUTHOR,args)
        self.assertEqual(attempt,self.reviews.stage(AUTHOR,args))
        entry=self.reviews.pending(REVIEWER,{})['items'][0]
        self.assertEqual(entry['attempt_id'],attempt['attempt_id']);self.assertTrue(entry['live_eligible'])
        self.assertEqual(entry['candidate_ref'],dict(memory_id=attempt['memory_id'],revision=1))
        decision=self.decide(attempt);self.assertEqual(decision,self.decide(attempt))
        self.assertEqual(self.reviews.pending(REVIEWER,{})['items'],[])
        history=self.reviews.history(REVIEWER,{'attempt_id':attempt['attempt_id']})
        self.assertEqual(history['state'],'accepted');self.assertEqual(history['decision']['reviewer'],'owner-review')
        self.assertEqual(history['decision']['expected_revision'],1)
        self.assertEqual(self.service.get(AUTHOR,self.source['memory_id'])['revision'],1)
        self.assertEqual(self.service.interests(REVIEWER)['verified_owner_preferences'],[])

    def test_invalidated_attempt_stays_pending_and_fresh_attempt_does_not_mutate_receipt(self):
        first=self.stage();args=self.args()
        unrelated=self.service.capture(AUTHOR,dict(request_id='other',domain='work',statement='Unrelated compiler note'))
        self.update(unrelated['memory_id'])
        self.assertEqual(self.reviews.stage(AUTHOR,args),first)
        self.assertFalse(self.reviews.history(REVIEWER,{'attempt_id':first['attempt_id']})['live_eligible'])
        with self.assertRaises(Denied):self.decide(first)
        second=self.stage('attempt-two',first['attempt_id'])
        self.assertNotEqual(second['memory_id'],first['memory_id'])
        self.assertTrue(self.service.get(AUTHOR,second['memory_id']))
        self.assertEqual(self.reviews.history(REVIEWER,{'attempt_id':second['attempt_id']})['parent_attempt'],first['attempt_id'])
        self.assertEqual(self.reviews.stage(AUTHOR,args),first)

    def test_changed_source_versions_need_new_preview_and_regeneration(self):
        first=self.stage();oldargs=self.args('two',first['attempt_id']);self.update(self.source['memory_id'])
        with self.assertRaises(Denied):self.reviews.stage(AUTHOR,oldargs)
        self.source['revision']=2
        second=self.stage('two',first['attempt_id'])
        self.assertTrue(self.service.get(REVIEWER,second['memory_id']))
        with self.assertRaises(Denied):self.decide(first)
        self.decide(second)

    def test_rejection_retains_reason_but_does_not_mean_semantic_false(self):
        attempt=self.stage();self.update(self.source['memory_id'])
        self.reviews.decide(REVIEWER,dict(request_id='reject',attempt_id=attempt['attempt_id'],decision='rejected',reason_code='outdated'))
        history=self.reviews.history(REVIEWER,{'attempt_id':attempt['attempt_id']})
        self.assertEqual(history['decision']['reason_code'],'outdated')
        self.assertNotIn('content',str(history));self.assertNotIn('source_refs',history)

    def test_authorization_forgery_and_wrong_source_regeneration(self):
        attempt=self.stage()
        with self.assertRaises(Denied):self.reviews.history(AUTHOR,{'attempt_id':attempt['attempt_id']})
        with self.assertRaises(Denied):self.reviews.decide(AUTHOR,dict(request_id='forged',attempt_id=attempt['attempt_id'],expected_revision=1,decision='accepted',reason_code='faithful'))
        extra=self.service.capture(AUTHOR,dict(request_id='different',domain='work',statement='Different compiler record'))
        ref=dict(memory_id=extra['memory_id'],revision=1)
        preview=Consolidation(self.service).preview(AUTHOR,{'source_refs':[ref]})
        with self.assertRaises(Denied):self.reviews.stage(AUTHOR,dict(request_id='replacement',source_refs=[ref],expected_candidate_id=preview['candidate_id'],parent_attempt=attempt['attempt_id']))

    def test_decision_cas_idempotency_and_changed_candidate(self):
        attempt=self.stage();self.decide(attempt)
        with self.assertRaises(Conflict):self.decide(attempt,request='other')
        altered=dict(request_id='decision',attempt_id=attempt['attempt_id'],expected_revision=1,decision='rejected',reason_code='duplicate')
        with self.assertRaises(IdempotencyConflict):self.reviews.decide(REVIEWER,altered)
        second=self.stage('second');self.update(second['memory_id'])
        with self.assertRaises(Conflict):self.decide(second,request='changed')
        with self.assertRaises(Denied):self.reviews.decide(REVIEWER,dict(request_id='changed2',attempt_id=second['attempt_id'],expected_revision=2,decision='accepted',reason_code='faithful'))

    def test_candidate_cannot_replace_pins_and_launder_revoked_evidence(self):
        attempt=self.stage()
        other=self.service.capture(AUTHOR,dict(request_id='alternate-source',domain='work',statement='Other compiler claim'))
        self.service.revise(AUTHOR,dict(request_id='replace-pins',memory_id=attempt['memory_id'],expected_revision=1,
            reason='Synthetic replacement',patch={'title':'Changed title'},
            evidence_refs=[dict(memory_id=other['memory_id'],revision=1,relationship='derived_from')]))
        with self.assertRaises(Denied):self.reviews.decide(REVIEWER,dict(request_id='altered-pins',attempt_id=attempt['attempt_id'],expected_revision=2,decision='accepted',reason_code='faithful'))

    def test_atomic_attempt_hook_rollback_and_retry(self):
        args=self.args()
        class FailingDB:
            def __getattr__(proxy,name):return getattr(self.db,name)
            def execute(proxy,sql,params=()):
                result=self.db.execute(sql,params)
                if sql.startswith('INSERT INTO brain_review_attempts'):raise RuntimeError('synthetic publication failure')
                return result
        with self.assertRaises(RuntimeError):Reviews(Service(FailingDB())).stage(AUTHOR,args)
        self.assertEqual(self.db.execute('SELECT count(*) FROM brain_review_attempts').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM memory_items').fetchone()[0],1)
        attempt=self.reviews.stage(AUTHOR,args)
        self.assertEqual(self.db.execute('SELECT count(*) FROM brain_review_attempts').fetchone()[0],1)
        self.assertEqual(attempt,self.reviews.stage(AUTHOR,args))

    def test_concurrent_decisions_have_one_winner(self):
        attempt=self.stage()
        def decide(index):
            db=sqlite3.connect(self.path,isolation_level=None,timeout=1)
            try:
                return Reviews(Service(db)).decide(REVIEWER,dict(request_id='concurrent-'+str(index),attempt_id=attempt['attempt_id'],
                    expected_revision=1,decision='accepted',reason_code='faithful'))
            except Conflict:return None
            finally:db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(decide,range(4)))
        self.assertEqual(sum(result is not None for result in results),1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM brain_review_decisions').fetchone()[0],1)

    def test_health_revocation_blocks_acceptance_and_regeneration(self):
        attempt=self.stage()
        self.service.revise(AUTHOR,dict(request_id='restriction',memory_id=self.source['memory_id'],expected_revision=1,
            reason='Synthetic restriction',patch={'sensitivity':'restricted_health'}))
        with self.assertRaises(Denied):self.decide(attempt)
        self.source['revision']=2
        with self.assertRaises(Denied):self.stage('new',attempt['attempt_id'])
        history=self.reviews.history(REVIEWER,{'attempt_id':attempt['attempt_id']})
        self.assertFalse(history['live_eligible'])
        self.assertNotIn('pins_json',history);self.assertNotIn('candidate_id',history)

    def test_history_survives_restart_and_pending_is_bounded(self):
        attempt=self.stage()
        db=sqlite3.connect(self.path,isolation_level=None)
        try:self.assertEqual(Reviews(Service(db)).history(REVIEWER,{'attempt_id':attempt['attempt_id']})['state'],'pending')
        finally:db.close()
        self.stage('second');page=self.reviews.pending(REVIEWER,{'limit':1})
        self.assertEqual(len(page['items']),1);self.assertIsNotNone(page['next_after'])
        self.assertEqual(len(self.reviews.pending(REVIEWER,{'limit':1,'after':page['next_after']})['items']),1)
        with self.assertRaises(Invalid):self.reviews.pending(REVIEWER,{'limit':51})

    def test_second_client_can_fetch_and_decide_from_queue_only(self):
        self.stage()
        db=sqlite3.connect(self.path,isolation_level=None)
        try:
            service=Service(db);reviews=Reviews(service)
            queued=reviews.pending(REVIEWER,{})['items'][0]
            ref=queued['candidate_ref']
            candidate=service.get(REVIEWER,ref['memory_id'])
            self.assertEqual(candidate['revision'],ref['revision'])
            reviews.decide(REVIEWER,dict(request_id='second-client',attempt_id=queued['attempt_id'],
                expected_revision=ref['revision'],decision='accepted',reason_code='faithful'))
            history=reviews.history(REVIEWER,{'attempt_id':queued['attempt_id']})
            self.assertEqual(history['candidate_ref'],ref);self.assertEqual(history['state'],'accepted')
        finally:db.close()

    def test_unavailable_closure_hides_ref_and_never_bypasses_live_cas(self):
        attempt=self.stage()
        close=dict(request_id='close',attempt_id=attempt['attempt_id'],decision='rejected',reason_code='outdated')
        with self.assertRaises(Denied):self.reviews.decide(REVIEWER,close)
        with self.assertRaises(Invalid):self.reviews.decide(REVIEWER,dict(close,decision='accepted'))
        self.service.revise(AUTHOR,dict(request_id='restricted-candidate',memory_id=attempt['memory_id'],expected_revision=1,
            reason='Synthetic restriction',patch={'sensitivity':'restricted_health'}))
        self.assertIsNone(self.reviews.pending(REVIEWER,{})['items'][0]['candidate_ref'])
        history=self.reviews.history(REVIEWER,{'attempt_id':attempt['attempt_id']})
        self.assertIsNone(history['candidate_ref']);self.assertNotIn(attempt['memory_id'],str(history))
        self.assertEqual(self.reviews.decide(REVIEWER,close),self.reviews.decide(REVIEWER,close))
        self.assertEqual(self.reviews.history(REVIEWER,{'attempt_id':attempt['attempt_id']})['decision']['expected_revision'],0)
        with self.assertRaises(Conflict):self.reviews.decide(REVIEWER,dict(close,request_id='competing'))
        self.assertEqual(self.reviews.pending(REVIEWER,{})['items'],[])

    def test_retracted_and_missing_candidate_can_be_closed_without_revision(self):
        attempt=self.stage()
        self.service.revise(AUTHOR,dict(request_id='retract-candidate',memory_id=attempt['memory_id'],expected_revision=1,reason='Synthetic withdrawal'),retract=True)
        self.reviews.decide(REVIEWER,dict(request_id='close-retracted',attempt_id=attempt['attempt_id'],decision='rejected',reason_code='outdated'))
        second=self.stage('missing')
        # Synthetic corruption/removal: preserve the attempt to exercise unavailable closure.
        self.db.execute('DELETE FROM memory_items WHERE id=?',(second['memory_id'],))
        self.reviews.decide(REVIEWER,dict(request_id='close-missing',attempt_id=second['attempt_id'],decision='rejected',reason_code='outdated'))

    def test_oldest_first_ties_duplicates_and_cursor_survive_decisions(self):
        attempts=[self.stage('ordered-'+str(i)) for i in range(3)]
        ordered=sorted(attempts,key=lambda a:a['attempt_id'],reverse=True)
        for index,attempt in enumerate(ordered):
            self.db.execute('UPDATE brain_review_attempts SET created_at=? WHERE attempt_id=?',(100+index,attempt['attempt_id']))
        page=self.reviews.pending(REVIEWER,{'limit':1});first=page['items'][0]
        self.assertEqual(first['attempt_id'],ordered[0]['attempt_id'])
        self.assertTrue(first['has_duplicates'])
        self.assertTrue(all(row['duplicate_group']==first['attempt_id'] for row in self.reviews.pending(REVIEWER,{})['items']))
        self.decide(ordered[0])
        rest=self.reviews.pending(REVIEWER,{'after':page['next_after']})['items']
        self.assertEqual([x['attempt_id'] for x in rest],[a['attempt_id'] for a in ordered[1:]])
        self.assertTrue(all(x['has_duplicates'] for x in rest))
        self.db.execute('UPDATE brain_review_attempts SET created_at=200')
        tied=self.reviews.pending(REVIEWER,{'limit':1})
        following=self.reviews.pending(REVIEWER,{'after':tied['next_after']})
        self.assertLess(tied['items'][0]['attempt_id'],following['items'][0]['attempt_id'])
        for cursor in ['bad','-1:'+ordered[0]['attempt_id'],'9'*20+':'+ordered[0]['attempt_id']]:
            with self.assertRaises(Invalid):self.reviews.pending(REVIEWER,{'after':cursor})

    def test_queue_revision_is_live_and_acceptance_rechecks_source_change(self):
        attempt=self.stage();ref=self.reviews.pending(REVIEWER,{})['items'][0]['candidate_ref']
        self.service.revise(AUTHOR,dict(request_id='candidate-title',memory_id=ref['memory_id'],expected_revision=1,
            reason='Synthetic edit',patch={'title':'Edited title'}))
        with self.assertRaises(Conflict):self.decide(attempt)
        current=self.reviews.history(REVIEWER,{'attempt_id':attempt['attempt_id']})['candidate_ref']
        self.assertEqual(current['revision'],2)
        self.update(self.source['memory_id'])
        with self.assertRaises(Denied):self.reviews.decide(REVIEWER,dict(request_id='after-source-change',attempt_id=attempt['attempt_id'],expected_revision=2,decision='accepted',reason_code='faithful'))
        self.assertIsNone(self.reviews.history(REVIEWER,{'attempt_id':attempt['attempt_id']})['candidate_ref'])
        self.reviews.decide(REVIEWER,dict(request_id='close-after-edit',attempt_id=attempt['attempt_id'],decision='rejected',reason_code='outdated'))

    def test_unavailable_closure_rechecks_eligibility_and_hides_revision_conflict(self):
        attempt=self.stage()
        unrelated=self.service.capture(AUTHOR,dict(request_id='unrelated',domain='work',statement='Synthetic unrelated note'))
        self.update(unrelated['memory_id'])
        self.assertIsNone(self.reviews.pending(REVIEWER,{})['items'][0]['candidate_ref'])
        # Synthetic restored validity represents an eligibility change after queue retrieval.
        self.db.execute('UPDATE brain_records SET valid=1 WHERE memory_id=?',(attempt['memory_id'],))
        close=dict(request_id='close-restored',attempt_id=attempt['attempt_id'],decision='rejected',reason_code='outdated')
        with self.assertRaises(Denied):self.reviews.decide(REVIEWER,close)
        self.assertEqual(self.reviews.history(REVIEWER,{'attempt_id':attempt['attempt_id']})['state'],'pending')
        self.service.revise(AUTHOR,dict(request_id='hide-revision',memory_id=attempt['memory_id'],expected_revision=1,
            reason='Synthetic restriction',patch={'sensitivity':'restricted_health'}))
        with self.assertRaises(Denied):self.reviews.decide(REVIEWER,dict(close,expected_revision=1))
        with self.assertRaises(Denied):self.reviews.decide(REVIEWER,dict(close,expected_revision=2))
        self.reviews.decide(REVIEWER,close)

    def test_concurrent_invalid_closure_has_one_winner(self):
        attempt=self.stage();self.update(self.source['memory_id'])
        def close(index):
            db=sqlite3.connect(self.path,isolation_level=None,timeout=1)
            try:
                return Reviews(Service(db)).decide(REVIEWER,dict(request_id='close-'+str(index),attempt_id=attempt['attempt_id'],
                    decision='rejected',reason_code='outdated'))
            except Conflict:return None
            finally:db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(close,range(4)))
        self.assertEqual(sum(result is not None for result in results),1)

    def test_source_change_racing_decision_preserves_history_and_live_policy(self):
        attempt=self.stage();barrier=threading.Barrier(2)
        def run(change):
            db=sqlite3.connect(self.path,isolation_level=None,timeout=2)
            try:
                service=Service(db);barrier.wait(timeout=2)
                if change:
                    return service.revise(AUTHOR,dict(request_id='racing-source-edit',memory_id=self.source['memory_id'],
                        expected_revision=1,reason='Synthetic racing edit',patch={'statement':'Changed compiler claim'}))
                try:return Reviews(service).decide(REVIEWER,dict(request_id='racing-accept',attempt_id=attempt['attempt_id'],
                    expected_revision=1,decision='accepted',reason_code='faithful'))
                except Denied:return None
            finally:db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[False,True]))
        history=self.reviews.history(REVIEWER,{'attempt_id':attempt['attempt_id']})
        self.assertEqual(history['state'],'accepted' if results[0] else 'pending')
        self.assertIsNone(history['candidate_ref']);self.assertFalse(history['live_eligible'])
        if results[0] is None:
            with self.assertRaises(Denied):self.reviews.decide(REVIEWER,dict(request_id='late-accept',attempt_id=attempt['attempt_id'],
                expected_revision=1,decision='accepted',reason_code='faithful'))

    def test_review_requires_explicit_migration(self):
        self.db.execute('DROP TABLE brain_review_decisions');self.db.execute('DROP TABLE brain_review_attempts')
        with self.assertRaises(Unavailable):self.reviews.pending(REVIEWER,{})
        from brain_service.migrate import migrate
        migrate(self.path)
        self.assertEqual(self.reviews.pending(REVIEWER,{})['items'],[])
        self.assertEqual(self.db.execute('SELECT count(*) FROM memory_items').fetchone()[0],1)
