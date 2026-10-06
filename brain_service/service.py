"""Trusted store component: one owner, bounded general operations, live eligibility.

Clients never receive this connection. Transport adapters resolve principals before
calling it; content, headers and memory records cannot supply capabilities.
"""
import hashlib
import json
import re
import time
from .policy import (API_VERSION, POLICY_VERSION, DOMAINS, CLASSES, RELATIONSHIPS,
                     Principal, Denied, Invalid, Unavailable, topics, general_envelope)
if '.' in __package__:
    from ..store import shared_writer as writer
else:
    from store import shared_writer as writer

ITEM_FIELDS=('id','user_id','type','domain','status','title','summary','content','url',
             'entities','tags','summary_bullets','project_id','quality_score','source',
             'captured_at','event_start','event_end','current_revision')
STATE_FIELDS=('owner_id','creator','current_revision','sensitivity','record_class','kind',
              'verification','topics_json','evidence_json','applicability','expires_at','valid')
WRITE_FIELDS={'request_id','statement','title','summary','domain','topics','record_class','kind',
              'attribution_basis','conversation_ref','evidence_refs','project_id','expires_at','applicability'}
PATCH_FIELDS={'statement','summary','title','domain','topics','project_id','expires_at','applicability','sensitivity','status','kind'}
# Publication recomputes identity, revision and validity; adapters preserve these fields.
METADATA_FIELDS=('sensitivity','record_class','kind','verification','topics','evidence_refs','applicability','expires_at')
MAX_LINEAGE_WORK=4096


class _WorkBudget:
    def __init__(self) -> None:
        self.remaining: int=MAX_LINEAGE_WORK

    def spend(self) -> None:
        if self.remaining<=0:raise Unavailable('Lineage work limit exceeded')
        self.remaining-=1


