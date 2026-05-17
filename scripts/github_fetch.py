"""Every 30m: pull recent GitHub activity events into the KB.

Captures each interesting event (push, PR open/close, review, comment, release,
create) as a memory_item with the original event timestamp. Used for:
- Correlation intelligence ("high commit days + poor sleep next day")
- "What did I ship this week" reflective queries
- Project activity per repo (project_id = repo full_name)

Domain heuristic: repos owned by GITHUB_USER -> 'side_projects', anything
else (orgs, contributions to upstream) -> 'work'.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Optional

import httpx

from _common import setup_logging, get_db
from store import queries as q
from store.embed import embed_text

log = setup_logging("github_fetch")

GH_USER = os.environ.get("GITHUB_USER", "vu1n")
GH_TOKEN = os.environ.get("GITHUB_API_KEY")
PER_PAGE = int(os.environ.get("GITHUB_PER_PAGE", "100"))
MAX_PAGES = int(os.environ.get("GITHUB_MAX_PAGES", "3"))
GH_API = "https://api.github.com"


def _gh_headers() -> dict:
    if not GH_TOKEN:
        raise RuntimeError("GITHUB_API_KEY not set")
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch_events() -> list[dict]:
    """Paginate /users/<user>/events for recent activity. Includes public + private."""
    events: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        resp = httpx.get(
            f"{GH_API}/users/{GH_USER}/events",
            headers=_gh_headers(),
            params={"per_page": PER_PAGE, "page": page},
            timeout=20.0,
        )
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        events.extend(batch)
        # If we got less than a full page, no more pages
        if len(batch) < PER_PAGE:
            break
    return events


def _parse_ts(iso: str) -> int:
    """Convert GitHub's ISO8601 UTC timestamp to ms."""
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def event_to_memory(event: dict) -> Optional[dict[str, Any]]:
    """Convert a GitHub event to memory_item kwargs. Returns None to skip the event."""
    etype = event.get("type") or ""
    repo_name = (event.get("repo") or {}).get("name") or ""
    payload = event.get("payload") or {}

    if not repo_name:
        return None
    owner = repo_name.split("/", 1)[0]
    domain = "side_projects" if owner == GH_USER else "work"

    if etype == "PushEvent":
        commits = payload.get("commits") or []
        if not commits:
            return None
        ref = (payload.get("ref") or "").replace("refs/heads/", "")
        n = len(commits)
        title = f"Pushed {n} commit{'s' if n > 1 else ''} to {repo_name}@{ref}"
        msg_lines = [f"  - {(c.get('message') or '').splitlines()[0][:140]}" for c in commits]
        content = title + "\n\n" + "\n".join(msg_lines)
        head_sha = commits[-1].get("sha")
        url = f"https://github.com/{repo_name}/commit/{head_sha}" if head_sha else f"https://github.com/{repo_name}"
    elif etype == "PullRequestEvent":
        pr = payload.get("pull_request") or {}
        action = payload.get("action", "")
        title = f"PR {action}: {(pr.get('title') or '')[:120]} ({repo_name})"
        content = pr.get("body") or pr.get("title") or ""
        url = pr.get("html_url") or ""
    elif etype == "PullRequestReviewEvent":
        pr = payload.get("pull_request") or {}
        review = payload.get("review") or {}
        title = f"Reviewed PR: {(pr.get('title') or '')[:100]} ({repo_name})"
        content = (review.get("body") or "")[:1000]
        url = review.get("html_url") or ""
    elif etype == "PullRequestReviewCommentEvent":
        pr = payload.get("pull_request") or {}
        comment = payload.get("comment") or {}
        title = f"PR review comment on {(pr.get('title') or '')[:90]} ({repo_name})"
        content = (comment.get("body") or "")[:1000]
        url = comment.get("html_url") or ""
    elif etype == "IssueCommentEvent":
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        title = f"Commented on '{(issue.get('title') or '')[:100]}' ({repo_name})"
        content = (comment.get("body") or "")[:1000]
        url = comment.get("html_url") or ""
    elif etype == "IssuesEvent":
        issue = payload.get("issue") or {}
        action = payload.get("action", "")
        title = f"Issue {action}: {(issue.get('title') or '')[:120]} ({repo_name})"
        content = (issue.get("body") or "")[:1000]
        url = issue.get("html_url") or ""
    elif etype == "ReleaseEvent":
        release = payload.get("release") or {}
        title = f"Released {release.get('tag_name') or '?'} on {repo_name}"
        content = (release.get("body") or release.get("name") or "")[:2000]
        url = release.get("html_url") or ""
    elif etype == "CreateEvent":
        ref_type = payload.get("ref_type") or ""
        ref = payload.get("ref") or ""
        if ref_type == "branch":
            # Skip branch creates — too noisy
            return None
        title = f"Created {ref_type}{' ' + ref if ref else ''}: {repo_name}"
        content = (payload.get("description") or "")[:500]
        url = f"https://github.com/{repo_name}"
    elif etype == "WatchEvent":
        # Starred a repo — actually a "Watch" in API terms
        title = f"Starred {repo_name}"
        content = ""
        url = f"https://github.com/{repo_name}"
        domain = "learning"  # stars reflect interest, not work
    elif etype == "ForkEvent":
        forkee = payload.get("forkee") or {}
        title = f"Forked {repo_name} -> {forkee.get('full_name') or '?'}"
        content = (forkee.get("description") or "")[:500]
        url = forkee.get("html_url") or f"https://github.com/{repo_name}"
    else:
        return None  # uninteresting (DeleteEvent, PublicEvent, MemberEvent, etc.)

    return {
        "title": title,
        "content": content,
        "url": url,
        "domain": domain,
        "project_id": repo_name,
        "ts_ms": _parse_ts(event.get("created_at", "")),
        "etype": etype,
    }


def main() -> int:
    if not GH_TOKEN:
        log.error("GITHUB_API_KEY not set")
        return 2

    db = get_db()
    try:
        events = fetch_events()
        log.info("fetched %d raw events", len(events))

        captured = 0
        skipped = 0
        for event in events:
            mem = event_to_memory(event)
            if mem is None:
                skipped += 1
                continue

            dedup = f"github:{event['id']}"
            if db.execute("SELECT 1 FROM memory_items WHERE dedup_key = ? LIMIT 1",
                          (dedup,)).fetchone():
                continue

            emb, model = embed_text(
                mem["title"] + "\n" + mem["content"],
                provider=os.environ.get("HERMES_AEON_EMBED", "gemini"),
            )
            q.capture_memory(
                db, type="note", domain=mem["domain"],
                title=mem["title"], content=mem["content"], url=mem["url"],
                tags=["github", mem["etype"]],
                project_id=mem["project_id"],
                source=f"github:{mem['etype']}",
                dedup_key=dedup,
                captured_at=mem["ts_ms"],
                embedding=emb, embedding_model=model,
            )
            captured += 1

        log.info("captured=%d skipped=%d", captured, skipped)
        if captured:
            print(f"github: captured {captured} events ({skipped} skipped)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
