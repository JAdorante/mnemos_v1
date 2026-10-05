"""In-process fakes of the external systems write-back talks to.

Each fake implements just the API surface the connector uses, with the
behaviours that matter for correctness: HubSpot stores every property as a
string and bumps updatedAt; its search matches CONTAINS_TOKEN on whole
tokens; Google Docs inserts at the end of the body and returns the revision;
the webhook receiver checks the HMAC and dedupes on Idempotency-Key. Each can
be told to fail the next N calls with a status (429/503/404).

`FakeWorld.transport` plugs into org_coordinator.connectors.http.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qs, urlparse


class FakeHubSpot:
    def __init__(self, token: str = "hs-token") -> None:
        self.token = token
        self.objects: dict[tuple[str, str], dict] = {}
        self.created: dict[str, dict[str, dict]] = {"notes": {}, "tasks": {}}
        self.patches: list[dict] = []
        self.fail: list[int] = []
        self._n = 0

    def add(self, obj: str, oid: str, **props) -> None:
        self.objects[(obj, oid)] = {"properties": {k: str(v) for k, v in props.items()},
                                    "updatedAt": "2026-10-01T00:00:00Z"}

    def edit(self, obj: str, oid: str, **props) -> None:
        """A human editing the CRM directly."""
        rec = self.objects[(obj, oid)]
        rec["properties"].update({k: str(v) for k, v in props.items()})
        rec["updatedAt"] = f"2026-10-02T{len(self.patches):02d}:00:00Z"

    def handle(self, method, url, headers, body):
        if self.fail:
            return self.fail.pop(0), {"message": "injected"}
        if headers.get("Authorization") != f"Bearer {self.token}":
            return 401, {"message": "bad token"}
        u = urlparse(url)
        parts = u.path.strip("/").split("/")          # crm v3 objects ...
        data = json.loads(body) if body else {}
        if parts[:3] != ["crm", "v3", "objects"]:
            return 404, {}
        coll = parts[3]
        if coll in ("notes", "tasks"):
            if len(parts) == 5 and parts[4] == "search" and method == "POST":
                tok = data["filterGroups"][0]["filters"][0]["value"]
                prop = data["filterGroups"][0]["filters"][0]["propertyName"]
                hits = [{"id": i} for i, r in self.created[coll].items()
                        if tok in re.findall(r"[\w-]+", r["properties"].get(prop, ""))]
                return 200, {"total": len(hits), "results": hits[:1]}
            if len(parts) == 4 and method == "POST":
                self._n += 1
                nid = str(9000 + self._n)
                self.created[coll][nid] = {"properties": data["properties"],
                                           "associations": data["associations"]}
                return 201, {"id": nid}
            return 404, {}
        key = (coll, parts[4] if len(parts) > 4 else "")
        rec = self.objects.get(key)
        if rec is None:
            return 404, {"message": "object not found"}
        if method == "GET":
            want = parse_qs(u.query).get("properties", [""])[0].split(",")
            return 200, {"id": key[1], "updatedAt": rec["updatedAt"],
                         "properties": {k: rec["properties"].get(k)
                                        for k in want if k}}
        if method == "PATCH":
            self.patches.append({"object": key, **data})
            rec["properties"].update({k: str(v) for k, v in
                                      data["properties"].items()})
            rec["updatedAt"] = f"2026-10-02T12:{len(self.patches):02d}:00Z"
            return 200, {"id": key[1], "updatedAt": rec["updatedAt"],
                         "properties": rec["properties"]}
        return 405, {}


class FakeGoogleDocs:
    def __init__(self) -> None:
        self.docs: dict[str, list[str]] = {}
        self.refreshes = 0
        self.fail: list[int] = []

    def add(self, doc_id: str, text: str = "Running log\n") -> None:
        self.docs[doc_id] = [text]

    def text(self, doc_id: str) -> str:
        return "".join(self.docs[doc_id])

    def handle(self, method, url, headers, body):
        if self.fail:
            return self.fail.pop(0), {"error": "injected"}
        u = urlparse(url)
        if u.netloc == "oauth2.googleapis.com":
            self.refreshes += 1
            form = parse_qs(body.decode())
            if form.get("refresh_token") != ["rt-1"]:
                return 400, {"error": "invalid_grant"}
            return 200, {"access_token": "at-1", "expires_in": 3600}
        if headers.get("Authorization") != "Bearer at-1":
            return 401, {}
        m = re.match(r"^/v1/documents/([^:/]+)(:batchUpdate)?$", u.path)
        if not m or m.group(1) not in self.docs:
            return 404, {}
        doc = m.group(1)
        if m.group(2) and method == "POST":
            for req in json.loads(body)["requests"]:
                self.docs[doc].append(req["insertText"]["text"])
            return 200, {"documentId": doc,
                         "writeControl": {"requiredRevisionId":
                                          f"rev{len(self.docs[doc])}"}}
        content, idx = [], 1
        for run in self.docs[doc]:
            content.append({"startIndex": idx, "endIndex": idx + len(run),
                            "paragraph": {"elements": [{"textRun":
                                                        {"content": run}}]}})
            idx += len(run)
        return 200, {"documentId": doc, "body": {"content": content}}


class FakeWebhook:
    def __init__(self, secret: str = "whsec") -> None:
        self.secret = secret
        self.received: dict[str, dict] = {}
        self.calls = 0
        self.fail: list[int] = []
        self.echo = True

    def handle(self, method, url, headers, body):
        self.calls += 1
        if self.fail:
            return self.fail.pop(0), {}
        want = "sha256=" + hmac.new(self.secret.encode(), body,
                                    hashlib.sha256).hexdigest()
        if headers.get("X-Sparrow-Signature") != want:
            return 401, {"error": "bad signature"}
        key = headers.get("Idempotency-Key")
        self.received.setdefault(key, json.loads(body))
        return 200, ({"idempotency_key": key} if self.echo else {})


class FakeWorld:
    def __init__(self) -> None:
        self.hubspot = FakeHubSpot()
        self.docs = FakeGoogleDocs()
        self.webhook = FakeWebhook()
        self.log: list[tuple[str, str]] = []

    def transport(self, method, url, headers, body):
        self.log.append((method, url))
        host = urlparse(url).netloc
        if host == "api.hubapi.com":
            status, payload = self.hubspot.handle(method, url, headers, body)
        elif host in ("docs.googleapis.com", "oauth2.googleapis.com"):
            status, payload = self.docs.handle(method, url, headers, body)
        elif host == "hooks.example.test":
            status, payload = self.webhook.handle(method, url, headers, body)
        else:
            return 599, {}, b"unknown host"
        return status, {}, json.dumps(payload).encode()

    def writes(self) -> int:
        return sum(1 for m, _u in self.log if m in ("PATCH", "POST")
                   and "search" not in _u and "oauth2" not in _u)


def now() -> float:
    return time.time()
