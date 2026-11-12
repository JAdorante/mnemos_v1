"""GitHub connector — issues, pull requests and repos the user is part of,
read-only.

OAuth App flow (``github.com/login/oauth``), or a ``GITHUB_TOKEN`` personal
access token for an install without an app. GitHub has no read-only scope
that reaches private repositories: ``repo`` is what a private issue needs,
so the default ask is ``repo read:org read:user`` (``GITHUB_OAUTH_SCOPES``
narrows it to ``public_repo read:user`` for public-only). The connector
never calls a write endpoint; a token with write scope is a GitHub
limitation, not a capability Sparrow uses.

Reads, each page capped:
  GET /issues?filter=all&state=all&since=…   issues and PRs the user is
                                              assigned, authored, mentioned
                                              in or subscribed to
  GET /user/repos?sort=pushed                 repos pushed to since the cursor
Items land through connectors/scheduler.py as ``github.issue``,
``github.pull_request`` and ``github.repo``.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import urlencode

from app.config import settings
from app.services.connectors.oauth_base import (
    OAuthConnectorBase,
    clip,
    iso_utc,
    parse_iso,
)

API = "https://api.github.com"
API_VERSION = "2022-11-28"
BODY_CAP = 1500
_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class GithubConnector(OAuthConnectorBase):
    id = "github"
    label = "GitHub"
    tool_names = ("GitHub",)
    category = "dev"
    description = (
        "Read-only issues, pull requests and repos you take part in. "
        "You sign in on GitHub's screen — Sparrow never sees your password."
    )
    capabilities = (
        "Remember issues and pull requests you are on",
        "Track repos you push to",
        "Close tasks when an issue or PR you promised moves",
    )
    source_prefixes = ("github.issue", "github.pull_request", "github.repo")
    sync_interval_s = 300
    sync_blurb = ("new and updated GitHub issues, pull requests and repos "
                  "you are part of")

    authorize_url = "https://github.com/login/oauth/authorize"
    token_url = "https://github.com/login/oauth/access_token"
    scopes = ("repo", "read:org", "read:user")
    # Alternatives a narrower GITHUB_OAUTH_SCOPES may name; never asked for
    # by default, accepted back only when they were requested.
    extra_authorize_params = {"allow_signup": "false"}
    loopback_host = "127.0.0.1"
    loopback_port = 0
    client_id_env = "GITHUB_OAUTH_CLIENT_ID"
    client_secret_env = "GITHUB_OAUTH_CLIENT_SECRET"
    env_token_var = "GITHUB_TOKEN"
    env_token_label = "personal access token"

    def _cfg(self):
        return settings.github

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION}

    def _whoami(self) -> dict[str, Any]:
        me, _ = self._request(f"{API}/user")
        login = (me or {}).get("login") or ""
        return {"account": login, "name": (me or {}).get("name") or login}

    # --- reads ------------------------------------------------------------------------
    def _max_pages(self) -> int:
        try:
            return max(1, int(os.environ.get("QUILL_GITHUB_MAX_PAGES", "5")))
        except ValueError:
            return 5

    def _paged(self, url: str) -> list[dict]:
        out: list[dict] = []
        pages = 0
        while url and pages < self._max_pages():
            data, hdrs = self._request(url)
            pages += 1
            if isinstance(data, list):
                out.extend(d for d in data if isinstance(d, dict))
            m = _LINK_NEXT.search(hdrs.get("link") or "")
            url = m.group(1) if m else None
        return out

    def fetch_issues(self, *, since: float) -> list[dict]:
        q = urlencode({"filter": "all", "state": "all", "sort": "updated",
                       "direction": "desc", "since": iso_utc(since),
                       "per_page": 100})
        rows = self._paged(f"{API}/issues?{q}")
        return [r for r in rows if parse_iso(r.get("updated_at")) >= since - 1]

    def fetch_repos(self, *, since: float) -> list[dict]:
        q = urlencode({"sort": "pushed", "direction": "desc", "per_page": 100,
                       "affiliation": "owner,collaborator,organization_member"})
        out = []
        for r in self._paged(f"{API}/user/repos?{q}"):
            if parse_iso(r.get("pushed_at")) < since:
                break
            out.append(r)
        return out

    # --- mapping ----------------------------------------------------------------------
    @staticmethod
    def _repo_name(row: dict) -> str:
        repo = row.get("repository") or {}
        if repo.get("full_name"):
            return str(repo["full_name"])
        url = str(row.get("repository_url") or "")
        return url.split("/repos/", 1)[1] if "/repos/" in url else ""

    @staticmethod
    def _login(u: dict | None) -> str:
        return str((u or {}).get("login") or "").strip()

    def _issue_item(self, r: dict) -> dict:
        repo = self._repo_name(r)
        number = r.get("number")
        is_pr = bool(r.get("pull_request"))
        kind = "pull_request" if is_pr else "issue"
        title = clip(r.get("title"), 200)
        author = self._login(r.get("user"))
        assignees = [self._login(a) for a in (r.get("assignees") or [])]
        assignees = [a for a in assignees if a]
        labels = [str(l.get("name")) for l in (r.get("labels") or [])
                  if isinstance(l, dict) and l.get("name")]
        state = str(r.get("state") or "")
        if is_pr and (r.get("pull_request") or {}).get("merged_at"):
            state = "merged"
        elif is_pr and r.get("draft"):
            state = "draft"
        ref = f"{repo}#{number}" if repo and number else str(r.get("id") or "")
        bits = [f"State: {state}" if state else "",
                f"Author: {author}" if author else "",
                f"Assignees: {', '.join(assignees)}" if assignees else "",
                f"Labels: {', '.join(labels)}" if labels else "",
                f"Comments: {r.get('comments')}" if r.get("comments") else ""]
        lines = [l for l in (f"GitHub {'pull request' if is_pr else 'issue'} {ref}: {title}",
                             " · ".join(b for b in bits if b),
                             str(r.get("html_url") or "")) if l]
        body = clip(r.get("body"), BODY_CAP)
        if body:
            lines += ["", body]
        people = []
        for p in [author] + assignees:
            if p and p not in people:
                people.append(p)
        return {
            "kind": kind, "external_id": ref,
            "thread_id": repo or None,
            "ts": parse_iso(r.get("updated_at")) or parse_iso(r.get("created_at")),
            "title": f"{ref} {title}".strip(),
            "text": "\n".join(lines),
            "people": people, "url": r.get("html_url"), "labels": labels,
            "meta": {"repo": repo, "number": number, "state": state,
                     "author": author, "assignees": assignees,
                     "created_at": r.get("created_at"),
                     "closed_at": r.get("closed_at")},
        }

    def _repo_item(self, r: dict) -> dict:
        name = str(r.get("full_name") or "")
        desc = clip(r.get("description"), 300)
        bits = ["private" if r.get("private") else "public",
                str(r.get("language") or ""),
                f"default branch {r.get('default_branch')}" if r.get("default_branch") else "",
                f"{r.get('open_issues_count')} open issues"
                if r.get("open_issues_count") else ""]
        lines = [f"GitHub repo {name}" + (f": {desc}" if desc else ""),
                 " · ".join(b for b in bits if b),
                 str(r.get("html_url") or "")]
        owner = self._login(r.get("owner"))
        return {
            "kind": "repo", "external_id": f"repo:{name}", "thread_id": name,
            "ts": parse_iso(r.get("pushed_at")) or parse_iso(r.get("updated_at")),
            "title": name, "text": "\n".join(l for l in lines if l),
            "people": [owner] if owner and not (r.get("owner") or {}).get(
                "type") == "Organization" else [],
            "url": r.get("html_url"),
            "meta": {"repo": name, "private": bool(r.get("private")),
                     "language": r.get("language"), "pushed_at": r.get("pushed_at")},
        }

    # --- background capture ---------------------------------------------------------
    def fetch_items(self, *, cursor: dict | None = None,
                    now: float | None = None) -> tuple[list[dict], dict]:
        from app.services.connectors.oauth_base import FETCH_OVERLAP_S
        now = float(now if now is not None else time.time())
        cursor = dict(cursor or {})
        since = self._since(cursor, now)
        items = [self._issue_item(r) for r in self.fetch_issues(since=since)]
        items += [self._repo_item(r) for r in self.fetch_repos(since=since)]
        self._note_fetch(items, now)
        return items, {**cursor, "since": now - FETCH_OVERLAP_S}


github = GithubConnector()
