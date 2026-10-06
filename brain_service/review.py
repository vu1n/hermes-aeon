"""Immutable review attempts/decisions; historical status never authorizes live recall."""
import hashlib
import json
from .consolidation import Consolidation, encoded
from .policy import Principal, Denied, Invalid, Unavailable
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

    def candidate_ref(self,principal: Principal,mid: str,budget: _WorkBudget):
        item=self.service._get(principal,mid,budget)
        return dict(memory_id=mid,revision=item['revision']) if item is not None else None

    def history(self,principal,args):
        self.ready(principal,'review');self.service._authorize(principal,'read')
        if not isinstance(args,dict) or set(args)!={'attempt_id'}:raise Invalid('Invalid history arguments')
        aid=args['attempt_id'];attempt=self.attempt(aid)
        decision=self.db.execute('SELECT reviewer,decision,reason_code,expected_revision,created_at FROM brain_review_decisions WHERE attempt_id=?',(aid,)).fetchone()
        candidate=self.candidate_ref(principal,attempt['memory_id'],_WorkBudget())
        # No source text, titles, applicability or pins escape revoked memory via history.
        return dict(attempt_id=aid,parent_attempt=attempt['parent_attempt'],creator=attempt['creator'],created_at=attempt['created_at'],
            state=decision[1] if decision else 'pending',decision=dict(zip(['reviewer','decision','reason_code','expected_revision','created_at'],decision)) if decision else None,
            live_eligible=candidate is not None,candidate_ref=candidate)

    def pending(self,principal,args):
        self.ready(principal,'review');self.service._authorize(principal,'read')
        if not isinstance(args,dict) or set(args)-{'limit','after'}:raise Invalid('Invalid pending arguments')
        limit=integer(args.get('limit',20),1,50);after=args.get('after','')
        if not isinstance(after,str) or len(after)>52:raise Invalid('Invalid cursor')
        stamp,aid=0,''
        if after:
            parts=after.split(':')
            if len(parts)!=2 or not parts[0].isascii() or not parts[0].isdigit():raise Invalid('Invalid cursor')
            stamp=integer(int(parts[0]),0,2**63-1);aid=identifier(parts[1])
        rows=self.db.execute("""SELECT a.attempt_id,a.memory_id,a.created_at,a.creator,a.candidate_id,a.pins_json,a.parent_attempt
            FROM brain_review_attempts a LEFT JOIN brain_review_decisions d ON d.attempt_id=a.attempt_id
            WHERE d.attempt_id IS NULL AND (a.created_at>? OR (a.created_at=? AND a.attempt_id>?))
            ORDER BY a.created_at,a.attempt_id LIMIT ?""",(stamp,stamp,aid,limit+1)).fetchall()
        budget=_WorkBudget();items=[];groups={}
        for attempt_id,mid,created_at,creator,candidate_id,pins,parent in rows[:limit]:
            candidate=self.candidate_ref(principal,mid,budget)
            peers=[]
            if candidate is not None:
                key=(creator,candidate_id,parent,pins)
                if key not in groups:
                    # Two indexed matches flag duplicates without counting the entire queue.
                    groups[key]=self.db.execute("""SELECT a.attempt_id FROM brain_review_attempts a
                        LEFT JOIN brain_review_decisions d ON d.attempt_id=a.attempt_id
                        WHERE a.creator=? AND a.candidate_id=? AND a.parent_attempt IS ? AND a.pins_json=?
                            AND d.attempt_id IS NULL ORDER BY a.created_at,a.attempt_id LIMIT 2""",key).fetchall()
                peers=groups[key]
            items.append(dict(attempt_id=attempt_id,created_at=created_at,live_eligible=candidate is not None,
                candidate_ref=candidate,has_duplicates=len(peers)>1,
                duplicate_group=peers[0][0] if peers else None))
        return dict(items=items,next_after=f"{rows[limit-1][2]}:{rows[limit-1][0]}" if len(rows)>limit else None)

    def decide(self,principal,args):
        self.ready(principal,'review');self.service._authorize(principal,'read')
        if not isinstance(args,dict) or not {'request_id','attempt_id','decision','reason_code'}<=set(args) or set(args)-{'request_id','attempt_id','expected_revision','decision','reason_code'}:raise Invalid('Invalid decision')
        request=request_identifier(args['request_id'])
        aid=identifier(args['attempt_id'])
        revision=args.get('expected_revision')
        if 'expected_revision' in args:integer(revision,1,2**63-1)
        if args['decision']=='accepted' and revision is None:raise Invalid('Acceptance requires expected revision')
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
                item=self.service.get(principal,attempt['memory_id'])
                if revision is None:
                    # Closing unavailable work is attempt-scoped; no hidden revision is exported.
                    if item is not None:raise Denied('Eligible rejection requires expected revision')
                    reviewed_revision=0
                else:
                    if item is None:raise Denied('Unavailable rejection must omit expected revision')
                    if item['revision']!=revision:raise Conflict(item['revision'])
                    reviewed_revision=revision
                if args['decision']=='accepted':
                    if item is None or item['revision']!=revision or encoded(item['evidence_refs'])!=attempt['pins_json'] or hashlib.sha256(item['content'].encode()).hexdigest()!=attempt['candidate_id']:
                        raise Denied('Candidate changed or unavailable')
                self.db.execute('INSERT INTO brain_review_decisions VALUES(?,?,?,?,?,?,?,?)',(aid,principal.id,request,digest,args['decision'],args['reason_code'],reviewed_revision,now_ms()))
                result=dict(attempt_id=aid,state=args['decision'],historical=True)
            self.db.commit();started=False
            return result
        except Exception:
            if started:self.db.rollback()
            raise
