"""Single-owner capabilities are trusted configuration, never memory content."""
from dataclasses import dataclass
import json
import re
if '.' in __package__:
    from ..aeon_readonly.projection.policy import UNSAFE
else:
    from aeon_readonly.projection.policy import UNSAFE

API_VERSION='brain.general.v1'
POLICY_VERSION='general-v1'
DOMAINS=frozenset({'work','learning','side_projects'})
CLASSES=frozenset({'evidence','assertion','working_context','derived_view'})
RELATIONSHIPS=frozenset({'supports','contradicts','supersedes','derived_from'})
TOPIC_ALIASES={'agent-evals':'agent-evaluation',
               'memory-service':'memory-services'}

@dataclass(frozen=True)
class Principal:
    id: str
    owner_id: str
    capabilities: frozenset[str]
    source_namespace: str

HERMES=Principal('hermes','local',frozenset({'read','capture','revise_own','retract_own','propose'}),'chat:hermes')

class Denied(ValueError):
    pass
class Invalid(ValueError):
    pass
class Unavailable(RuntimeError):
    pass

def topics(value):
    if not isinstance(value,list) or len(value)>32:raise Invalid('Invalid topics')
    result=[]
    for raw in value:
        if not isinstance(raw,str) or len(raw)>64:raise Invalid('Invalid topic')
        normalized=raw.strip().lower().replace('_','-').replace(' ','-')
        if not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}',normalized):raise Invalid('Invalid topic')
        result.append(TOPIC_ALIASES.get(normalized,normalized))
    return sorted(set(result))

def general_envelope(item,metadata,now):
    if metadata['sensitivity']!='general' or not metadata.get('valid',True):return False
    if item['domain'] not in DOMAINS or item['status']!='active':return False
    if metadata['expires_at'] is not None and metadata['expires_at']<=now:return False
    try:
        envelope=json.dumps({'item':item,'metadata':metadata},ensure_ascii=True,sort_keys=True)
    except (TypeError,ValueError,RecursionError):return False
    if len(envelope)>150000:return False
    # JSON escaping changes whitespace and word boundaries; screen decoded strings.
    pending=[item,metadata]
    while pending:
        value=pending.pop()
        if isinstance(value,str):
            if UNSAFE.search(value):return False
        elif isinstance(value,dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value,(list,tuple)):
            pending.extend(value)
    return True
