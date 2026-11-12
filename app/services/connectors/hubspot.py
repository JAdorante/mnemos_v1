"""HubSpot connector — CRM contacts, deals and companies, read-only.

OAuth (``app.hubspot.com/oauth/authorize``) with the narrowest CRM read
scopes, or a ``HUBSPOT_ACCESS_TOKEN`` private-app token for an install
without a public app. HubSpot access tokens live 30 minutes; the refresh
token is used on the next read. The redirect URL must match the registered
one exactly and must be https except on ``localhost``, so the desktop
loopback uses a fixed ``http://localhost:48622/``
(``QUILL_OAUTH_LOOPBACK_PORT_HUBSPOT``).

Reads: the CRM v3 search endpoint per object, filtered on the last-modified
property past the cursor; deal stages and owners are resolved to labels
when those lookups succeed. Items land through connectors/scheduler.py as
``hubspot.contact``, ``hubspot.deal`` and ``hubspot.company``.

The org coordinator has a separate HubSpot *write-back* connector
(org_coordinator/connectors/hubspot.py) behind its approval flow; nothing
here writes.
"""
from __future__ import annotations

import os
import time
from typing import Any

from app.config import settings
from app.services.connectors.oauth_base import (
    OAuthConnectorBase,
    clip,
    parse_iso,
)

API = "https://api.hubapi.com"
PAGE = 100

_CONTACT_PROPS = ("firstname", "lastname", "email", "company", "jobtitle",
                  "phone", "lifecyclestage", "hubspot_owner_id",
                  "lastmodifieddate", "createdate")
_DEAL_PROPS = ("dealname", "dealstage", "pipeline", "amount", "closedate",
               "hubspot_owner_id", "hs_lastmodifieddate", "createdate")
_COMPANY_PROPS = ("name", "domain", "industry", "city", "country",
                  "hubspot_owner_id", "hs_lastmodifieddate", "createdate")


