"""Immutable review attempts/decisions; historical status never authorizes live recall."""
import hashlib
import json
from .consolidation import Consolidation, encoded
from .policy import Denied, Invalid, Unavailable
from .service import Service, _WorkBudget, identifier, integer, now_ms, request_identifier
if '.' in __package__:
    from ..store.shared_writer import Conflict, IdempotencyConflict
else:
    from store.shared_writer import Conflict, IdempotencyConflict

REASONS=frozenset({'faithful','conflict','outdated','duplicate','irrelevant'})

class Reviews:
    def __init__(self,service: Service) -> None:
        self.service,self.db=service,service.db

    def ready(self,principal,capability):
        self.service._authorize(principal,capability)
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE name IN ('brain_review_attempts','brain_review_decisions')")}
        if len(tables)!=2:raise Unavailable('Explicit review migration required')

    def attempt(self,aid):
        identifier(aid)
        row=self.db.execute('SELECT memory_id,parent_attempt,creator,candidate_id,pins_json,created_at FROM brain_review_attempts WHERE attempt_id=?',(aid,)).fetchone()
        if not row:raise Denied('Attempt unavailable')
        return dict(zip(['memory_id','parent_attempt','creator','candidate_id','pins_json','created_at'],row))

    def stage(self,principal,args):
        self.ready(principal,'derive')
        if not isinstance(args,dict) or set(args)-{'request_id','source_refs','expected_candidate_id','parent_attempt'}:raise Invalid('Invalid review stage')
        request=request_identifier(args.get('request_id'))
        aid=hashlib.sha256(encoded([principal.id,request]).encode()).hexdigest()[:32]
        receipt_args=dict(args,request_id='review:'+aid)
        self.service._authorize(principal,'capture');self.service._authorize(principal,'read')
        _,_,previous=self.service._receipt(principal,'review_stage',receipt_args)
        if previous:return previous
        preview=Consolidation(self.service).preview(principal,dict(source_refs=args.get('source_refs')))
        if preview['candidate_id']!=args.get('expected_candidate_id'):raise Denied('Candidate changed')
        parent=args.get('parent_attempt')
        if parent is not None:
            old=self.attempt(parent)
            if old['creator']!=principal.id:raise Denied('Regenerate own attempts only')
            if {r['memory_id'] for r in json.loads(old['pins_json'])}!={r['memory_id'] for r in preview['candidate']['source_refs']}:
                raise Denied('Regeneration must preserve original identities')
        def publish(result,now):
            self.db.execute('INSERT INTO brain_review_attempts VALUES(?,?,?,?,?,?,?)',
                (aid,result['memory_id'],parent,principal.id,preview['candidate_id'],encoded(preview['candidate']['source_refs']),now))
            result['attempt_id']=aid
        return Consolidation(self.service).capture_candidate(principal,preview,'review:'+aid,publication_hook=publish,receipt_args=receipt_args)

    def history(self,principal,args):
        self.ready(principal,'review');self.service._authorize(principal,'read')
        if not isinstance(args,dict) or set(args)!={'attempt_id'}:raise Invalid('Invalid history arguments')
        aid=args['attempt_id'];attempt=self.attempt(aid)
        decision=self.db.execute('SELECT reviewer,decision,reason_code,expected_revision,created_at FROM brain_review_decisions WHERE attempt_id=?',(aid,)).fetchone()
        live=self.service.get(principal,attempt['memory_id'])
        # No source text, titles, applicability or pins escape revoked memory via history.
        return dict(attempt_id=aid,parent_attempt=attempt['parent_attempt'],creator=attempt['creator'],created_at=attempt['created_at'],
            state=decision[1] if decision else 'pending',decision=dict(zip(['reviewer','decision','reason_code','expected_revision','created_at'],decision)) if decision else None,
            live_eligible=live is not None)

    def pending(self,principal,args):
        self.ready(principal,'review');self.service._authorize(principal,'read')
        if not isinstance(args,dict) or set(args)-{'limit','after'}:raise Invalid('Invalid pending arguments')
        limit=integer(args.get('limit',20),1,50);after=args.get('after','')
        if not isinstance(after,str):raise Invalid('Invalid cursor')
        if after:identifier(after)
        rows=self.db.execute('SELECT a.attempt_id,a.memory_id FROM brain_review_attempts a LEFT JOIN brain_review_decisions d ON d.attempt_id=a.attempt_id WHERE d.attempt_id IS NULL AND a.attempt_id>? ORDER BY a.attempt_id LIMIT ?', (after,limit+1)).fetchall()
        budget=_WorkBudget()
        items=[dict(attempt_id=aid,live_eligible=self.service._get(principal,mid,budget) is not None) for aid,mid in rows[:limit]]
        return dict(items=items,next_after=items[-1]['attempt_id'] if len(rows)>limit else None)

    def decide(self,principal,args):
        self.ready(principal,'review');self.service._authorize(principal,'read')
        if not isinstance(args,dict) or set(args)!={'request_id','attempt_id','expected_revision','decision','reason_code'}:raise Invalid('Invalid decision')
        request=request_identifier(args['request_id'])
        aid=identifier(args['attempt_id']);integer(args['expected_revision'],1,2**63-1)
        if args['decision'] not in {'accepted','rejected'} or args['reason_code'] not in REASONS:raise Invalid('Invalid decision reason')
        digest=hashlib.sha256(encoded(args).encode()).hexdigest()
        started=False
        try:
            self.db.execute('BEGIN IMMEDIATE');started=True
            previous=self.db.execute('SELECT attempt_id,digest,decision FROM brain_review_decisions WHERE reviewer=? AND request_id=?',(principal.id,request)).fetchone()
            if previous:
                if previous[0]!=aid or previous[1]!=digest:raise IdempotencyConflict()
                result=dict(attempt_id=aid,state=previous[2],historical=True)
            else:
                attempt=self.attempt(aid)
                if self.db.execute('SELECT 1 FROM brain_review_decisions WHERE attempt_id=?',(aid,)).fetchone():raise Conflict(1)
                current=self.db.execute('SELECT current_revision FROM memory_items WHERE id=?',(attempt['memory_id'],)).fetchone()
                if not current:raise Denied('Candidate unavailable')
                if current[0]!=args['expected_revision']:raise Conflict(current[0])
                if args['decision']=='accepted':
                    item=self.service.get(principal,attempt['memory_id'])
                    if item is None or item['revision']!=args['expected_revision'] or encoded(item['evidence_refs'])!=attempt['pins_json'] or hashlib.sha256(item['content'].encode()).hexdigest()!=attempt['candidate_id']:
                        raise Denied('Candidate changed or unavailable')
                self.db.execute('INSERT INTO brain_review_decisions VALUES(?,?,?,?,?,?,?,?)',(aid,principal.id,request,digest,args['decision'],args['reason_code'],args['expected_revision'],now_ms()))
                result=dict(attempt_id=aid,state=args['decision'],historical=True)
            self.db.commit();started=False
            return result
        except Exception:
            if started:self.db.rollback()
            raise
