"""Pull recent GitHub events. Returns dict {captured, skipped, fetched}."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Optional

import httpx

from store import queries as q
from store.embed import embed_text

log = logging.getLogger("aeon.ingest.github")

GH_API = "https://api.github.com"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _fetch(user: str, token: str, per_page: int, max_pages: int) -> list[dict]:
    events: list[dict] = []
    for page in range(1, max_pages + 1):
        resp = httpx.get(
            f"{GH_API}/users/{user}/events",
            headers=_headers(token),
            params={"per_page": per_page, "page": page},
            timeout=20.0,
        )
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        events.extend(batch)
        if len(batch) < per_page:
            break
    return events


def _parse_ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _to_memory(event: dict, gh_user: str) -> Optional[dict[str, Any]]:
    etype = event.get("type") or ""
    repo_name = (event.get("repo") or {}).get("name") or ""
    payload = event.get("payload") or {}
    if not repo_name:
        return None
    owner = repo_name.split("/", 1)[0]
    domain = "side_projects" if owner == gh_user else "work"

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
            return None
        title = f"Created {ref_type}{' ' + ref if ref else ''}: {repo_name}"
        content = (payload.get("description") or "")[:500]
        url = f"https://github.com/{repo_name}"
    elif etype == "WatchEvent":
        title = f"Starred {repo_name}"
        content = ""
        url = f"https://github.com/{repo_name}"
        domain = "learning"
    elif etype == "ForkEvent":
        forkee = payload.get("forkee") or {}
        title = f"Forked {repo_name} -> {forkee.get('full_name') or '?'}"
        content = (forkee.get("description") or "")[:500]
        url = forkee.get("html_url") or f"https://github.com/{repo_name}"
    else:
        return None

    return {"title": title, "content": content, "url": url, "domain": domain,
            "project_id": repo_name, "ts_ms": _parse_ts(event.get("created_at", "")),
            "etype": etype}


def run(db) -> dict:
    gh_user = os.environ.get("GITHUB_USER", "vu1n")
    token = os.environ.get("GITHUB_API_KEY")
    if not token:
        raise RuntimeError("GITHUB_API_KEY not set")

    per_page = int(os.environ.get("GITHUB_PER_PAGE", "100"))
    max_pages = int(os.environ.get("GITHUB_MAX_PAGES", "3"))
    events = _fetch(gh_user, token, per_page, max_pages)

    captured = skipped = 0
    for event in events:
        mem = _to_memory(event, gh_user)
        if mem is None:
            skipped += 1
            continue
        dedup = f"github:{event['id']}"
        if db.execute("SELECT 1 FROM memory_items WHERE dedup_key = ? LIMIT 1",
                      (dedup,)).fetchone():
            continue
        emb, model = embed_text(mem["title"] + "\n" + mem["content"],
                                provider=os.environ.get("HERMES_AEON_EMBED", "gemini"))
        q.capture_memory(
            db, type="note", domain=mem["domain"],
            title=mem["title"], content=mem["content"], url=mem["url"],
            tags=["github", mem["etype"]],
            project_id=mem["project_id"],
            source=f"github:{mem['etype']}",
            dedup_key=dedup, captured_at=mem["ts_ms"],
            embedding=emb, embedding_model=model,
        )
        captured += 1

    log.info("fetched=%d captured=%d skipped=%d", len(events), captured, skipped)
    return {"fetched": len(events), "captured": captured, "skipped": skipped}