class HubspotConnector(OAuthConnectorBase):
    id = "hubspot"
    label = "HubSpot"
    tool_names = ("HubSpot",)
    category = "crm"
    description = (
        "Read-only CRM contacts, deal stages and companies. "
        "You sign in on HubSpot's screen — Sparrow never sees your password."
    )
    capabilities = (
        "Remember contacts and the companies they belong to",
        "Track deals and their stage changes",
        "Seed People from your CRM",
    )
    source_prefixes = ("hubspot.contact", "hubspot.deal", "hubspot.company")
    sync_interval_s = 600
    sync_blurb = "HubSpot contacts, deals and companies changed since the last check"

    authorize_url = "https://app.hubspot.com/oauth/authorize"
    token_url = "https://api.hubapi.com/oauth/v1/token"
    scopes = ("oauth", "crm.objects.contacts.read", "crm.objects.deals.read",
              "crm.objects.companies.read", "crm.objects.owners.read")
    loopback_host = "localhost"
    loopback_port = 48622
    client_id_env = "HUBSPOT_OAUTH_CLIENT_ID"
    client_secret_env = "HUBSPOT_OAUTH_CLIENT_SECRET"
    env_token_var = "HUBSPOT_ACCESS_TOKEN"
    env_token_label = "private app token"

    def _cfg(self):
        return settings.hubspot

    def _whoami(self) -> dict[str, Any]:
        info, _ = self._request(f"{API}/account-info/v3/details")
        info = info or {}
        return {"account": str(info.get("portalId") or ""),
                "name": str(info.get("companyName") or info.get("uiDomain") or "")}

    # --- reads ------------------------------------------------------------------------
    def _max_pages(self) -> int:
        try:
            return max(1, int(os.environ.get("QUILL_HUBSPOT_MAX_PAGES", "5")))
        except ValueError:
            return 5

    def _search(self, obj: str, *, since: float, modified_prop: str,
                props: tuple[str, ...]) -> list[dict]:
        out: list[dict] = []
        after = None
        for _ in range(self._max_pages()):
            body: dict[str, Any] = {
                "filterGroups": [{"filters": [{
                    "propertyName": modified_prop, "operator": "GTE",
                    "value": str(int(since * 1000))}]}],
                "sorts": [{"propertyName": modified_prop, "direction": "DESCENDING"}],
                "properties": list(props), "limit": PAGE,
            }
            if after:
                body["after"] = after
            data, _ = self._request(f"{API}/crm/v3/objects/{obj}/search",
                                    method="POST", body=body)
            data = data or {}
            out.extend(r for r in (data.get("results") or []) if isinstance(r, dict))
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
        return out

    def fetch_contacts(self, *, since: float) -> list[dict]:
        return self._search("contacts", since=since, modified_prop="lastmodifieddate",
                            props=_CONTACT_PROPS)

    def fetch_deals(self, *, since: float) -> list[dict]:
        return self._search("deals", since=since, modified_prop="hs_lastmodifieddate",
                            props=_DEAL_PROPS)

    def fetch_companies(self, *, since: float) -> list[dict]:
        return self._search("companies", since=since,
                            modified_prop="hs_lastmodifieddate", props=_COMPANY_PROPS)

    def fetch_pipelines(self) -> dict[str, dict[str, str]]:
        """{"pipelines": {id: label}, "stages": {id: label}} — best effort."""
        out = {"pipelines": {}, "stages": {}}
        try:
            data, _ = self._request(f"{API}/crm/v3/pipelines/deals")
        except Exception:
            return out
        for p in ((data or {}).get("results") or []):
            if not isinstance(p, dict):
                continue
            if p.get("id"):
                out["pipelines"][str(p["id"])] = str(p.get("label") or p["id"])
            for s in p.get("stages") or []:
                if isinstance(s, dict) and s.get("id"):
                    out["stages"][str(s["id"])] = str(s.get("label") or s["id"])
        return out

    def fetch_owners(self) -> dict[str, str]:
        """owner id → name — best effort, one page."""
        out: dict[str, str] = {}
        try:
            data, _ = self._request(f"{API}/crm/v3/owners/?limit=100")
        except Exception:
            return out
        for o in ((data or {}).get("results") or []):
            if not isinstance(o, dict) or not o.get("id"):
                continue
            name = " ".join(s for s in (o.get("firstName"), o.get("lastName")) if s)
            out[str(o["id"])] = name or str(o.get("email") or o["id"])
        return out

    def portal_id(self) -> str:
        try:
            return str(self._whoami().get("account") or "")
        except Exception:
            return ""

    # --- mapping ----------------------------------------------------------------------
    @staticmethod
    def _p(rec: dict, key: str) -> str:
        v = (rec.get("properties") or {}).get(key)
        return str(v).strip() if v not in (None, "") else ""

    def _record_url(self, portal: str, type_id: str, oid: str) -> str:
        return (f"https://app.hubspot.com/contacts/{portal}/record/{type_id}/{oid}"
                if portal and oid else "")

    def _ts(self, rec: dict, prop: str) -> float:
        return (parse_iso(self._p(rec, prop)) or parse_iso(rec.get("updatedAt"))
                or parse_iso(rec.get("createdAt")))

    def _contact_item(self, r: dict, *, owners: dict, portal: str) -> dict:
        name = " ".join(s for s in (self._p(r, "firstname"), self._p(r, "lastname")) if s)
        email = self._p(r, "email")
        title = name or email or f"contact {r.get('id')}"
        owner = owners.get(self._p(r, "hubspot_owner_id"), "")
        bits = [email, self._p(r, "jobtitle"),
                f"at {self._p(r, 'company')}" if self._p(r, "company") else "",
                f"Lifecycle: {self._p(r, 'lifecyclestage')}" if self._p(r, "lifecyclestage") else "",
                f"Owner: {owner}" if owner else ""]
        url = self._record_url(portal, "0-1", str(r.get("id") or ""))
        lines = [f"HubSpot contact {title}", " · ".join(b for b in bits if b), url]
        people = [p for p in (name, owner) if p]
        return {
            "kind": "contact", "external_id": f"contact:{r.get('id')}",
            "ts": self._ts(r, "lastmodifieddate"), "title": title,
            "text": "\n".join(l for l in lines if l), "people": people,
            "url": url or None,
            "meta": {"email": email, "company": self._p(r, "company"),
                     "job_title": self._p(r, "jobtitle"),
                     "lifecycle": self._p(r, "lifecyclestage"), "owner": owner,
                     "phone": self._p(r, "phone")},
        }

    def _deal_item(self, r: dict, *, owners: dict, pipes: dict, portal: str) -> dict:
        name = clip(self._p(r, "dealname"), 200) or f"deal {r.get('id')}"
        stage_id = self._p(r, "dealstage")
        pipe_id = self._p(r, "pipeline")
        stage = pipes.get("stages", {}).get(stage_id, stage_id)
        pipeline = pipes.get("pipelines", {}).get(pipe_id, pipe_id)
        owner = owners.get(self._p(r, "hubspot_owner_id"), "")
        amount = self._p(r, "amount")
        close = self._p(r, "closedate")
        bits = [f"Stage: {stage}" if stage else "",
                f"Pipeline: {pipeline}" if pipeline and pipeline != "default" else "",
                f"Amount: {amount}" if amount else "",
                f"Close: {close[:10]}" if close else "",
                f"Owner: {owner}" if owner else ""]
        url = self._record_url(portal, "0-3", str(r.get("id") or ""))
        lines = [f"HubSpot deal {name}", " · ".join(b for b in bits if b), url]
        return {
            "kind": "deal", "external_id": f"deal:{r.get('id')}",
            "thread_id": pipeline or None,
            "ts": self._ts(r, "hs_lastmodifieddate"), "title": name,
            "text": "\n".join(l for l in lines if l),
            "people": [owner] if owner else [], "url": url or None,
            "meta": {"stage": stage, "stage_id": stage_id, "pipeline": pipeline,
                     "amount": amount, "close_date": close, "owner": owner},
        }

    def _company_item(self, r: dict, *, owners: dict, portal: str) -> dict:
        name = clip(self._p(r, "name"), 200) or self._p(r, "domain") \
            or f"company {r.get('id')}"
        owner = owners.get(self._p(r, "hubspot_owner_id"), "")
        where = ", ".join(s for s in (self._p(r, "city"), self._p(r, "country")) if s)
        bits = [self._p(r, "domain"), self._p(r, "industry"), where,
                f"Owner: {owner}" if owner else ""]
        url = self._record_url(portal, "0-2", str(r.get("id") or ""))
        lines = [f"HubSpot company {name}", " · ".join(b for b in bits if b), url]
        return {
            "kind": "company", "external_id": f"company:{r.get('id')}",
            "ts": self._ts(r, "hs_lastmodifieddate"), "title": name,
            "text": "\n".join(l for l in lines if l),
            "people": [owner] if owner else [], "entities": [name],
            "url": url or None,
            "meta": {"domain": self._p(r, "domain"), "industry": self._p(r, "industry"),
                     "owner": owner},
        }

    # --- background capture ---------------------------------------------------------
    def fetch_items(self, *, cursor: dict | None = None,
                    now: float | None = None) -> tuple[list[dict], dict]:
        from app.services.connectors.oauth_base import FETCH_OVERLAP_S
        now = float(now if now is not None else time.time())
        cursor = dict(cursor or {})
        since = self._since(cursor, now)
        contacts = self.fetch_contacts(since=since)
        deals = self.fetch_deals(since=since)
        companies = self.fetch_companies(since=since)
        owners = self.fetch_owners() if (contacts or deals or companies) else {}
        pipes = self.fetch_pipelines() if deals else {"pipelines": {}, "stages": {}}
        portal = str(cursor.get("portal_id") or "") or (
            self.portal_id() if (contacts or deals or companies) else "")
        items = [self._contact_item(r, owners=owners, portal=portal) for r in contacts]
        items += [self._deal_item(r, owners=owners, pipes=pipes, portal=portal)
                  for r in deals]
        items += [self._company_item(r, owners=owners, portal=portal) for r in companies]
        self._note_fetch(items, now)
        nxt = {**cursor, "since": now - FETCH_OVERLAP_S}
        if portal:
            nxt["portal_id"] = portal
        return items, nxt


hubspot = HubspotConnector()
