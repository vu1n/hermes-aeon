"""Hermes compatibility within the trusted store process; no canonical recall fallback."""
from .policy import HERMES, topics, Invalid
from .service import Service, available

def revision_hook(db):
    if not available(db):return None
    service=Service(db)
    def persist(result,now):
        item=service._item(result['memory_id']);old=service._metadata(result['memory_id'])
        source=item['source'] or ''
        imported=source.startswith(('discover:','github:')) or source=='x-bookmark'
        derived=source.startswith('cron:') or source=='profile:interests'
        metadata=dict(sensitivity=old['sensitivity'] if old else 'general',
                      record_class=old['record_class'] if old else ('derived_view' if derived else 'evidence' if imported else 'assertion'),
                      kind=old['kind'] if old else 'imported_record',verification=old['verification'] if old else 'source_observation' if imported else 'client_asserted',
                      topics=old['topics'] if old else [],evidence_refs=old['evidence_refs'] if old else [],
                      applicability=old['applicability'] if old else None,expires_at=old['expires_at'] if old else None)
        if not old:
            try:metadata['topics']=topics(item['tags'])
            except Invalid:metadata['sensitivity']='unclassified'
        if derived:metadata['sensitivity']='unclassified'
        service._hook(HERMES,metadata,'Hermes compatibility write')(result,now)
    return persist

def service(db):return Service(db)
