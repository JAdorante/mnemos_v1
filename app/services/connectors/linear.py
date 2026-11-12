"""Linear connector — issues and projects, read-only.

OAuth (``linear.app/oauth/authorize``, scope ``read``, which Linear defines
as read-only), or a ``LINEAR_API_KEY`` personal API key for an install
without an app. Linear's token exchange needs the client secret, and its
redirect URL must match the registered one exactly, so the desktop loopback
uses a fixed ``http://localhost:48621/`` (``QUILL_OAUTH_LOOPBACK_PORT_LINEAR``).

Reads are GraphQL (``api.linear.app/graphql``): issues and projects whose
``updatedAt`` passed the cursor, in pages of 50. Items land through
connectors/scheduler.py as ``linear.issue`` and ``linear.project``.
"""
from __future__ import annotations

import os
import time
from typing import Any

from app.config import settings
from app.services.connectors.oauth_base import (
    OAuthApiError,
    OAuthConnectorBase,
    clip,
    iso_utc,
    parse_iso,
)

GRAPHQL = "https://api.linear.app/graphql"
BODY_CAP = 1500
PAGE = 50

_ISSUES_Q = """
query Issues($after: String) {
  issues(first: %d, after: $after, orderBy: updatedAt,
         filter: {updatedAt: {gt: "%s"}}) {
    nodes {
      id identifier title description url updatedAt createdAt
      priority priorityLabel dueDate
      state { name type }
      assignee { name email }
      creator { name email }
      team { key name }
      project { name }
      labels { nodes { name } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""
_PROJECTS_Q = """
query Projects($after: String) {
  projects(first: %d, after: $after,
           filter: {updatedAt: {gt: "%s"}}) {
    nodes {
      id name description url updatedAt targetDate progress
      %s
      lead { name email }
      teams { nodes { key } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""
# Linear renamed Project.state (a string) to status { name }; ask for the
# new field and fall back to the old one on a schema error.
_PROJECT_STATUS_FIELDS = ("status { name }", "state")


class LinearConnector(OAuthConnectorBase):
    id = "linear"
    label = "Linear"
    tool_names = ("Linear",)
    category = "dev"
    description = (
        "Read-only issues and project status from your Linear workspace. "
        "You sign in on Linear's screen — Sparrow never sees your password."
    )
    capabilities = (
        "Remember issues assigned to you and your team",
        "Track project status and target dates",
        "Close tasks when an issue you promised moves",
    )
    source_prefixes = ("linear.issue", "linear.project")
    sync_interval_s = 300
    sync_blurb = "new and updated Linear issues and projects"

    authorize_url = "https://linear.app/oauth/authorize"
    token_url = "https://api.linear.app/oauth/token"
    scopes = ("read",)
    scope_separator = ","
    extra_authorize_params = {"prompt": "consent"}
    loopback_host = "localhost"
    loopback_port = 48621
    client_id_env = "LINEAR_OAUTH_CLIENT_ID"
    client_secret_env = "LINEAR_OAUTH_CLIENT_SECRET"
    env_token_var = "LINEAR_API_KEY"
    env_token_label = "personal API key"

    def _cfg(self):
        return settings.linear

    def _auth_headers(self) -> dict[str, str]:
        tok = self._access_token()
        # Personal API keys go bare; OAuth tokens as Bearer.
        if self.env_token_active():
            return {"Authorization": tok}
        return {"Authorization": f"Bearer {tok}"}

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        data, _ = self._request(GRAPHQL, method="POST",
                                body={"query": query, "variables": variables or {}})
        if not isinstance(data, dict):
            raise RuntimeError("linear returned a non-object")
        errs = data.get("errors")
        if errs:
            msg = "; ".join(str(e.get("message") or e) for e in errs
                            if isinstance(e, dict)) or str(errs)
            raise OAuthApiError(200, msg)
        return data.get("data") or {}

    def _whoami(self) -> dict[str, Any]:
        me = self._gql("{ viewer { name email } }").get("viewer") or {}
        return {"account": me.get("email") or me.get("name") or "",
                "name": me.get("name") or ""}

    def _max_pages(self) -> int:
        try:
            return max(1, int(os.environ.get("QUILL_LINEAR_MAX_PAGES", "10")))
        except ValueError:
            return 10

    def _paged(self, query: str, root: str) -> list[dict]:
        out: list[dict] = []
        after = None
        for _ in range(self._max_pages()):
            conn = self._gql(query, {"after": after}).get(root) or {}
            out.extend(n for n in (conn.get("nodes") or []) if isinstance(n, dict))
            info = conn.get("pageInfo") or {}
            if not info.get("hasNextPage") or not info.get("endCursor"):
                break
            after = info["endCursor"]
        return out

    def fetch_issues(self, *, since: float) -> list[dict]:
        return self._paged(_ISSUES_Q % (PAGE, iso_utc(since)), "issues")

    def fetch_projects(self, *, since: float) -> list[dict]:
        last: Exception | None = None
        for fields in _PROJECT_STATUS_FIELDS:
            try:
                return self._paged(_PROJECTS_Q % (PAGE, iso_utc(since), fields),
                                   "projects")
            except OAuthApiError as exc:
                last = exc
                if "status" not in str(exc) and "state" not in str(exc):
                    raise
        raise last  # type: ignore[misc]

    # --- mapping ----------------------------------------------------------------------
    @staticmethod
    def _name(u: dict | None) -> str:
        u = u or {}
        return str(u.get("name") or u.get("email") or "").strip()

    def _issue_item(self, n: dict) -> dict:
        ident = str(n.get("identifier") or n.get("id") or "")
        title = clip(n.get("title"), 200)
        state = ((n.get("state") or {}).get("name") or "")
        assignee = self._name(n.get("assignee"))
        creator = self._name(n.get("creator"))
        team = n.get("team") or {}
        project = (n.get("project") or {}).get("name") or ""
        labels = [str(l.get("name")) for l in ((n.get("labels") or {}).get("nodes") or [])
                  if isinstance(l, dict) and l.get("name")]
        bits = [f"Status: {state}" if state else "",
                f"Priority: {n.get('priorityLabel')}" if n.get("priorityLabel") else "",
                f"Assignee: {assignee}" if assignee else "",
                f"Team: {team.get('name') or team.get('key')}" if team else "",
                f"Project: {project}" if project else "",
                f"Due: {n.get('dueDate')}" if n.get("dueDate") else "",
                f"Labels: {', '.join(labels)}" if labels else ""]
        lines = [l for l in (f"Linear issue {ident}: {title}",
                             " · ".join(b for b in bits if b), str(n.get("url") or "")) if l]
        desc = clip(n.get("description"), BODY_CAP)
        if desc:
            lines += ["", desc]
        people = [p for p in (assignee, creator) if p]
        people = list(dict.fromkeys(people))
        return {
            "kind": "issue", "external_id": ident,
            "thread_id": str(team.get("key") or "") or None,
            "ts": parse_iso(n.get("updatedAt")) or parse_iso(n.get("createdAt")),
            "title": f"{ident} {title}".strip(),
            "text": "\n".join(lines),
            "people": people, "url": n.get("url"), "labels": labels,
            "meta": {"state": state, "state_type": (n.get("state") or {}).get("type"),
                     "priority": n.get("priority"), "assignee": assignee,
                     "creator": creator, "team": team.get("key"),
                     "project": project, "due": n.get("dueDate")},
        }

    def _project_item(self, n: dict) -> dict:
        name = clip(n.get("name"), 200)
        status = ((n.get("status") or {}).get("name") if isinstance(n.get("status"), dict)
                  else n.get("state")) or ""
        lead = self._name(n.get("lead"))
        teams = [str(t.get("key")) for t in ((n.get("teams") or {}).get("nodes") or [])
                 if isinstance(t, dict) and t.get("key")]
        progress = n.get("progress")
        try:
            pct = f"{round(float(progress) * 100)}% done" if progress is not None else ""
        except (TypeError, ValueError):
            pct = ""
        bits = [f"Status: {status}" if status else "", pct,
                f"Lead: {lead}" if lead else "",
                f"Target: {n.get('targetDate')}" if n.get("targetDate") else "",
                f"Teams: {', '.join(teams)}" if teams else ""]
        lines = [l for l in (f"Linear project {name}", " · ".join(b for b in bits if b),
                             str(n.get("url") or "")) if l]
        desc = clip(n.get("description"), BODY_CAP)
        if desc:
            lines += ["", desc]
        return {
            "kind": "project", "external_id": f"project:{n.get('id')}",
            "thread_id": teams[0] if teams else None,
            "ts": parse_iso(n.get("updatedAt")),
            "title": name, "text": "\n".join(lines),
            "people": [lead] if lead else [], "url": n.get("url"),
            "meta": {"status": status, "lead": lead, "teams": teams,
                     "target_date": n.get("targetDate"), "progress": progress},
        }

    # --- background capture ---------------------------------------------------------
    def fetch_items(self, *, cursor: dict | None = None,
                    now: float | None = None) -> tuple[list[dict], dict]:
        from app.services.connectors.oauth_base import FETCH_OVERLAP_S
        now = float(now if now is not None else time.time())
        cursor = dict(cursor or {})
        since = self._since(cursor, now)
        items = [self._issue_item(n) for n in self.fetch_issues(since=since)]
        items += [self._project_item(n) for n in self.fetch_projects(since=since)]
        self._note_fetch(items, now)
        return items, {**cursor, "since": now - FETCH_OVERLAP_S}


linear = LinearConnector()
