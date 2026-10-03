"""General foundation gates use only synthetic local owners, agents and records."""
import concurrent.futures
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from brain_service.policy import API_VERSION, HERMES, Principal, Denied, Invalid, Unavailable, general_envelope
from brain_service.service import Service, now_ms
from brain_service.gateway import Gateway
from brain_service.migrate import migrate
from store import shared_writer as writer, queries

ROOT=Path(__file__).resolve().parent.parent
ALPHA=Principal('alpha','local',frozenset({'read','capture','revise_own','retract_own','propose'}),'chat:alpha')
BETA=Principal('beta','local',ALPHA.capabilities,'chat:beta')
IMPORTER=Principal('feed','local',frozenset({'read','capture','import'}),'discover:hn')
DERIVER=Principal('derived','local',frozenset({'read','capture','derive','revise_own'}),'derived:general')

class GeneralService(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'synthetic.sqlite'
        self.db=sqlite3.connect(self.path,isolation_level=None)
        for file in ['aeon_sharedwrites/base_schema_fixture.sql','aeon_sharedwrites/migration.sql','brain_service/migration.sql']:
            self.db.executescript((ROOT/file).read_text())
        self.service=Service(self.db)
        self.gateway=Gateway(self.db,principals={p.id:p for p in [ALPHA,BETA,IMPORTER]},peer_bindings={1001:'alpha',1002:'beta',1003:'feed'})
    def tearDown(self):self.db.close();self.temp.cleanup()
    def args(self,request='capture1',**extra):
        return dict(request_id=request,statement='Synthetic memory service project',domain='work',topics=['memory-service'],**extra)
    def capture(self,principal=ALPHA,request='capture1',**extra):
        args=self.args(request)
        args.update(extra)
        return self.service.capture(principal,args)
    def revise(self,mid,patch_,request='revise1',revision=1,principal=ALPHA):
        return self.service.revise(principal,dict(request_id=request,memory_id=mid,expected_revision=revision,reason='Synthetic correction',patch=patch_))
    def counts(self):
        return [self.db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ['memory_items','memory_revisions','brain_revision_metadata','brain_requests']]

    def test_principal_owner_and_source_cannot_be_client_fields(self):
        request=dict(api_version=API_VERSION,operation='capture',arguments=self.args())
        result=self.gateway.handle_peer(1002,request)
        self.assertTrue(result['ok'])
        item=self.service.get(ALPHA,result['result']['memory_id'])
        self.assertEqual(item['source'],'chat:beta');self.assertEqual(item['owner_id'],'local')
        self.assertEqual(item['provenance']['capturing_actor'],'beta')
        for field,value in [('actor','alpha'),('owner_id','other'),('capabilities',['import']),('source','discover:hn'),('verification','owner_verified')]:
            args=self.args('forged-'+field);args[field]=value
            self.assertEqual(self.gateway.handle_peer(1002,dict(request,arguments=args))['error']['code'],'invalid_request')
        self.assertFalse(self.gateway.handle_peer(9999,request)['ok'])
        with self.assertRaises(Invalid):Service(self.db,owner_id='another-owner')
        with self.assertRaises(Invalid):Gateway(self.db,principals={'foreign':Principal('foreign','other',ALPHA.capabilities,'chat:foreign')},peer_bindings={1:'foreign'})

    def test_lost_response_replay_payload_conflict_and_no_extra_revision(self):
        a=self.capture();self.assertEqual(a,self.capture())
        with self.assertRaises(writer.IdempotencyConflict):self.capture(statement='Changed payload')
        self.assertEqual(self.counts(),[1,1,1,1])
        mid=a['memory_id'];args=dict(request_id='u',memory_id=mid,expected_revision=1,reason='Correction',patch=dict(topics=['agent-evals']))
        b=self.service.revise(ALPHA,args)
        self.revise(mid,{'statement':'A later revision'},request='later',revision=2)
        self.assertEqual(b,self.service.revise(ALPHA,args))
        self.assertEqual(self.counts(),[1,3,3,3])

    def test_two_agents_own_changes_only_and_linked_proposal(self):
        a=self.capture();mid=a['memory_id']
        with self.assertRaises(Denied):self.revise(mid,{'statement':'A different agent replacement'},principal=BETA)
        proposal=dict(request_id='p',memory_id=mid,expected_revision=1,statement='Alternative project approach',reason='A differing claim')
        b=self.service.propose(BETA,proposal)
        self.assertEqual(self.service.get(ALPHA,mid)['revision'],1)
        item=self.service.get(ALPHA,b['memory_id'])
        self.assertEqual(item['provenance']['capturing_actor'],'beta')
        self.assertEqual(item['evidence_refs'][0]['relationship'],'contradicts')
        self.revise(mid,{'statement':'Changed target'})
        self.assertEqual(b,self.service.propose(BETA,proposal))

    def test_importer_namespace_and_immutable_evidence(self):
        a=self.capture(IMPORTER,record_class='evidence')
        item=self.service.get(ALPHA,a['memory_id'])
        self.assertEqual(item['source'],'discover:hn');self.assertEqual(item['verification'],'source_observation')
        with self.assertRaises(Denied):self.capture(record_class='evidence')
        with self.assertRaises(Denied):self.revise(a['memory_id'],{'statement':'Pretend source changed'},principal=IMPORTER)

    def test_existing_correction_shape_preserves_attribution_and_replay(self):
        mid=self.capture()['memory_id']
        args=dict(request_id='compat',memory_id=mid,expected_revision=1,
                  statement='Corrected synthetic project',summary='Source claim',kind='preference',
                  attribution_basis='user_explicit',conversation_ref='opaque-message')
        request=dict(api_version=API_VERSION,operation='aeon_correct',arguments=args)
        result=self.gateway.handle_peer(1001,request)
        self.assertTrue(result['ok'],result)
        item=self.service.get(BETA,mid)
        self.assertEqual(item['kind'],'preference')
        self.assertEqual(item['verification'],'client_asserted')
        self.assertEqual(item['provenance']['attribution_basis'],'user_explicit')
        self.assertEqual(item['provenance']['conversation_ref'],'opaque-message')
        self.assertEqual(item['provenance']['capture_origin'],'chat:alpha')
        self.assertEqual(result,self.gateway.handle_peer(1001,request))
        forged=dict(args,actor='beta')
        self.assertEqual(self.gateway.handle_peer(1001,dict(request,arguments=forged))['error']['code'],'invalid_request')
        self.assertEqual(self.gateway.handle_peer(1002,request)['error']['code'],'not_found_or_denied')

    def test_record_class_filter_and_complete_count_contract(self):
        self.capture(record_class='working_context')
        self.capture(IMPORTER,request='import',record_class='evidence')
        recent=self.service.recent(ALPHA,record_class='working_context')
        self.assertEqual(recent['count'],1)
        self.assertTrue(recent['total_is_complete'])
        self.assertEqual(self.service.search(ALPHA,query='project',record_class='evidence')['count'],1)
        with self.assertRaises(Invalid):self.service.recent(ALPHA,record_class='other')
        with self.assertRaises(Invalid):self.service.search(ALPHA,query='project',record_class='other')

    def test_restricted_only_revision_does_not_invalidate_general_view(self):
        mid=self.capture()['memory_id']
        view=self.capture(DERIVER,request='view',record_class='derived_view',
                          evidence_refs=[dict(memory_id=mid,revision=1,relationship='derived_from')])['memory_id']
        restricted=queries.capture_memory(self.db,type='note',domain='health',content='Synthetic health record',source='manual',request_id='restricted')
        before=self.service.status(ALPHA)
        queries.update_memory_content(self.db,memory_id=restricted,content='Changed synthetic health record',summary=None,source='manual',expected_revision=1,request_id='restricted-update')
        self.assertIsNotNone(self.service.get(BETA,view))
        self.assertEqual(self.service.status(ALPHA),before)

    def test_complete_revision_snapshots_and_metadata_cas(self):
        mid=self.capture(project_id='project-a',record_class='working_context',conversation_ref='opaque-message')['memory_id']
        self.revise(mid,dict(statement='Updated statement',title='Updated title',summary='Updated summary',topics=['agent-evals'],project_id='project-b',domain='learning',applicability='current build',expires_at=now_ms()+10000))
        snapshots=[json.loads(r[0]) for r in self.db.execute('SELECT snapshot_json FROM brain_revision_metadata ORDER BY revision_n')]
        self.assertEqual(len(snapshots),2)
        self.assertEqual(snapshots[0]['item']['project_id'],'project-a')
        second=snapshots[1]
        self.assertEqual(second['item']['domain'],'learning');self.assertEqual(second['item']['title'],'Updated title')
        self.assertEqual(second['metadata']['topics'],['agent-evaluation']);self.assertEqual(second['metadata']['applicability'],'current build')
        self.assertEqual(second['provenance']['actor'],'alpha')
        with self.assertRaises(writer.Conflict):self.revise(mid,{'topics':['another']},request='stale',revision=1)
        self.assertEqual(self.counts(),[1,2,2,2])

    def test_revision_metadata_and_index_roll_back_with_canonical_change(self):
        mid=self.capture()['memory_id'];before=self.counts()
        with patch.object(self.service,'_result',side_effect=RuntimeError('synthetic indexing failure')):
            with self.assertRaises(RuntimeError):self.revise(mid,{'statement':'Failed replacement'})
        self.assertEqual(self.counts(),before)
        self.assertEqual(self.service.get(ALPHA,mid)['revision'],1)
        self.assertEqual(self.service.search(ALPHA,query='project')['count'],1)

    def test_concurrent_cas_one_success_one_conflict(self):
        mid=self.capture()['memory_id']
        def apply(number):
            db=sqlite3.connect(self.path,isolation_level=None,timeout=.1)
            try:
                return Service(db).revise(ALPHA,dict(request_id='parallel'+str(number),memory_id=mid,expected_revision=1,reason='Concurrent edit',patch=dict(topics=['topic-'+str(number)])))
            except writer.Conflict:return 'conflict'
            finally:db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(apply,[1,2]))
        self.assertEqual(results.count('conflict'),1)
        self.assertEqual(self.service.get(BETA,mid)['revision'],2)

    def test_general_only_indexes_filters_and_receipt_watermark(self):
        a=self.capture(project_id='build-a',record_class='working_context')
        self.capture(BETA,request='beta1',topics=['agent-evals'],domain='learning')
        self.assertEqual(a['search_visibility'],'visible');self.assertEqual(a['durability'],'committed')
        result=self.service.search(BETA,query='project',topic='memory-service',project_id='build-a',source='chat:alpha',domain='work',min_commit_sequence=a['commit_sequence'])
        self.assertEqual(result['count'],1);self.assertEqual(result['items'][0]['matched_topics'],['memory-services'])
        self.assertFalse(result['indexing_pending'])
        self.assertTrue(self.service.search(ALPHA,query='project',min_commit_sequence=10000)['indexing_pending'])
        self.assertEqual(self.service.recent(ALPHA,topic='agent-evals')['count'],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM brain_general_fts').fetchone()[0],2)

    def test_frozen_index_cannot_undo_revocation_or_revision(self):
        mid=self.capture()['memory_id']
        frozen=self.db.execute('SELECT * FROM brain_general_index').fetchone()
        self.revise(mid,{'sensitivity':'restricted_health'})
        # Model an old publisher finishing late, retaining an old index row and snippet.
        self.db.execute('INSERT OR REPLACE INTO brain_general_index VALUES(?,?,?)',frozen)
        self.db.execute('INSERT INTO brain_general_fts VALUES(?,?,?,?,?)',(mid,'stale project','','old text','memory-services'))
        self.assertIsNone(self.service.get(BETA,mid))
        self.assertEqual(self.service.search(BETA,query='project')['count'],0)
        self.assertEqual(self.service.recent(BETA)['total'],0)
        with self.assertRaises(Denied):self.revise(mid,{'sensitivity':'general'},request='lower',revision=2)

    def test_retraction_and_peer_revocation_are_immediate(self):
        a=self.capture();mid=a['memory_id']
        args=dict(request_id='retract',memory_id=mid,expected_revision=1,reason='Withdraw assertion')
        result=self.service.revise(ALPHA,args,retract=True)
        self.assertEqual(result,self.service.revise(ALPHA,args,retract=True))
        self.assertIsNone(self.service.get(BETA,mid));self.assertEqual(self.service.search(BETA,query='project')['count'],0)
        self.gateway.peer_bindings.pop(1002)
        self.assertEqual(self.gateway.handle_peer(1002,dict(api_version=API_VERSION,operation='status',arguments={}))['error']['code'],'not_found_or_denied')

    def test_owned_linked_assertion_can_be_retracted_after_input_changes(self):
        mid=self.capture()['memory_id']
        proposed=self.service.propose(BETA,dict(request_id='proposal',memory_id=mid,expected_revision=1,
                                               statement='Alternative project direction',reason='Differing source claim'))['memory_id']
        self.revise(mid,{'statement':'Changed input'})
        result=self.service.revise(BETA,dict(request_id='withdraw',memory_id=proposed,expected_revision=1,
                                            reason='Withdraw historical proposal'),retract=True)
        self.assertEqual(result['revision'],2)
        self.assertEqual(self.db.execute('SELECT status FROM memory_items WHERE id=?',(proposed,)).fetchone()[0],'retracted')
        snapshot=json.loads(self.db.execute('SELECT snapshot_json FROM brain_revision_metadata WHERE memory_id=? AND revision_n=2',(proposed,)).fetchone()[0])
        self.assertEqual(snapshot['metadata']['evidence_refs'][0]['revision'],1)

    def test_revision_status_cannot_bypass_retraction_capability(self):
        mid=self.capture(DERIVER)['memory_id']
        with self.assertRaises(Denied):self.revise(mid,{'status':'retracted'},principal=DERIVER)
        self.assertEqual(self.service.get(ALPHA,mid)['revision'],1)

    def test_feed_volume_does_not_hide_active_working_context(self):
        mid=self.capture(record_class='working_context')['memory_id']
        self.db.execute('UPDATE memory_items SET captured_at=? WHERE id=?',(now_ms()-1000,mid))
        for number in range(101):self.capture(IMPORTER,request='newer'+str(number),record_class='evidence')
        self.assertEqual(self.service.interests(BETA)['active_context'][0]['memory_id'],mid)

    def test_restricted_lineage_and_derived_revocation(self):
        mid=self.capture()['memory_id']
        ref=dict(memory_id=mid,revision=1,relationship='derived_from')
        view=self.capture(DERIVER,record_class='derived_view',evidence_refs=[ref],statement='A harmless synthesis')['memory_id']
        self.assertIsNotNone(self.service.get(BETA,view))
        self.revise(mid,{'sensitivity':'restricted_health'})
        self.assertIsNone(self.service.get(BETA,view))
        with self.assertRaises(Denied):self.capture(DERIVER,request='restricted-view',record_class='derived_view',evidence_refs=[dict(ref,revision=2)])

    def test_shared_lineage_is_memoized_without_losing_live_revocation(self):
        all_ids=[]
        layer=[]
        for number in range(4):
            layer.append(self.capture(request='leaf'+str(number))['memory_id'])
        all_ids.extend(layer)
        for level in range(4):
            refs=[dict(memory_id=mid,revision=1,relationship='supports') for mid in layer]
            layer=[self.capture(request=f'layer{level}-{number}',evidence_refs=refs)['memory_id'] for number in range(4)]
            all_ids.extend(layer)
        root=self.capture(request='graph-root',evidence_refs=[dict(memory_id=mid,revision=1,relationship='supports') for mid in layer])['memory_id']
        with patch.object(self.service,'_item',wraps=self.service._item) as load:
            self.assertIsNotNone(self.service.get(BETA,root))
            self.assertEqual(load.call_count,len(all_ids)+1)
        self.revise(all_ids[0],{'sensitivity':'restricted_health'})
        self.assertIsNone(self.service.get(BETA,root))

    def test_lineage_memo_preserves_depth_limit_for_shared_shortcuts(self):
        leaf=self.capture(request='depth-leaf')['memory_id']
        chain=[leaf]
        for number in range(8):
            chain.append(self.capture(request='depth'+str(number),evidence_refs=[dict(memory_id=chain[-1],revision=1,relationship='supports')])['memory_id'])
        self.assertIsNotNone(self.service.get(BETA,chain[-1]))
        root=self.capture(request='too-deep',evidence_refs=[dict(memory_id=mid,revision=1,relationship='supports') for mid in [chain[1],chain[-1]]])
        self.assertEqual(root['search_visibility'],'ineligible')
        self.assertIsNone(self.service.get(BETA,root['memory_id']))

    def test_recall_budget_is_shared_and_exhaustion_never_returns_partial_results(self):
        first=self.capture(request='first')['memory_id']
        self.capture(request='second')
        with patch('brain_service.service.MAX_LINEAGE_WORK',1):
            self.assertIsNotNone(self.service.get(BETA,first))
            self.assertIsNotNone(self.service.get(BETA,first))
            for operation,args in [('search',{'query':'project'}),('recent',{}),('interests',{})]:
                result=self.gateway.handle_peer(1002,dict(api_version=API_VERSION,operation=operation,arguments=args))
                self.assertEqual(result,{'ok':False,'error':{'code':'general_unavailable'}})
        self.assertEqual(self.service.recent(BETA)['count'],2)
        forged=self.gateway.handle_peer(1002,dict(api_version=API_VERSION,operation='recent',arguments={'budget':1000000}))
        self.assertEqual(forged['error']['code'],'invalid_request')

    def test_lineage_budget_exhaustion_rolls_back_write_and_allows_retry(self):
        mid=self.capture()['memory_id']
        args=self.args('budget-write',evidence_refs=[dict(memory_id=mid,revision=1,relationship='supports')])
        before=self.counts()
        with patch('brain_service.service.MAX_LINEAGE_WORK',1):
            result=self.gateway.handle_peer(1001,dict(api_version=API_VERSION,operation='capture',arguments=args))
            self.assertEqual(result,{'ok':False,'error':{'code':'general_unavailable'}})
        self.assertEqual(self.counts(),before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM brain_general_fts').fetchone()[0],1)
        committed=self.service.capture(ALPHA,args)
        self.assertEqual(committed['search_visibility'],'visible')
        self.assertEqual(committed,self.service.capture(ALPHA,args))
        view=self.capture(DERIVER,request='budget-view',record_class='derived_view',
                          evidence_refs=[dict(memory_id=mid,revision=1,relationship='derived_from')])['memory_id']
        before=self.counts()
        with patch('brain_service.service.MAX_LINEAGE_WORK',0):
            with self.assertRaises(Unavailable):self.revise(mid,{'statement':'Uncommitted replacement'})
        self.assertEqual(self.counts(),before)
        self.assertEqual(self.service.get(BETA,mid)['revision'],1)
        self.assertIsNotNone(self.service.get(BETA,view))

    def test_proposal_and_hermes_hook_budget_exhaustion_roll_back(self):
        mid=self.capture()['memory_id']
        args=dict(request_id='budget-proposal',memory_id=mid,expected_revision=1,
                  statement='Alternative project direction',reason='Differing claim')
        before=self.counts()
        with patch('brain_service.service.MAX_LINEAGE_WORK',2):
            result=self.gateway.handle_peer(1002,dict(api_version=API_VERSION,operation='propose',arguments=args))
            self.assertEqual(result,{'ok':False,'error':{'code':'general_unavailable'}})
        self.assertEqual(self.counts(),before)
        proposed=self.service.propose(BETA,args)
        self.assertEqual(proposed['search_visibility'],'visible')
        self.assertEqual(proposed,self.service.propose(BETA,args))
        before=self.counts()
        with patch('brain_service.service.MAX_LINEAGE_WORK',0):
            with self.assertRaises(Unavailable):
                queries.capture_memory(self.db,type='note',domain='work',content='Synthetic Hermes project',source='manual',request_id='budget-hermes')
        self.assertEqual(self.counts(),before)
        hermes=queries.capture_memory(self.db,type='note',domain='work',content='Synthetic Hermes project',source='manual',request_id='budget-hermes')
        self.assertIsNotNone(self.service.get(BETA,hermes))

    def test_decoded_restricted_text_is_screened_on_admission_and_live_reads(self):
        safe=self.capture()['memory_id']
        cases=[dict(statement='Project\nmedical'),dict(statement='Synthetic credit\tcard detail'),
               dict(title='Project\rmedical'),dict(summary='Synthetic credit\u2003card detail'),
               dict(applicability='Project\nmedical'),dict(conversation_ref='Project\nmedical')]
        for number,extra in enumerate(cases):
            with self.subTest(extra=extra):
                result=self.capture(request='decoded'+str(number),**extra)
                self.assertEqual(result['search_visibility'],'ineligible')
                self.assertIsNone(self.service.get(BETA,result['memory_id']))
        # An old eligible index cannot bypass screening of current decoded nested values.
        for entities in [{'nested':[{'description':'Project\nmedical'}]}, {'Project\nmedical':'value'}]:
            self.db.execute('UPDATE memory_items SET entities=? WHERE id=?',(json.dumps(entities),safe))
            self.assertIsNone(self.service.get(BETA,safe))
            self.assertEqual(self.service.search(BETA,query='project')['count'],0)
            self.assertEqual(self.service.recent(BETA)['total'],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM brain_general_changes').fetchone()[0],1)

    def test_decoded_screening_preserves_general_multiline_text_and_envelope_bound(self):
        result=self.capture(statement='Project notes\nCompiler work\tAgent research',summary='Synthetic general summary')
        item=self.service.get(BETA,result['memory_id'])
        self.assertIsNotNone(item)
        self.assertEqual(self.service.search(BETA,query='compiler')['count'],1)
        self.assertEqual(self.service.recent(BETA)['count'],1)
        state=self.service._metadata(item['id'])
        item['content']=''
        size=len(json.dumps({'item':item,'metadata':state},ensure_ascii=True,sort_keys=True))
        padding=150000-size
        item['content']='x '*(padding//2)+'x'*(padding%2)
        self.assertTrue(general_envelope(item,state,now_ms()))
        item['content']+='x'
        self.assertFalse(general_envelope(item,state,now_ms()))

    def test_decoded_screening_long_general_runs_finish_and_email_detection_is_preserved(self):
        # Bound the test process itself so a regex-performance regression cannot hang CI.
        script="""
from brain_service.policy import general_envelope
item={'domain':'work','status':'active','content':'x'*140000}
state={'sensitivity':'general','expires_at':None}
assert general_envelope(item,state,0)
for address in ['synthetic@example.invalid','...@example.invalid','_+%@example.invalid']:
    item['content']=address
    assert not general_envelope(item,state,0)
"""
        subprocess.run([sys.executable,'-c',script],cwd=ROOT,check=True,capture_output=True,timeout=5)

    def test_active_context_expires_without_creating_owner_preferences(self):
        a=self.capture(record_class='working_context',kind='preference',attribution_basis='user_explicit')
        item=self.service.get(BETA,a['memory_id'])
        self.assertEqual(item['verification'],'client_asserted')
        self.assertAlmostEqual(item['expires_at']-now_ms(),14*86400000,delta=1000)
        self.assertEqual(self.service.interests(BETA)['verified_owner_preferences'],[])
        for i in range(8):self.capture(IMPORTER,request='passive'+str(i),record_class='evidence')
        self.assertEqual(self.service.interests(BETA)['verified_owner_preferences'],[])
        with patch('brain_service.service.now_ms',return_value=item['expires_at']+1):
            self.assertEqual(self.service.interests(BETA)['active_context'],[])

    def test_synthetic_health_and_metadata_canaries_never_affect_general_output(self):
        self.capture();before=self.service.search(BETA,query='project');before_status=self.service.status(BETA)
        for i,extra in enumerate([dict(domain='health',content='SYNTHETIC_BODY_CANARY'),dict(domain='work',content='SYNTHETIC health diagnosis'),dict(domain='work',content='Harmless project',entities={'hidden':'medical record'})]):
            queries.capture_memory(self.db,type='note',domain=extra['domain'],content=extra['content'],entities=extra.get('entities'),source='manual',request_id='canary'+str(i))
        self.capture(request='reference-only',conversation_ref='medical record')
        self.assertEqual(before,self.service.search(BETA,query='project'))
        self.assertEqual(before_status,self.service.status(BETA))
        self.assertEqual(self.db.execute('SELECT count(*) FROM brain_general_fts').fetchone()[0],1)
        self.assertNotIn('CANARY',json.dumps(self.service.recent(BETA)))

    def test_unknown_legacy_rows_remain_quarantined_and_missing_gate_fails_closed(self):
        writer.capture(self.db,consumer_id='old',request_id='legacy',type='note',domain='work',content='Unknown project provenance')
        self.assertEqual(self.service.search(ALPHA,query='project')['count'],0)
        self.db.execute('DROP TABLE brain_records')
        with self.assertRaises(Unavailable):self.service.search(ALPHA,query='project')

    def test_explicit_migration_is_repeatable_and_does_not_admit_existing_data(self):
        migrate(self.path);migrate(self.path)
        self.assertEqual(self.counts(),[0,0,0,0])
        with self.assertRaises(FileNotFoundError):migrate(Path(self.temp.name)/'absent.sqlite')

if __name__=='__main__':unittest.main()
