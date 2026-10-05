"""Manual extractive candidate generation. No model, promotion or source mutation."""
import hashlib
import json
import time
from .policy import Principal, Denied, Invalid, Unavailable, general_envelope
from .service import Service, _WorkBudget, identifier, integer, now_ms

VERSION='consolidation.v1'
MAX_SOURCES=16
MAX_TEXT=20000
MAX_OUTPUT=16000
MAX_SECONDS=2


def encoded(value: object) -> str:
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)


class Consolidation:
    def __init__(self,service: Service) -> None:
        self.service=service

    def preview(self,principal: Principal,args):
        if not isinstance(args,dict) or set(args)!={'source_refs'}:raise Invalid('Invalid consolidation arguments')
        refs=args['source_refs']
        if not isinstance(refs,list) or not 1<=len(refs)<=MAX_SOURCES:raise Invalid('Invalid sources')
        deadline=time.monotonic()+MAX_SECONDS
        budget=_WorkBudget()
        records={}
        retained={}
        conflicts=[]
        consumed=0

        def visit(ref,path):
            nonlocal consumed
            if time.monotonic()>deadline:raise Unavailable('Consolidation time limit')
            budget.spend()
            if not isinstance(ref,dict) or set(ref)!={'memory_id','revision'}:raise Invalid('Invalid source reference')
            mid=identifier(ref['memory_id']);revision=integer(ref['revision'],1,2**63-1)
            if mid in path or len(path)>8:raise Denied('Circular or deep lineage')
            if mid in records:
                if records[mid]['revision']!=revision:raise Denied('Inconsistent source revision')
                return
            if len(records)>=MAX_SOURCES:raise Unavailable('Consolidation source limit')
            item=self.service._get(principal,mid,budget)
            if item is None or item['revision']!=revision:raise Denied('Source unavailable')
            consumed+=len(item['content'] or '')
            if consumed>MAX_TEXT:raise Unavailable('Consolidation text limit')
            records[mid]=item
            # Derived repetitions add no claim or evidence: traverse their original inputs.
            if item['record_class']!='derived_view':retained[mid]=item
            for link in item['evidence_refs']:
                if item['record_class']=='derived_view' and link['relationship']!='derived_from':raise Denied('Uncertified derivation')
                if link['relationship']=='contradicts':
                    conflicts.append(dict(claim=dict(memory_id=mid,revision=revision),target=dict(memory_id=link['memory_id'],revision=link['revision']),resolution='unresolved'))
                visit(dict(memory_id=link['memory_id'],revision=link['revision']),path|{mid})

        for ref in sorted(refs,key=encoded):visit(ref,set())
        for mid,item in records.items():
            current=self.service._get(principal,mid,budget)
            if current is None or current['revision']!=item['revision']:raise Denied('Source changed during preview')
        if time.monotonic()>deadline:raise Unavailable('Consolidation time limit')
        if not retained:raise Denied('Original sources required')
        domains={item['domain'] for item in retained.values()}
        if len(domains)!=1:raise Invalid('Consolidate one domain at a time')
        groups={}
        for mid,item in sorted(retained.items()):
            normalized=' '.join((item['content'] or '').split())
            group=groups.setdefault(normalized,dict(excerpt=normalized[:500],excerpt_truncated=len(normalized)>500,sources=[]))
            # Different actors and duplicate text cannot establish independent evidence.
            group['sources'].append(dict(memory_id=mid,revision=item['revision'],actor=item['provenance']['capturing_actor'],
                record_class=item['record_class'],verification=item['verification'],
                applicability=item['applicability'],expires_at=item['expires_at'],evidence_refs=item['evidence_refs'],
                support='source_observation' if item['record_class']=='evidence' and not item['evidence_refs'] else 'client_claim'))
        pins=[dict(memory_id=mid,revision=item['revision'],relationship='derived_from') for mid,item in sorted(retained.items())]
        candidate=dict(version=VERSION,review='owner_review_required',uncertainty='Unverified source claims; duplicate text and actors do not establish independence. Semantic contradictions are not detected.',
            claims=list(groups.values()),conflicts=sorted(conflicts,key=encoded),
            source_refs=pins,domain=next(iter(domains)),topics=sorted({topic for item in retained.values() for topic in item['topics']})[:32])
        statement=encoded(candidate)
        if len(statement)>MAX_OUTPUT:raise Unavailable('Consolidation output limit')
        # Screen the whole exported candidate, not just its excerpts.
        if not general_envelope(dict(domain=candidate['domain'],status='active',content=statement),dict(sensitivity='general',expires_at=None),now_ms()):
            raise Denied('Candidate unavailable')
        digest=hashlib.sha256(statement.encode()).hexdigest()
        return dict(candidate_id=digest,candidate=candidate)

    def stage(self,principal: Principal,args):
        if not isinstance(args,dict) or set(args)!={'source_refs','expected_candidate_id'}:raise Invalid('Invalid staging arguments')
        # Staging is a derived proposal only; it cannot apply a correction or attest review.
        self.service._authorize(principal,'derive')
        preview=self.preview(principal,dict(source_refs=args['source_refs']))
        if args['expected_candidate_id']!=preview['candidate_id']:raise Denied('Candidate changed')
        candidate=preview['candidate']
        return self.service.capture(principal,dict(request_id='consolidate:'+preview['candidate_id'],
            statement=encoded(candidate),title='Consolidation candidate: owner review required',
            domain=candidate['domain'],topics=candidate['topics'],record_class='derived_view',kind='idea',
            attribution_basis='assistant_inferred',applicability='Pending owner review; source applicability is retained per claim.',
            evidence_refs=candidate['source_refs']))
