"""Generic webhook write-back: POST a signed JSON record envelope.

Unblocks anything else (Zapier, internal tools). The receiver authenticates
with `X-Sparrow-Signature: sha256=<hex HMAC of the exact body>` under the
connector's signing secret, and dedupes on `Idempotency-Key`. There is no
read-back API on an arbitrary receiver, so a 2xx that echoes the key (in the
body as {"idempotency_key": ...} or the header) is the verification; a 2xx
without the echo is accepted but recorded as `ack_only`.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from org_coordinator.connectors import http
from org_coordinator.connectors.base import PermanentError


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookConnector:
    kind = "webhook"

    def __init__(self, connector_id: str, config: dict, secret: dict) -> None:
        self.connector_id = connector_id
        self.url = config.get("url") or ""
        self.secret = secret.get("signing_secret") or ""
        if not self.url.startswith("https://") and not config.get("allow_http"):
            raise PermanentError("webhook url must be https")
        if not self.secret:
            raise PermanentError("webhook connector has no signing_secret")

    def supports(self, kind: str, predicate: str, op: str) -> bool:
        return op == "post"

    def resolve_target(self, scope: dict, subject_ref: str,
                       target_hint: str | None) -> str | None:
        return f"webhook:{self.connector_id}"

    def map_record(self, version: dict, target: str, mapping: dict) -> dict:
        envelope = {
            "type": "sparrow.record.version",
            "scope_id": version.get("scope_id"),
            "subject_ref": version.get("subject_ref"),
            "subject_label": version.get("subject_label"),
            "kind": version.get("kind"), "predicate": version.get("predicate"),
            "value": version.get("value"),
            "valid_from": version.get("valid_from"),
            "packet_id": version.get("packet_id")}
        return {"connector_id": self.connector_id, "kind": self.kind,
                "target": target, "op": "post", "fields": envelope,
                "text": None, "marker": None}

    def preview(self, plan: dict) -> list[dict]:
        return [{"field": "post", "before": None, "after": plan["fields"]}]

    def write(self, plan: dict, idempotency_key: str, *,
              expected: dict | None = None) -> dict:
        body = json.dumps({**plan["fields"], "idempotency_key": idempotency_key},
                          sort_keys=True, separators=(",", ":")).encode()
        _s, resp = http.call("POST", self.url, raw=body, headers={
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "X-Sparrow-Signature": sign(self.secret, body)})
        echoed = isinstance(resp, dict) and \
            resp.get("idempotency_key") == idempotency_key
        return {"external_version": idempotency_key,
                "written": plan["fields"],
                "skipped": [] if echoed else ["ack_only"]}

    def read_back(self, plan: dict) -> dict:
        return {"exists": True}          # verification happened on the 2xx

    def verified(self, plan: dict, back: dict) -> bool:
        return True
