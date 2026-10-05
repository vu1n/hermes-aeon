"""Synthetic proposal loop: no private data, network or model calls."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from brain_service.consolidation import Consolidation
from brain_service.gateway import Gateway
from brain_service.policy import Principal, Denied, Invalid, Unavailable
from brain_service.service import Service
from store import shared_writer

ROOT=Path(__file__).resolve().parent.parent
CAPS=frozenset({'read','capture','revise_own','retract_own','propose','derive','import'})
A=Principal('alpha','local',CAPS,'chat:alpha')
B=Principal('beta','local',CAPS,'chat:beta')

class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=sqlite3.connect(Path(self.temp.name)/'synthetic.sqlite',isolation_level=None)
        for name in ['aeon_sharedwrites/base_schema_fixture.sql','aeon_sharedwrites/migration.sql','brain_service/migration.sql']:
            self.db.executescript((ROOT/name).read_text())
        self.service=Service(self.db);self.loop=Consolidation(self.service)
        self.counter=0
    def tearDown(self):self.db.close();self.temp.cleanup()
    def capture(self,actor=A,statement='Synthetic compiler approach',**extra):
        self.counter+=1
        result=self.service.capture(actor,dict(request_id='source-'+str(self.counter),domain='work',statement=statement,**extra))
        return dict(memory_id=result['memory_id'],revision=result['revision'])
    def preview(self,*refs):return self.loop.preview(A,dict(source_refs=list(refs)))
    def stage(self,preview,*refs):return self.loop.stage(A,dict(source_refs=list(refs),expected_candidate_id=preview['candidate_id']))
    def revise(self,ref,statement='Changed compiler approach',**extra):
        self.counter+=1
        return self.service.revise(A,dict(request_id='revision-'+str(self.counter),memory_id=ref['memory_id'],expected_revision=ref['revision'],reason='Synthetic change',patch=dict(statement=statement,**extra)))

    def test_dry_run_cross_agent_provenance_dedup_and_no_preferences(self):
        a=self.capture(record_class='evidence',applicability='Compiler project')
        b=self.capture(B,record_class='evidence')
        before=self.db.total_changes
        candidate=self.preview(a,b)
        self.assertEqual(self.db.total_changes,before)
        self.assertEqual(candidate,self.preview(b,a))
        claims=candidate['candidate']['claims']
        self.assertEqual(len(claims),1)
        self.assertEqual({s['actor'] for s in claims[0]['sources']},{'alpha','beta'})
        self.assertEqual({s['support'] for s in claims[0]['sources']},{'source_observation'})
        staged=self.stage(candidate,a,b)
        item=self.service.get(B,staged['memory_id'])
        self.assertEqual(item['record_class'],'derived_view')
        self.assertEqual(item['kind'],'idea');self.assertEqual(item['verification'],'client_asserted')
        self.assertEqual(json.loads(item['content'])['review'],'owner_review_required')
        self.assertEqual(self.service.interests(B)['verified_owner_preferences'],[])

    def test_repeated_derived_views_do_not_add_support_or_duplicate_candidates(self):
        a=self.capture(record_class='evidence')
        preview=self.preview(a);first=self.stage(preview,a)
        derived=dict(memory_id=first['memory_id'],revision=1)
        repeated=self.capture(B,statement='Repeated interpretation',record_class='derived_view',evidence_refs=[dict(a,relationship='derived_from')])
        self.assertEqual(preview,self.preview(derived,repeated,a))
        self.assertEqual(first,self.stage(preview,derived,repeated,a))
        self.assertEqual(self.db.execute("SELECT count(*) FROM brain_records WHERE creator='alpha' AND record_class='derived_view'").fetchone()[0],1)

    def test_explicit_conflict_is_unresolved_and_original_records_unchanged(self):
        a=self.capture()
        result=self.service.propose(B,dict(request_id='alternative',memory_id=a['memory_id'],expected_revision=1,statement='Alternative compiler approach',reason='Different source claim'))
        b=dict(memory_id=result['memory_id'],revision=1)
        candidate=self.preview(a,b)['candidate']
        self.assertEqual(candidate['conflicts'][0]['resolution'],'unresolved')
        self.assertEqual(len(candidate['claims']),2)
        self.assertEqual(self.service.get(A,a['memory_id'])['revision'],1)
        self.assertEqual(candidate['claims'][1]['sources'][0]['support'],'client_claim')

    def test_support_and_supersedes_edges_survive_export(self):
        original=self.capture(record_class='evidence')
        claim=self.capture(B,statement='Supported compiler assertion',evidence_refs=[dict(original,relationship='supports')])
        replacement=self.capture(statement='Replacement compiler claim',evidence_refs=[dict(claim,relationship='supersedes')])
        candidate=self.preview(replacement)['candidate']
        sources={s['memory_id']:s for group in candidate['claims'] for s in group['sources']}
        self.assertEqual(sources[claim['memory_id']]['evidence_refs'],[dict(original,relationship='supports')])
        self.assertEqual(sources[replacement['memory_id']]['evidence_refs'],[dict(claim,relationship='supersedes')])
        staged=self.stage(dict(candidate_id=self.preview(replacement)['candidate_id']),replacement)
        item=self.service.get(B,staged['memory_id'])
        self.assertTrue(all(ref['relationship']=='derived_from' for ref in item['evidence_refs']))

    def test_stale_preview_rejected_and_staged_view_disappears_from_recall(self):
        a=self.capture();preview=self.preview(a);staged=self.stage(preview,a)
        self.revise(a)
        with self.assertRaises(Denied):self.stage(preview,a)
        self.assertIsNone(self.service.get(B,staged['memory_id']))
        self.assertFalse(any(i['id']==staged['memory_id'] for i in self.service.search(B,query='compiler')['items']))

    def test_withdrawn_or_restricted_or_uncertified_inputs_fail_closed(self):
        for patch_ in [{'status':'retracted'},{'sensitivity':'restricted_health'}]:
            a=self.capture();preview=self.preview(a)
            self.revise(a,**patch_)
            with self.assertRaises(Denied):self.stage(preview,a)
        health=self.capture(statement='Synthetic health observation')
        with self.assertRaises(Denied):self.preview(health)
        # A derived record has no eligibility if its certified input is restricted.
        good=self.capture();view=self.capture(record_class='derived_view',evidence_refs=[dict(good,relationship='derived_from')])
        self.revise(good,sensitivity='restricted_health')
        with self.assertRaises(Denied):self.preview(view)

    def test_transaction_recheck_blocks_update_between_preview_and_capture(self):
        a=self.capture();preview=self.preview(a);original=shared_writer.capture
        def racing(*args,**kwargs):
            # Canonical references already passed preflight; the hook must reject this change.
            self.revise(a)
            return original(*args,**kwargs)
        with patch.object(shared_writer,'capture',side_effect=racing):
            with self.assertRaises(Denied):self.stage(preview,a)
        self.assertEqual(self.db.execute("SELECT count(*) FROM brain_records WHERE record_class='derived_view'").fetchone()[0],0)

    def test_preview_rechecks_earlier_read_after_concurrent_revision(self):
        a=self.capture();b=self.capture(B,statement='Other compiler claim');original=self.service._get
        calls=0
        def racing(principal,mid,budget):
            nonlocal calls
            result=original(principal,mid,budget);calls+=1
            if calls==2:self.revise(a)
            return result
        with patch.object(self.service,'_get',side_effect=racing):
            with self.assertRaises(Denied):self.preview(a,b)

    def test_authorization_forgery_and_candidate_tampering(self):
        a=self.capture();preview=self.preview(a)
        reader=Principal('reader','local',frozenset({'read'}),'chat:reader')
        with self.assertRaises(Denied):self.loop.stage(reader,dict(source_refs=[a],expected_candidate_id=preview['candidate_id']))
        with self.assertRaises(Denied):self.stage(dict(preview,candidate_id='0'*64),a)
        with self.assertRaises(Invalid):self.loop.preview(A,dict(source_refs=[a],actor='beta'))
        gateway=Gateway(self.db,principals={'alpha':A},peer_bindings={101:'alpha'})
        result=gateway.handle_peer(101,dict(api_version='brain.general.v1',operation='consolidate_preview',arguments={'source_refs':[a]}))
        self.assertEqual(result['result'],preview)

    def test_uncertified_legacy_and_circular_lineage_are_excluded(self):
        legacy=shared_writer.capture(self.db,consumer_id='alpha',request_id='legacy',type='note',domain='work',
            content='Uncertified capsule',source='cron:capsule',entry_kind='idea',attribution_basis='assistant_inferred')
        with self.assertRaises(Denied):self.preview(dict(memory_id=legacy['memory_id'],revision=1))
        a=self.capture();b=self.capture(B)
        for ref,target in [(a,b),(b,a)]:
            self.db.execute('UPDATE brain_records SET evidence_json=? WHERE memory_id=?',
                (json.dumps([dict(target,relationship='supports')]),ref['memory_id']))
        with self.assertRaises(Denied):self.preview(a)

    def test_size_time_and_source_bounds(self):
        a=self.capture(statement='x'*20001)
        with self.assertRaises(Unavailable):self.preview(a)
        with self.assertRaises(Invalid):self.preview(*([a]*17))
        a=self.capture()
        with patch('brain_service.consolidation.time.monotonic',side_effect=[0,3]):
            with self.assertRaises(Unavailable):self.preview(a)
        with patch('brain_service.consolidation.MAX_OUTPUT',1):
            with self.assertRaises(Unavailable):self.preview(a)

    def test_expiry_and_mixed_domain_fail_closed(self):
        a=self.capture(record_class='working_context',expires_at=1)
        with self.assertRaises(Denied):self.preview(a)
        a=self.capture();b=self.service.capture(B,dict(request_id='learning',domain='learning',statement='Compiler learning'))
        with self.assertRaises(Invalid):self.preview(a,dict(memory_id=b['memory_id'],revision=1))
