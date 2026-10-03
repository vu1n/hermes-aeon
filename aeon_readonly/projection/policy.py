"""Pure scope, screening and audit serialization shared by readers and publishers."""
from dataclasses import dataclass
import json
import re
from urllib.parse import urlsplit

DOMAINS = {"learning", "work", "side_projects"}
SOURCES = {"x-bookmark", "discover:hf-papers", "discover:hn", "discover:hype-github",
           "discover:hype-huggingface", "discover:hype-reddit", "discover:lobsters", "discover:x",
           "github:ForkEvent", "github:IssueCommentEvent", "github:IssuesEvent",
           "github:PullRequestEvent", "github:PullRequestReviewCommentEvent",
           "github:PullRequestReviewEvent", "github:WatchEvent"}
HOSTS = {"github.com", "arxiv.org", "huggingface.co", "news.ycombinator.com",
         "lobste.rs", "x.com", "twitter.com", "reddit.com", "www.reddit.com"}
# Start email matching once per local-part run, avoiding quadratic suffix retries.
UNSAFE = re.compile(
    r"(?i)\b(?:health|medical|patient|diagnos\w*|therapy|sleep|nutrition|oura|"
    r"multivitamin|medication|prescription|salary|bank|mortgage|credit\s*card|"
    r"investment|financial|password|credential|secret|bearer|private[ _-]?key)\b|"
    r"api[ _-]?key|\bsk-[A-Za-z0-9_-]+|\btoken\s*[:=]|"
    r"(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions|"
    r"(?:reveal|exfiltrate)\s+(?:the\s+)?(?:secret|token|credential)"
)
RESEARCH_FIELDS = ("id", "source", "domain", "type", "status", "captured_at", "current_revision",
          "title", "summary", "content", "url", "screened_at", "scope_version")

@dataclass(frozen=True)
class Policy:
    scope: str
    sources: frozenset[str]
    fields: tuple[str, ...]
    chat_provenance: bool = False

    @property
    def source_fields(self):
        return tuple(k for k in self.fields[:-2] if k != 'provenance_json')

RESEARCH = Policy('aeon-source-filtered-research-v1', frozenset(SOURCES), RESEARCH_FIELDS)
SHARED = Policy('aeon-shared-notes-v2', frozenset(SOURCES | {'chat:dot'}),
                RESEARCH_FIELDS[:-2] + ('provenance_json',) + RESEARCH_FIELDS[-2:], True)


def provenance_json(audit, revision):
    return json.dumps(dict(consumer_id=audit[0], kind=audit[1], attribution_basis=audit[2],
                           conversation_ref=audit[3], created_at=audit[4], revision=revision))

def screen(row, policy, full=False):
    if row["source"] not in policy.sources or row["domain"] not in DOMAINS:
        return None
    if row["status"] != "active" or row["type"] not in {"link", "note"}:
        return None
    if row["scope_version"] != policy.scope or type(row["screened_at"]) is not int or row["screened_at"] < 1:
        return None
    if not re.fullmatch(r"[a-f0-9]{32}", row["id"] or ""):
        return None
    for field in ("captured_at", "current_revision"):
        if type(row[field]) is not int or row[field] < 1:
            return None
    texts = [row[k] or "" for k in ("title", "summary", "content", "url")]
    if any(not isinstance(t, str) or len(t) > 100_000 for t in texts):
        return None
    if UNSAFE.search("\n".join(texts)):
        return None
    url = row["url"]
    if url:
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname not in HOSTS or
                parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.port not in (None, 443)):
                return None
        except ValueError:
            return None
    if policy.chat_provenance and row['source'].startswith('chat:'):
        try:
            provenance = json.loads(row['provenance_json'])
            if provenance['consumer_id'] not in {row['source'][5:], 'hermes'} or provenance['revision'] != row['current_revision']:
                return None
            if provenance['kind'] not in {'idea','decision','preference','project_context','imported_record'} or provenance['attribution_basis'] not in {'user_explicit','assistant_inferred','user_corrected','source_import'}:
                return None
            if provenance['consumer_id'] != 'hermes' and (provenance['kind'] == 'imported_record' or provenance['attribution_basis'] == 'source_import'):
                return None
        except (ValueError, TypeError, KeyError):
            return None
    else:
        provenance = None
    result = {k: row[k] for k in policy.fields if k not in {"scope_version", "content", "provenance_json"}}
    if policy.chat_provenance:
        result["provenance"] = provenance
    result["title"] = (row["title"] or "")[:300]
    result["summary"] = (row["summary"] or "")[:2000]
    if full:
        result["content"] = (row["content"] or "")[:12_000]
        result["content_truncated"] = len(row["content"] or "") > 12_000
    else:
        result["excerpt"] = (row["summary"] or row["content"] or "")[:1000]
    return result