def available(db):
    required={'brain_records','brain_revision_metadata','brain_links','brain_requests','brain_general_changes','brain_general_index','brain_general_fts'}
    names={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'brain_%'").fetchall()}
    return required<=names

def identifier(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{32}',value):raise Invalid('Invalid memory ID')
    return value

def request_identifier(value):
    if not isinstance(value,str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,120}',value):raise Invalid('Invalid request ID')
    return value


def bounded_text(value,maximum,optional=True):
    if value is None and optional:return None
    if not isinstance(value,str) or len(value)>maximum:raise Invalid('Invalid text')
    return value

def integer(value,low,high):
    if type(value) is not int or not low<=value<=high:raise Invalid('Invalid integer')
    return value

def now_ms():return int(time.time()*1000)


class Service:
    def __init__(self,db,*,owner_id='local'):
        if owner_id!='local':raise Invalid('Only the configured local owner is supported')
        self.db,self.owner_id=db,owner_id

    def _authorize(self,principal,capability):
        if not isinstance(principal,Principal) or principal.owner_id!=self.owner_id or capability not in principal.capabilities:
            raise Denied('Capability denied')
        if not available(self.db):raise Unavailable('Explicit foundation migration required')

    def _item(self,mid):
        row=self.db.execute('SELECT '+','.join(ITEM_FIELDS)+' FROM memory_items WHERE id=?',(mid,)).fetchone()
        if row is None:return None
        item=dict(zip(ITEM_FIELDS,row))
        for field,default in [('tags',[]),('entities',{}),('summary_bullets',[])]:
            item[field]=json.loads(item[field]) if item[field] else default
        return item

    def _metadata(self,mid):
        row=self.db.execute('SELECT '+','.join(STATE_FIELDS)+' FROM brain_records WHERE memory_id=?',(mid,)).fetchone()
        if row is None:return None
        state=dict(zip(STATE_FIELDS,row))
        state['topics']=json.loads(state.pop('topics_json'))
        state['evidence_refs']=json.loads(state.pop('evidence_json'))
        return state

    def _eligible(self,item,state,*,budget=None):
        budget=budget or _WorkBudget()
        records={item['id']:(item,state)} if item else {}
        memo: dict[tuple[str,int,int],bool]={}

        def visit(current,metadata,depth):
            budget.spend()
            if not current or not metadata or depth>8:return False
            # Depth belongs in the key: a shallow visit cannot authorize a deeper path.
            key=(current['id'],current['current_revision'],depth)
            if key in memo:return memo[key]
            eligible=(current['user_id']==self.owner_id and metadata['owner_id']==self.owner_id
                      and current['current_revision']==metadata['current_revision']
                      and (metadata['record_class']!='derived_view' or bool(metadata['evidence_refs']))
                      and general_envelope(current,metadata,now_ms()))
            if eligible:
                for ref in metadata['evidence_refs']:
                    budget.spend()
                    mid=ref['memory_id']
                    if mid not in records:records[mid]=(self._item(mid),self._metadata(mid))
                    target,target_state=records[mid]
                    if not target or target['current_revision']!=ref['revision'] or not visit(target,target_state,depth+1):
                        eligible=False
                        break
            memo[key]=eligible
            return eligible

        # Cache only this traversal. Later reads and in-transaction rechecks stay live.
        return visit(item,state,0)

    def _result(self,item,state):
        # The current audit is server-recorded attribution, never authenticated human authorship.
        audit=self.db.execute('SELECT consumer_id,entry_kind,attribution_basis,conversation_ref,created_at FROM memory_write_audit WHERE memory_id=? AND revision_n=?',
                              (item['id'],item['current_revision'])).fetchone()
        if audit is None:return None
        result=dict(item,owner_id=self.owner_id,revision=item['current_revision'],
                    record_class=state['record_class'],sensitivity=state['sensitivity'],
                    topics=state['topics'],kind=state['kind'],verification=state['verification'],
                    applicability=state['applicability'],expires_at=state['expires_at'],evidence_refs=state['evidence_refs'],
                    provenance=dict(capture_origin=item['source'],capturing_actor=state['creator'],editing_actor=audit[0],
                                    attribution_basis=audit[2],conversation_ref=audit[3],created_at=audit[4]))
        if not general_envelope(result,state,now_ms()):return None
        return result

    def get(self,principal,mid):
        return self._get(principal,mid,_WorkBudget())

    def _get(self,principal,mid,budget):
        self._authorize(principal,'read');identifier(mid)
        item,state=self._item(mid),self._metadata(mid)
        return self._result(item,state) if self._eligible(item,state,budget=budget) else None

    def status(self,principal):
        self._authorize(principal,'read')
        sequence=self.db.execute('SELECT coalesce(max(sequence),0) FROM brain_general_changes').fetchone()[0]
        return dict(api_version=API_VERSION,policy_version=POLICY_VERSION,commit_sequence=sequence,
                    indexed_sequence=sequence,indexing='synchronous',owner_id=self.owner_id)

    def _references(self,principal,refs,budget):
        if not isinstance(refs,list) or len(refs)>16:raise Invalid('Invalid evidence references')
        result=[]
        for ref in refs:
            if not isinstance(ref,dict) or set(ref)!={'memory_id','revision','relationship'}:raise Invalid('Invalid evidence reference')
            identifier(ref['memory_id']);integer(ref['revision'],1,2**63-1)
            if ref['relationship'] not in RELATIONSHIPS:raise Invalid('Invalid relationship')
            target=self._get(principal,ref['memory_id'],budget)
            if not target or target['revision']!=ref['revision']:raise Denied('Reference unavailable')
            result.append(dict(ref))
        return result

    def _receipt(self,principal,operation,args):
        """Hash the original API request so replay survives translated fields and changed inputs."""
        request=request_identifier(args.get('request_id'))
        try:serialized=json.dumps({'operation':operation,'arguments':args},sort_keys=True,allow_nan=False,separators=(',',':'))
        except (ValueError,TypeError,RecursionError):raise Invalid('Invalid request') from None
        if len(serialized)>150000:raise Invalid('Request exceeds limit')
        digest=hashlib.sha256(serialized.encode()).hexdigest()
        previous=self.db.execute('SELECT digest,result_json FROM brain_requests WHERE principal=? AND request_id=?',(principal.id,request)).fetchone()
        if previous:
            if previous[0]!=digest:raise writer.IdempotencyConflict()
            return request,digest,json.loads(previous[1])
        return request,digest,None

    def _hook(self,principal,metadata,reason,request=None,digest=None,*,budget=None,publication_hook=None):
        budget=budget or _WorkBudget()
        def persist(result,now):
            mid,revision=result['memory_id'],result['revision']
            old=self._metadata(mid)
            item=self._item(mid)
            if old and old['sensitivity']=='general' and item['current_revision']>1:
                # Any changed input invalidates derived views conservatively before publication.
                self.db.execute("UPDATE brain_records SET valid=0 WHERE record_class='derived_view'")
                self.db.execute("DELETE FROM brain_general_fts WHERE memory_id IN (SELECT memory_id FROM brain_records WHERE valid=0)")
                self.db.execute("DELETE FROM brain_general_index WHERE memory_id IN (SELECT memory_id FROM brain_records WHERE valid=0)")
            state=dict(metadata,owner_id=self.owner_id,creator=old['creator'] if old else principal.id,
                       current_revision=revision,valid=1)
            audit=self.db.execute('SELECT consumer_id,entry_kind,attribution_basis,conversation_ref,created_at FROM memory_write_audit WHERE memory_id=? AND revision_n=?',(mid,revision)).fetchone()
            provenance=dict(actor=audit[0],kind=audit[1],attribution_basis=audit[2],conversation_ref=audit[3],created_at=audit[4])
            if not general_envelope(dict(item,provenance=provenance,status='active'),dict(state,expires_at=None,valid=True),now):
                # Classification may increase restrictions; it never declassifies uncertain input.
                if state['sensitivity']=='general':state['sensitivity']='unclassified'
            for ref in state['evidence_refs'] if item['status']=='active' else []:
                target=self._item(ref['memory_id']);target_state=self._metadata(ref['memory_id'])
                if not target or target['current_revision']!=ref['revision'] or not self._eligible(target,target_state,budget=budget):
                    raise Denied('Input revision changed')
            self.db.execute('INSERT OR REPLACE INTO brain_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (mid,self.owner_id,state['creator'],revision,state['sensitivity'],state['record_class'],state['kind'],state['verification'],
                 json.dumps(state['topics']),json.dumps(state['evidence_refs']),state['applicability'],state['expires_at'],state['valid']))
            snapshot=dict(item=item,metadata=state,provenance=provenance)
            self.db.execute('INSERT INTO brain_revision_metadata VALUES(?,?,?,?,?,?)',
                            (mid,revision,principal.id,reason,json.dumps(snapshot,sort_keys=True),now))
            for ref in state['evidence_refs']:
                self.db.execute('INSERT INTO brain_links VALUES(?,?,?,?,?)',(mid,revision,ref['memory_id'],ref['revision'],ref['relationship']))
            self.db.execute('DELETE FROM brain_general_fts WHERE memory_id=?',(mid,))
            self.db.execute('DELETE FROM brain_general_index WHERE memory_id=?',(mid,))
            indexed=False
            if self._eligible(item,state,budget=budget):
                document=self._result(item,state)
                if document is not None:
                    indexed=True
                    self.db.execute('INSERT INTO brain_general_index VALUES(?,?,?)',(mid,revision,json.dumps(document,sort_keys=True)))
                    self.db.execute('INSERT INTO brain_general_fts VALUES(?,?,?,?,?)',(mid,item['title'] or '',item['summary'] or '',item['content'] or '', ' '.join(state['topics'])))
            sequence=None
            if state['sensitivity']=='general' or (old and old['sensitivity']=='general'):
                self.db.execute('INSERT INTO brain_general_changes(memory_id,revision_n,operation) VALUES(?,?,?)',(mid,revision,'capture' if revision==1 else 'revise'))
                sequence=self.db.execute('SELECT max(sequence) FROM brain_general_changes').fetchone()[0]
            result.update(commit_sequence=sequence,durability='committed',search_visibility='visible' if indexed else 'ineligible')
            # Extensions and their receipt fields share this transaction and roll back together.
            if publication_hook is not None:publication_hook(result,now)
            if request is not None:
                self.db.execute('INSERT INTO brain_requests VALUES(?,?,?,?)',(principal.id,request,digest,json.dumps(result,sort_keys=True)))
        return persist

    def capture(self,principal,args,*,receipt_operation="capture",receipt_args=None,budget=None,publication_hook=None):
        self._authorize(principal,'capture')
        if not isinstance(args,dict) or set(args)-WRITE_FIELDS:raise Invalid('Unexpected capture fields')
        request,digest,previous=self._receipt(principal,receipt_operation,receipt_args if receipt_args is not None else args)
        if previous:return previous
        budget=budget or _WorkBudget()
        statement=bounded_text(args.get('statement'),100000,False)
        if not statement.strip():raise Invalid('Empty statement')
        domain=args.get('domain')
        if domain not in DOMAINS:raise Denied('Domain unavailable')
        record_class=args.get('record_class','assertion')
        if record_class not in CLASSES:raise Invalid('Invalid record class')
        if record_class=='evidence' and 'import' not in principal.capabilities:raise Denied('Import capability required')
        if record_class=='derived_view' and 'derive' not in principal.capabilities:raise Denied('Derivation capability required')
        refs=self._references(principal,args.get('evidence_refs',[]),budget)
        if record_class=='derived_view' and (not refs or any(r['relationship']!='derived_from' for r in refs)):raise Invalid('Derived inputs required')
        kind=args.get('kind','project_context' if record_class=='working_context' else 'idea')
        if kind not in writer.KINDS:raise Invalid('Invalid kind')
        basis=args.get('attribution_basis','assistant_inferred')
        if basis not in writer.BASES:raise Invalid('Invalid attribution')
        expiry=args.get('expires_at')
        if expiry is not None:integer(expiry,1,2**63-1)
        if record_class=='working_context' and expiry is None:expiry=now_ms()+14*86400000
        metadata=dict(sensitivity='general',record_class=record_class,kind=kind,
                      verification='source_observation' if record_class=='evidence' else 'client_asserted',
                      topics=topics(args.get('topics',[])),evidence_refs=refs,
                      applicability=bounded_text(args.get('applicability'),256),expires_at=expiry)
        return writer.capture(self.db,consumer_id=principal.id,request_id='v1:'+request,type='note',domain=domain,
            content=statement,title=bounded_text(args.get('title'),300),summary=bounded_text(args.get('summary'),2000),
            source=principal.source_namespace,project_id=bounded_text(args.get('project_id'),128),tags=metadata['topics'],
            entry_kind=kind,attribution_basis=basis,conversation_ref=bounded_text(args.get('conversation_ref'),128),
            extra_payload=args,revision_hook=self._hook(principal,metadata,'capture',request,digest,budget=budget,publication_hook=publication_hook))

    def revise(self,principal,args,*,retract=False):
        self._authorize(principal,'retract_own' if retract else 'revise_own')
        allowed={'request_id','memory_id','expected_revision','reason'} if retract else {'request_id','memory_id','expected_revision','reason','patch','evidence_refs','attribution_basis','conversation_ref'}
        if not isinstance(args,dict) or set(args)-allowed:raise Invalid('Unexpected revision fields')
        request,digest,previous=self._receipt(principal,'retract' if retract else 'revise',args)
        if previous:return previous
        budget=_WorkBudget()
        mid=identifier(args.get('memory_id'));integer(args.get('expected_revision'),1,2**63-1)
        reason=bounded_text(args.get('reason'),512,False)
        if not reason.strip():raise Invalid('Reason required')
        old,item=self._metadata(mid),self._item(mid)
        if not old or not item or old['owner_id']!=self.owner_id or old['creator']!=principal.id or old['record_class']=='evidence':raise Denied('Item unavailable for revision')
        patch={'status':'retracted'} if retract else args.get('patch')
        if not isinstance(patch,dict) or not patch or set(patch)-PATCH_FIELDS:raise Invalid('Invalid patch')
        metadata={k:old[k] for k in METADATA_FIELDS}
        if 'sensitivity' in patch:
            if patch['sensitivity'] not in {'general','restricted_health','unclassified'}:raise Invalid('Invalid sensitivity')
            if patch['sensitivity']=='general' and old['sensitivity']!='general':raise Denied('Declassification unavailable')
            metadata['sensitivity']=patch['sensitivity']
        if 'kind' in patch:
            if patch['kind'] not in writer.KINDS:raise Invalid('Invalid kind')
            metadata['kind']=patch['kind']
        basis=args.get('attribution_basis','user_corrected')
        if basis not in writer.BASES:raise Invalid('Invalid attribution')
        conversation_ref=bounded_text(args.get('conversation_ref'),128)
        if 'topics' in patch:metadata['topics']=topics(patch['topics'])
        if 'expires_at' in patch:
            if patch['expires_at'] is not None:integer(patch['expires_at'],1,2**63-1)
            metadata['expires_at']=patch['expires_at']
        if 'applicability' in patch:metadata['applicability']=bounded_text(patch['applicability'],256)
        if 'evidence_refs' in args:metadata['evidence_refs']=self._references(principal,args['evidence_refs'],budget)
        if patch.get('domain',item['domain']) not in DOMAINS:raise Denied('Domain unavailable')
        if patch.get('status','active') not in {'active','archived','retracted'}:raise Invalid('Invalid status')
        if patch.get('status')=='retracted':self._authorize(principal,'retract_own')
        for field,limit in [('statement',100000),('title',300),('summary',2000),('project_id',128)]:
            if field in patch:bounded_text(patch[field],limit,optional=field!='statement')
        def persist(result,now):
            for field in ['title','domain','project_id','status']:
                if field in patch:self.db.execute('UPDATE memory_items SET '+field+'=? WHERE id=?',(patch[field],mid))
            self.db.execute('UPDATE memory_items SET tags=? WHERE id=?',(json.dumps(metadata['topics']),mid))
            self._hook(principal,metadata,reason,request,digest,budget=budget)(result,now)
        return writer.update(self.db,consumer_id=principal.id,request_id='v1:'+request,memory_id=mid,
            expected_revision=args['expected_revision'],content=patch.get('statement',item['content']),
            summary=patch.get('summary',item['summary']),source=principal.source_namespace,
            entry_kind=metadata['kind'],attribution_basis=basis,conversation_ref=conversation_ref,extra_payload=args,revision_hook=persist)

    def propose(self,principal,args):
        self._authorize(principal,'propose')
        if not isinstance(args,dict) or set(args)-{'request_id','memory_id','expected_revision','statement','reason'}:raise Invalid('Invalid proposal')
        _,_,previous=self._receipt(principal,'propose',args)
        if previous:return previous
        budget=_WorkBudget()
        target=self._get(principal,identifier(args.get('memory_id')),budget)
        if not target or target['revision']!=args.get('expected_revision'):raise Denied('Target unavailable')
        capture=dict(request_id=args.get('request_id'),statement=args.get('statement'),domain=target['domain'],
                     record_class='assertion',kind='idea',attribution_basis='assistant_inferred',
                     evidence_refs=[dict(memory_id=target['id'],revision=target['revision'],relationship='contradicts')],
                     applicability=bounded_text(args.get('reason'),256,False))
        return self.capture(principal,capture,receipt_operation="propose",receipt_args=args,budget=budget)

    def recent(self,principal,**filters):
        return self._recent(principal,_WorkBudget(),**filters)

    def _recent(self,principal,budget,*,hours=24,limit=20,offset=0,domain=None,source=None,topic=None,project_id=None,type=None,min_score=None,order='captured_at',record_class=None):
        self._authorize(principal,'read');integer(hours,1,2160);integer(limit,1,100);integer(offset,0,1000)
        if domain is not None and domain not in DOMAINS:raise Denied('Domain unavailable')
        if record_class is not None and record_class not in CLASSES:raise Invalid('Invalid record class')
        for value in [source,project_id]:bounded_text(value,128)
        if topic is not None:topic=topics([topic])[0]
        if type is not None and type not in writer.TYPES:raise Invalid('Invalid type')
        if order not in {'captured_at','score'}:raise Invalid('Invalid order')
        if min_score is not None and (isinstance(min_score,bool) or not isinstance(min_score,(int,float)) or not 0<=min_score<=1):raise Invalid('Invalid score')
        where=["r.owner_id=?","r.sensitivity='general'","r.valid=1","i.status='active'","i.current_revision=r.current_revision","x.revision_n=i.current_revision","i.captured_at>=?"]
        params=[self.owner_id,now_ms()-hours*3600000]
        if domain is not None:where.append('i.domain=?');params.append(domain)
        if source is not None:where.append('substr(i.source,1,?)=?');params.extend([len(source.rstrip('%')),source.rstrip('%')])
        if project_id is not None:where.append('i.project_id=?');params.append(project_id)
        if type is not None:where.append('i.type=?');params.append(type)
        if record_class is not None:where.append('r.record_class=?');params.append(record_class)
        if min_score is not None:where.append('i.quality_score>=?');params.append(min_score)
        ordering='i.captured_at DESC,i.id ASC' if order=='captured_at' else 'i.quality_score IS NULL,i.quality_score DESC,i.captured_at DESC,i.id ASC'
        rows=self.db.execute('SELECT i.id FROM memory_items i JOIN brain_records r ON r.memory_id=i.id JOIN brain_general_index x ON x.memory_id=i.id WHERE '+' AND '.join(where)+' ORDER BY '+ordering+' LIMIT 2000',tuple(params)).fetchall()
        visible=[]
        for row in rows:
            item=self._get(principal,row[0],budget)
            if item is not None and (topic is None or topic in item['topics']):visible.append(item)
        return dict(items=visible[offset:offset+limit],count=len(visible[offset:offset+limit]),total=len(visible),
                    candidate_window_exhausted=len(rows)==2000,total_is_complete=len(rows)<2000,has_more=offset+limit<len(visible),
                    next_offset=offset+limit if offset+limit<len(visible) else None)

    def search(self,principal,*,query,limit=10,domain=None,source=None,topic=None,project_id=None,type=None,min_commit_sequence=None,record_class=None):
        self._authorize(principal,'read');bounded_text(query,256,False);integer(limit,1,100)
        tokens=re.findall(r'[\w-]+',query)
        if not tokens or len(tokens)>8 or any(len(t)>64 for t in tokens):raise Invalid('Invalid query')
        status=self.status(principal)
        if min_commit_sequence is not None:
            integer(min_commit_sequence,0,2**63-1)
            if status['indexed_sequence']<min_commit_sequence:return dict(items=[],count=0,indexing_pending=True,**status)
        # Dedicated general-only FTS provides matching IDs. No global vector/FTS candidates or corpus statistics.
        match=' OR '.join('"'+token.replace('"','""')+'"' for token in tokens)
        rows=self.db.execute("SELECT f.memory_id FROM brain_general_fts f JOIN brain_records r ON r.memory_id=f.memory_id JOIN memory_items i ON i.id=f.memory_id JOIN brain_general_index x ON x.memory_id=f.memory_id WHERE brain_general_fts MATCH ? AND r.owner_id=? AND r.sensitivity='general' AND r.valid=1 AND i.status='active' AND i.current_revision=r.current_revision AND x.revision_n=i.current_revision ORDER BY i.captured_at DESC,i.id ASC LIMIT 2000",(match,self.owner_id)).fetchall()
        if domain is not None and domain not in DOMAINS:raise Denied('Domain unavailable')
        if record_class is not None and record_class not in CLASSES:raise Invalid('Invalid record class')
        if topic is not None:topic=topics([topic])[0]
        bounded_text(source,128);bounded_text(project_id,128)
        budget=_WorkBudget()
        visible=[]
        for row in rows:
            item=self._get(principal,row[0],budget)
            if item is None or (domain is not None and item['domain']!=domain) or (source is not None and not (item['source'] or '').startswith(source.rstrip('%'))) or (topic is not None and topic not in item['topics']) or (project_id is not None and item['project_id']!=project_id) or (type is not None and item['type']!=type) or (record_class is not None and item['record_class']!=record_class):continue
            text=' '.join(str(item.get(k) or '') for k in ['title','summary','content']).lower()
            lexical=sum(1 for token in tokens if token.lower() in text)
            active_boost=.1 if item['record_class']=='working_context' and (topic or project_id) else 0
            item=dict(item,relevance_score=lexical+active_boost,matched_topics=[topic] if topic else [])
            visible.append(item)
        visible.sort(key=lambda x:(-x['relevance_score'],-x['captured_at'],x['id']))
        return dict(items=visible[:limit],count=min(len(visible),limit),candidate_window_exhausted=len(rows)==2000,indexing_pending=False,**status)

    def interests(self,principal):
        budget=_WorkBudget()
        rows=self._recent(principal,budget,hours=2160,limit=100,record_class='working_context')['items']
        active=[dict(memory_id=r['id'],revision=r['revision'],topics=r['topics'],expires_at=r['expires_at'],verification=r['verification']) for r in rows if r['record_class']=='working_context']
        # No inference threshold or feed/view count creates an owner preference.
        assertions=self._recent(principal,budget,hours=2160,limit=100,record_class='assertion')['items']
        verified=[dict(memory_id=r['id'],revision=r['revision'],topics=r['topics']) for r in assertions if r['kind']=='preference' and r['verification']=='owner_verified']
        return dict(active_context=active,verified_owner_preferences=verified)
