"""Hermes compatibility within the trusted store process; no canonical recall fallback."""
from .policy import HERMES, topics, Invalid
from .service import METADATA_FIELDS, Service, available

def revision_hook(db):
    if not available(db):return None
    service=Service(db)
    def persist(result,now):
        item=service._item(result['memory_id']);old=service._metadata(result['memory_id'])
        source=item['source'] or ''
        imported=source.startswith(('discover:','github:')) or source=='x-bookmark'
        derived=source.startswith('cron:') or source=='profile:interests'
        if old:
            metadata={key:old[key] for key in METADATA_FIELDS}
        else:
            metadata=dict(sensitivity='general',
                          record_class='derived_view' if derived else 'evidence' if imported else 'assertion',
                          kind='imported_record',verification='source_observation' if imported else 'client_asserted',
                          topics=[],evidence_refs=[],applicability=None,expires_at=None)
            try:metadata['topics']=topics(item['tags'])
            except Invalid:metadata['sensitivity']='unclassified'
        if derived:metadata['sensitivity']='unclassified'
        service._hook(HERMES,metadata,'Hermes compatibility write')(result,now)
    return persist

def service(db):return Service(db)
