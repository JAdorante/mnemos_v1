"""Google Drive write-back: append a dated entry to a scope's running-log doc.

The doc is named by the scope's external_ref (or the packet's target),
`gdrive:doc/<documentId>`. Auth is an OAuth refresh token held in the
connector secret ({"client_id", "client_secret", "refresh_token"}); the
service exchanges it for short-lived access tokens itself.

Append-only, so there is never a "before" to conflict with and nothing to
drift: the entry carries the packet's marker, `write` checks the doc for it
before appending (a retry never doubles an entry), and read-back verifies it
is there.
"""
from __future__ import annotations

import re
import threading
import time

from org_coordinator.connectors import http
from org_coordinator.connectors.base import PermanentError
from org_coordinator.connectors.mapping import marker, render_text

DOCS = "https://docs.googleapis.com/v1/documents"
TOKEN_URL = "https://oauth2.googleapis.com/token"
_TARGET = re.compile(r"^gdrive:doc/([A-Za-z0-9_-]{10,})$")
_tokens: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def parse_target(ref: str | None) -> str | None:
    m = _TARGET.match((ref or "").strip())
    return m.group(1) if m else None


class GoogleDriveConnector:
    kind = "gdrive"

    def __init__(self, connector_id: str, config: dict, secret: dict) -> None:
        self.connector_id = connector_id
        self.docs = (config.get("docs_url") or DOCS).rstrip("/")
        self.token_url = config.get("token_url") or TOKEN_URL
        self.client_id = config.get("client_id") or secret.get("client_id") or ""
        self.client_secret = secret.get("client_secret") or ""
        self.refresh_token = secret.get("refresh_token") or ""
        if not (self.client_id and self.refresh_token):
            raise PermanentError("gdrive connector needs client_id + refresh_token")

    def _access(self) -> str:
        with _lock:
            tok = _tokens.get(self.connector_id)
            if tok and tok[1] > time.time() + 60:
                return tok[0]
        _s, body = http.call("POST", self.token_url, form={
            "grant_type": "refresh_token", "refresh_token": self.refresh_token,
            "client_id": self.client_id, "client_secret": self.client_secret})
        access = (body or {}).get("access_token")
        if not access:
            raise PermanentError("token refresh returned no access_token")
        with _lock:
            _tokens[self.connector_id] = (
                access, time.time() + float(body.get("expires_in") or 3600))
        return access

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self._access()}"}

    def supports(self, kind: str, predicate: str, op: str) -> bool:
        return op == "append_entry"

    def resolve_target(self, scope: dict, subject_ref: str,
                       target_hint: str | None) -> str | None:
        for ref in (target_hint, (scope or {}).get("external_ref")):
            doc = parse_target(ref)
            if doc:
                return f"gdrive:doc/{doc}"
        return None

    def map_record(self, version: dict, target: str, mapping: dict) -> dict:
        day = time.strftime("%Y-%m-%d", time.gmtime(float(version["valid_from"])))
        mk = marker(version)
        return {"connector_id": self.connector_id, "kind": self.kind,
                "target": target, "op": "append_entry", "fields": None,
                "text": f"{day} — {render_text(version)} [{mk}]",
                "marker": mk}

    def preview(self, plan: dict) -> list[dict]:
        return [{"field": "entry", "before": None, "after": plan["text"]}]

    def _doc_text(self, doc_id: str) -> tuple[str, int]:
        _s, body = http.call("GET", f"{self.docs}/{doc_id}", headers=self._h())
        content = ((body or {}).get("body") or {}).get("content") or []
        parts: list[str] = []
        end = 1
        for el in content:
            end = max(end, int(el.get("endIndex") or 1))
            for pe in ((el.get("paragraph") or {}).get("elements") or []):
                parts.append(((pe.get("textRun") or {}).get("content")) or "")
        return "".join(parts), end

    def write(self, plan: dict, idempotency_key: str, *,
              expected: dict | None = None) -> dict:
        doc = parse_target(plan["target"])
        if not doc:
            raise PermanentError(f"not a doc target: {plan['target']}")
        text, _end = self._doc_text(doc)
        if plan["marker"] in text:
            return {"external_version": None, "written": {"text": plan["text"]},
                    "skipped": ["already_appended"]}
        _s, body = http.call(
            "POST", f"{self.docs}/{doc}:batchUpdate", headers=self._h(),
            json_body={"requests": [{"insertText": {
                "endOfSegmentLocation": {}, "text": plan["text"] + "\n"}}]})
        rev = (body or {}).get("writeControl", {}).get("requiredRevisionId")
        return {"external_version": rev, "written": {"text": plan["text"]},
                "skipped": []}

    def read_back(self, plan: dict) -> dict:
        text, _ = self._doc_text(parse_target(plan["target"]) or "")
        return {"exists": plan["text"] in text}

    def verified(self, plan: dict, back: dict) -> bool:
        return bool(back.get("exists"))


def _reset_tokens() -> None:
    with _lock:
        _tokens.clear()

