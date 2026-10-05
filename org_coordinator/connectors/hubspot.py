"""HubSpot write-back: update deal/contact/company properties, create notes and
tasks associated to the object (spec, Phase 1 targets).

Auth: a private-app token (secret {"token"}). Property values are strings on
HubSpot's side, so every planned value is a string too — what the approver
sees in the preview is exactly what is PATCHed and exactly what read-back
compares against.

Idempotency: a property PATCH is naturally idempotent (and a field already
holding the planned value is skipped); a created note/task carries the
packet's marker token in its body, and `write` searches for it before
creating, so a retry after a lost response never makes a second note.
"""
from __future__ import annotations

import re
from typing import Any

from org_coordinator.connectors import http
from org_coordinator.connectors.base import ConflictError, PermanentError
from org_coordinator.connectors.mapping import (field_and_value, marker,
                                                render_text, _num_str)

API = "https://api.hubapi.com"
OBJECTS = {"deal": "deals", "deals": "deals", "contact": "contacts",
           "contacts": "contacts", "company": "companies",
           "companies": "companies"}
# HUBSPOT_DEFINED association type ids (v4): note/task -> object.
ASSOC = {("notes", "deals"): 214, ("notes", "contacts"): 202,
         ("notes", "companies"): 190, ("tasks", "deals"): 216,
         ("tasks", "contacts"): 204, ("tasks", "companies"): 192}
_TARGET = re.compile(r"^hubspot:([a-z]+)/(\d+)$")


def parse_target(ref: str | None) -> tuple[str, str] | None:
    m = _TARGET.match((ref or "").strip())
    if not m or m.group(1) not in OBJECTS:
        return None
    return OBJECTS[m.group(1)], m.group(2)


class HubSpotConnector:
    kind = "hubspot"

    def __init__(self, connector_id: str, config: dict, secret: dict) -> None:
        self.connector_id = connector_id
        self.base = (config.get("base_url") or API).rstrip("/")
        self.token = secret.get("token") or ""
        if not self.token:
            raise PermanentError("hubspot connector has no token")

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    # -- contract --
    def supports(self, kind: str, predicate: str, op: str) -> bool:
        return op in ("set_property", "create_note", "create_task")

    def resolve_target(self, scope: dict, subject_ref: str,
                       target_hint: str | None) -> str | None:
        for ref in (target_hint, (scope or {}).get("external_ref")):
            parsed = parse_target(ref)
            if parsed:
                return f"hubspot:{parsed[0]}/{parsed[1]}"
        return None

    def map_record(self, version: dict, target: str, mapping: dict) -> dict:
        op = mapping["op"]
        plan: dict[str, Any] = {"connector_id": self.connector_id,
                                "kind": self.kind, "target": target, "op": op,
                                "fields": None, "text": None, "marker": None}
        if op == "set_property":
            field, value = field_and_value(version, mapping)
            plan["fields"] = {field: "" if value is None else _num_str(value)}
            return plan
        mk = marker(version)
        text = f"{render_text(version)}\n\n[{mk}]"
        plan["text"], plan["marker"] = text, mk
        if op == "create_task":
            due = (version.get("value") or {}).get("due") or ""
            plan["fields"] = {"hs_task_subject": render_text(version)[:250],
                              "hs_task_due": due[:10]}
        return plan

    def _object(self, target: str) -> tuple[str, str]:
        parsed = parse_target(target)
        if not parsed:
            raise PermanentError(f"not a writable HubSpot target: {target}")
        return parsed

    def _read_props(self, target: str, fields: list[str]) -> dict:
        obj, oid = self._object(target)
        _s, body = http.call(
            "GET", f"{self.base}/crm/v3/objects/{obj}/{oid}"
                   f"?properties={','.join(fields)}", headers=self._h())
        props = (body or {}).get("properties") or {}
        out = {f: props.get(f) for f in fields}
        out["__version__"] = (body or {}).get("updatedAt")
        return out

    def preview(self, plan: dict) -> list[dict]:
        if plan["op"] == "set_property":
            cur = self._read_props(plan["target"], list(plan["fields"]))
            return [{"field": f, "before": cur.get(f), "after": v}
                    for f, v in sorted(plan["fields"].items())]
        what = "note" if plan["op"] == "create_note" else "task"
        return [{"field": what, "before": None, "after": plan["text"]}]

    def _find_created(self, plan: dict) -> str | None:
        coll = "notes" if plan["op"] == "create_note" else "tasks"
        prop = "hs_note_body" if coll == "notes" else "hs_task_body"
        _s, body = http.call(
            "POST", f"{self.base}/crm/v3/objects/{coll}/search",
            headers=self._h(),
            json_body={"filterGroups": [{"filters": [{
                "propertyName": prop, "operator": "CONTAINS_TOKEN",
                "value": plan["marker"]}]}], "limit": 1})
        rows = (body or {}).get("results") or []
        return str(rows[0]["id"]) if rows else None

    def write(self, plan: dict, idempotency_key: str, *,
              expected: dict | None = None) -> dict:
        obj, oid = self._object(plan["target"])
        if plan["op"] == "set_property":
            cur = self._read_props(plan["target"], list(plan["fields"]))
            todo = {}
            for f, after in plan["fields"].items():
                if cur.get(f) == after:
                    continue                      # already there: idempotent
                if expected is not None and cur.get(f) != expected.get(f):
                    raise ConflictError(f, expected.get(f), cur.get(f))
                todo[f] = after
            version = cur.get("__version__")
            if todo:
                _s, body = http.call(
                    "PATCH", f"{self.base}/crm/v3/objects/{obj}/{oid}",
                    headers=self._h(), json_body={"properties": todo})
                version = (body or {}).get("updatedAt") or version
            return {"external_version": version, "written": plan["fields"],
                    "skipped": sorted(set(plan["fields"]) - set(todo))}
        existing = self._find_created(plan)
        if existing:
            return {"external_version": existing, "written": {"id": existing},
                    "skipped": ["already_created"]}
        coll = "notes" if plan["op"] == "create_note" else "tasks"
        props: dict[str, Any] = {}
        if coll == "notes":
            props = {"hs_note_body": plan["text"],
                     "hs_timestamp": _ts_ms(plan, default_now=True)}
        else:
            props = {"hs_task_subject": plan["fields"]["hs_task_subject"],
                     "hs_task_body": plan["text"],
                     "hs_task_status": "NOT_STARTED",
                     "hs_timestamp": _ts_ms(plan, default_now=True)}
        _s, body = http.call(
            "POST", f"{self.base}/crm/v3/objects/{coll}", headers=self._h(),
            json_body={"properties": props, "associations": [{
                "to": {"id": oid}, "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": ASSOC[(coll, obj)]}]}]})
        new_id = str((body or {}).get("id") or "")
        return {"external_version": new_id, "written": {"id": new_id},
                "skipped": []}

    def read_back(self, plan: dict) -> dict:
        if plan["op"] == "set_property":
            cur = self._read_props(plan["target"], list(plan["fields"]))
            cur.pop("__version__", None)
            return cur
        return {"exists": self._find_created(plan) is not None}

    def verified(self, plan: dict, back: dict) -> bool:
        if plan["op"] == "set_property":
            return all(back.get(f) == v for f, v in plan["fields"].items())
        return bool(back.get("exists"))


def _ts_ms(plan: dict, *, default_now: bool) -> str:
    import calendar
    import time
    due = ((plan.get("fields") or {}).get("hs_task_due") or "")[:10]
    if due:
        return str(calendar.timegm(time.strptime(due, "%Y-%m-%d")) * 1000)
    return str(int(time.time() * 1000)) if default_now else ""
