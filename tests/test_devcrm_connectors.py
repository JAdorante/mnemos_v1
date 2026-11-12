"""GitHub, Linear and HubSpot connectors — registry, the shared OAuth base
(redirect state, exchange, refresh, scope guard, env-token mode), each
provider's read mapping against fake HTTP, landing through the scheduler,
and the generic OAuth callback."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import connectors
from app.services import exhaust_ingest as ex
from app.services.connectors import github as gh
from app.services.connectors import hubspot as hs
from app.services.connectors import linear as ln
from app.services.connectors import oauth_base as ob

NOW = 1_762_800_000.0  # 2025-11-10ish, fixed
ISO_RECENT = "2025-11-10T12:00:00Z"


class RegistryTests(unittest.TestCase):
    def test_all_three_ready_not_planned(self) -> None:
        from app.services.connectors.planned import PLANNED
        for cid, cls, tool in (("github", gh.GithubConnector, "GitHub"),
                               ("linear", ln.LinearConnector, "Linear"),
                               ("hubspot", hs.HubspotConnector, "HubSpot")):
            c = connectors.get(cid)
            self.assertIsInstance(c, cls)
            self.assertEqual(c.availability, "ready")
            self.assertEqual(connectors.for_tool(tool).id, cid)
            self.assertNotIn(cid, [p.id for p in PLANNED])
            self.assertEqual(connectors.tool_status_map()[tool]["availability"],
                             "ready")

    def test_directory_cards(self) -> None:
        d = connectors.directory()
        rows = {c["id"]: c for c in d["connectors"]}
        self.assertEqual(rows["github"]["category"], "dev")
        self.assertEqual(rows["linear"]["category"], "dev")
        self.assertEqual(rows["hubspot"]["category"], "crm")
        for cid in ("github", "linear", "hubspot"):
            self.assertNotIn("planned", rows[cid]["description"].lower())
            self.assertTrue(rows[cid]["capabilities"])

    def test_source_prefixes_and_callback_exemption(self) -> None:
        from app.services import api_auth
        from app.services.connectors import session
        prefixes = session.source_prefixes()
        self.assertIn("github.pull_request", prefixes["github"])
        self.assertIn("linear.project", prefixes["linear"])
        self.assertIn("hubspot.deal", prefixes["hubspot"])
        for cid in ("github", "linear", "hubspot"):
            self.assertTrue(api_auth.path_is_exempt(f"/oauth/{cid}/callback", "GET"))
        self.assertFalse(api_auth.path_is_exempt("/oauth/nosuch/callback", "GET"))

    def test_unconfigured_status_names_the_vars(self) -> None:
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}, clear=False), \
             patch.object(gh.GithubConnector, "_cfg",
                          lambda self: SimpleNamespace(client_id="", client_secret="",
                                                       scopes="",
                                                       token_path="/nonexistent/t.json",
                                                       oauth_state_path="/nonexistent/s.json")):
            st = gh.github.status()
        self.assertFalse(st["configured"])
        self.assertIn("GITHUB_OAUTH_CLIENT_ID", st["setup_hint"])
        self.assertIn("GITHUB_TOKEN", st["setup_hint"])


class _Sandbox(unittest.TestCase):
    """Per-connector token/state files under a temp dir, a confidential
    client configured, no env tokens."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data = Path(self._td.name)
        self._env = patch.dict(os.environ, {
            "GITHUB_TOKEN": "", "LINEAR_API_KEY": "", "HUBSPOT_ACCESS_TOKEN": "",
            "QUILL_PUBLIC_BASE_URL": "", "QUILL_PEER_BASE_URL": "",
            "QUILL_OAUTH_REDIRECT_BASE": ""}, clear=False)
        self._env.start(); self.addCleanup(self._env.stop)
        for mod, cls, scopes in ((gh, gh.GithubConnector, "repo read:org read:user"),
                                 (ln, ln.LinearConnector, "read"),
                                 (hs, hs.HubspotConnector,
                                  "oauth crm.objects.contacts.read "
                                  "crm.objects.deals.read crm.objects.companies.read "
                                  "crm.objects.owners.read")):
            cid = cls.id
            cfg = SimpleNamespace(
                client_id=f"{cid}-client", client_secret=f"{cid}-secret", scopes=scopes,
                token_path=str(self.data / "connectors" / cid / "token.json"),
                oauth_state_path=str(self.data / "connectors" / cid / "oauth_state.json"))
            p = patch.object(cls, "_cfg", lambda self, _c=cfg: _c)
            p.start(); self.addCleanup(p.stop)
        # Fresh last-check counters.
        for c in (gh.github, ln.linear, hs.hubspot):
            c._last_counts = {}


class OAuthBaseTests(_Sandbox):
    def test_begin_redirect_writes_state_and_provider_auth_url(self) -> None:
        r = gh.github.begin_connect(public_base="https://seat.example.com",
                                    return_path="/privacy")
        self.assertTrue(r["ok"]); self.assertEqual(r["mode"], "redirect")
        self.assertTrue(r["auth_url"].startswith("https://github.com/login/oauth/authorize?"))
        self.assertIn("scope=repo+read%3Aorg+read%3Auser", r["auth_url"])
        self.assertIn("redirect_uri=https%3A%2F%2Fseat.example.com%2Foauth%2Fgithub%2Fcallback",
                      r["auth_url"])
        self.assertEqual(r["return_path"], "/privacy")
        self.assertTrue(gh.github.has_oauth_state(r["state"]))
        self.assertEqual(gh.github.state_return_origin(r["state"]),
                         "https://seat.example.com")

    def test_linear_and_hubspot_auth_urls(self) -> None:
        r = ln.linear.begin_connect(public_base="https://seat.example.com")
        self.assertIn("linear.app/oauth/authorize?", r["auth_url"])
        self.assertIn("scope=read", r["auth_url"]); self.assertIn("prompt=consent", r["auth_url"])
        r = hs.hubspot.begin_connect(public_base="https://seat.example.com")
        self.assertIn("app.hubspot.com/oauth/authorize?", r["auth_url"])
        self.assertIn("crm.objects.deals.read", r["auth_url"])

    def test_relay_anchor_owns_the_redirect_uri(self) -> None:
        r = gh.github.start_oauth_redirect("https://rot-1.trycloudflare.com",
                                           redirect_base="https://gb10.ts.net")
        self.assertEqual(r["redirect_uri"], "https://gb10.ts.net/oauth/github/callback")
        self.assertEqual(r["return_origin"], "https://rot-1.trycloudflare.com")

    def test_secret_required_and_http_base_refused(self) -> None:
        cfg = gh.github._cfg()
        cfg.client_secret = ""
        self.assertFalse(gh.github.configured())
        r = gh.github.begin_connect(public_base="https://seat.example.com")
        self.assertFalse(r["ok"]); self.assertTrue(r.get("skip"))
        cfg.client_secret = "github-secret"
        r = gh.github.start_oauth_redirect("http://seat.example.com")
        self.assertFalse(r["ok"])

    def test_complete_consumes_state_and_saves_tokens(self) -> None:
        start = gh.github.start_oauth_redirect("https://seat.example.com")
        captured = {}

        def fake_exchange(code, redirect):
            captured["code"], captured["redirect"] = code, redirect
            return {"access_token": "gho_x", "scope": "repo,read:org,read:user",
                    "token_type": "bearer"}

        with patch.object(gh.GithubConnector, "_exchange_code",
                          lambda self, c, r: fake_exchange(c, r)):
            r = gh.github.complete_connect("abc", start["state"], redirect_uri="")
        self.assertTrue(r["ok"])
        self.assertEqual(captured["redirect"], "https://seat.example.com/oauth/github/callback")
        self.assertTrue(gh.github.connected())
        self.assertEqual(gh.github.auth_mode(), "oauth")
        self.assertFalse(gh.github.has_oauth_state(start["state"]))
        r2 = gh.github.complete_connect("abc", start["state"], redirect_uri="")
        self.assertFalse(r2["ok"])
        gh.github.disconnect()
        self.assertFalse(gh.github.connected())

    def test_token_request_sends_secret_and_refuses_errors(self) -> None:
        captured = {}

        class _Resp:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.body

        def fake_urlopen(req, timeout=30):
            captured["body"] = req.data.decode(); captured["url"] = req.full_url
            captured["accept"] = req.get_header("Accept")
            return _Resp(captured.pop("resp"))

        with patch.object(ob, "urlopen", fake_urlopen):
            captured["resp"] = b'{"access_token":"a","scope":"read"}'
            ln.linear._exchange_code("c", "https://x/oauth/linear/callback")
            self.assertEqual(captured["url"], "https://api.linear.app/oauth/token")
            self.assertIn("client_secret=linear-secret", captured["body"])
            self.assertIn("grant_type=authorization_code", captured["body"])
            self.assertEqual(captured["accept"], "application/json")
            captured["resp"] = b'{"error":"bad_verification_code","error_description":"nope"}'
            with self.assertRaises(RuntimeError):
                gh.github._exchange_code("c", "https://x/oauth/github/callback")

    def test_scope_guard(self) -> None:
        gh.github.assert_scopes("repo,read:org,read:user")
        gh.github.assert_scopes("read:user")  # user narrowed: fine
        gh.github.assert_scopes(None)         # provider that does not echo
        with self.assertRaises(PermissionError):
            gh.github.assert_scopes("repo,delete_repo")
        with self.assertRaises(PermissionError):
            ln.linear.assert_scopes(["read", "write"])

    def test_access_token_refreshes_when_stale(self) -> None:
        hs.hubspot._save_tokens({"access_token": "old", "refresh_token": "rt",
                                 "expires_in": 1800})
        tok = hs.hubspot.load_tokens(); tok["obtained_at"] = NOW - 7200
        hs.hubspot._save_json(hs.hubspot._token_path(), tok)
        with patch.object(hs.HubspotConnector, "_refresh_token",
                          lambda self, r: {"access_token": "new", "expires_in": 1800}):
            self.assertEqual(hs.hubspot._access_token(), "new")
        self.assertEqual(hs.hubspot.load_tokens()["refresh_token"], "rt")
        # GitHub OAuth-app tokens carry no expiry: never refreshed.
        gh.github._save_tokens({"access_token": "gho_x"})
        self.assertEqual(gh.github._access_token(), "gho_x")

    def test_env_token_mode_connects_verifies_and_opts_out(self) -> None:
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_env"}, clear=False):
            self.assertTrue(gh.github.configured())
            self.assertTrue(gh.github.connected())
            with patch.object(gh.GithubConnector, "_whoami",
                              lambda self: {"account": "octocat"}):
                r = gh.github.begin_connect(public_base="https://seat.example.com")
            self.assertEqual(r, {"ok": True, "mode": "token", "account": "octocat"})
            self.assertEqual(gh.github._auth_headers()["Authorization"], "Bearer ghp_env")
            r = gh.github.disconnect()
            self.assertTrue(r["env_token_opted_out"])
            self.assertFalse(gh.github.connected())
            self.assertIsNone(gh.github.auth_mode())
            # A refused token leaves the connector disconnected.
            def boom(self): raise ob.OAuthApiError(401, "bad credentials")
            with patch.object(gh.GithubConnector, "_whoami", boom):
                r = gh.github.begin_connect()
            self.assertFalse(r["ok"]); self.assertIn("GITHUB_TOKEN", r["error"])
            self.assertFalse(gh.github.connected())

    def test_linear_api_key_goes_bare(self) -> None:
        with patch.dict(os.environ, {"LINEAR_API_KEY": "lin_api_x"}, clear=False):
            self.assertEqual(ln.linear._auth_headers()["Authorization"], "lin_api_x")


# --- GitHub ----------------------------------------------------------------------------
GH_ISSUES_P1 = [
    {"id": 1, "number": 42, "title": "Flaky login test", "state": "open",
     "updated_at": ISO_RECENT, "created_at": "2025-11-01T00:00:00Z",
     "html_url": "https://github.com/acme/web/issues/42", "comments": 3,
     "user": {"login": "priya"}, "assignees": [{"login": "sam"}],
     "labels": [{"name": "bug"}, {"name": "p1"}],
     "repository": {"full_name": "acme/web"},
     "body": "Fails on CI about one run in five.\r\nSee logs."},
    {"id": 2, "number": 7, "title": "Add retry", "state": "closed",
     "updated_at": ISO_RECENT, "created_at": "2025-11-02T00:00:00Z",
     "html_url": "https://github.com/acme/web/pull/7", "draft": False,
     "pull_request": {"merged_at": "2025-11-09T00:00:00Z"},
     "user": {"login": "sam"}, "assignees": [],
     "repository_url": "https://api.github.com/repos/acme/web"},
]
GH_ISSUES_P2 = [
    {"id": 3, "number": 1, "title": "Ancient", "state": "open",
     "updated_at": "2024-01-01T00:00:00Z", "html_url": "https://github.com/acme/old/issues/1",
     "user": {"login": "x"}, "repository": {"full_name": "acme/old"}},
]
GH_REPOS = [
    {"full_name": "acme/web", "description": "The web app", "private": True,
     "language": "TypeScript", "default_branch": "main", "open_issues_count": 12,
     "pushed_at": ISO_RECENT, "html_url": "https://github.com/acme/web",
     "owner": {"login": "acme", "type": "Organization"}},
    {"full_name": "sam/dotfiles", "private": False, "pushed_at": "2024-01-01T00:00:00Z",
     "html_url": "https://github.com/sam/dotfiles", "owner": {"login": "sam", "type": "User"}},
]


def _fake_github(urls: list):
    def req(self, url, *, method="GET", body=None, headers=None):
        urls.append(url)
        if "/issues?" in url:
            if "page=2" in url:
                return GH_ISSUES_P2, {}
            return GH_ISSUES_P1, {"link": '<https://api.github.com/issues?page=2>; rel="next"'}
        if "/user/repos?" in url:
            return GH_REPOS, {}
        if url.endswith("/user"):
            return {"login": "sam", "name": "Sam Ortiz"}, {}
        raise AssertionError(url)
    return req


class GithubMappingTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        gh.github._save_tokens({"access_token": "gho_x"})

    def test_issues_prs_repos_shape_and_pagination(self) -> None:
        urls: list = []
        with patch.object(gh.GithubConnector, "_request", _fake_github(urls)):
            items, cursor = gh.github.fetch_items(cursor={}, now=NOW)
        self.assertEqual([u for u in urls if "/issues?" in u][0].count("since="), 1)
        kinds = [(i["kind"], i["external_id"]) for i in items]
        self.assertEqual(kinds, [("issue", "acme/web#42"), ("pull_request", "acme/web#7"),
                                 ("repo", "repo:acme/web")])
        issue = items[0]
        self.assertEqual(issue["thread_id"], "acme/web")
        self.assertEqual(issue["people"], ["priya", "sam"])
        self.assertEqual(issue["labels"], ["bug", "p1"])
        self.assertIn("GitHub issue acme/web#42: Flaky login test", issue["text"])
        self.assertIn("Fails on CI", issue["text"])
        self.assertNotIn("body", issue)  # text only: SYSTEM modality, no body consent
        self.assertEqual(items[1]["meta"]["state"], "merged")
        self.assertEqual(items[2]["people"], [])  # org-owned repo names nobody
        self.assertLess(cursor["since"], NOW)
        self.assertGreater(cursor["since"], NOW - 3600)
        self.assertIn("1 issue, 1 pull request, 1 repo", gh.github.status()["status_detail"])

    def test_whoami_and_headers(self) -> None:
        with patch.object(gh.GithubConnector, "_request", _fake_github([])):
            self.assertEqual(gh.github._whoami()["account"], "sam")
        h = gh.github._auth_headers()
        self.assertEqual(h["Accept"], "application/vnd.github+json")
        self.assertIn("X-GitHub-Api-Version", h)


# --- Linear ----------------------------------------------------------------------------
def _linear_issue(ident, title, updated=ISO_RECENT, **kw):
    n = {"id": f"id-{ident}", "identifier": ident, "title": title, "url": f"https://linear.app/acme/issue/{ident}",
         "updatedAt": updated, "createdAt": "2025-11-01T00:00:00Z", "priority": 2,
         "priorityLabel": "High", "state": {"name": "In Progress", "type": "started"},
         "assignee": {"name": "Priya Natarajan", "email": "priya@acme.com"},
         "creator": {"name": "Sam Ortiz"}, "team": {"key": "ENG", "name": "Engineering"},
         "project": {"name": "Q4 launch"}, "labels": {"nodes": [{"name": "backend"}]},
         "description": "Do the thing."}
    n.update(kw)
    return n


def _fake_linear(queries: list, *, project_status_ok: bool = True):
    def gql(self, query, variables=None):
        queries.append((query, dict(variables or {})))
        after = (variables or {}).get("after")
        if "viewer" in query:
            return {"viewer": {"name": "Sam", "email": "sam@acme.com"}}
        if query.lstrip().startswith("query Issues"):
            if after is None:
                return {"issues": {"nodes": [_linear_issue("ENG-12", "Rate limiter")],
                                   "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}
            return {"issues": {"nodes": [_linear_issue("ENG-13", "Cleanup", assignee=None)],
                               "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        if query.lstrip().startswith("query Projects"):
            if "status { name }" in query and not project_status_ok:
                raise ob.OAuthApiError(200, 'Cannot query field "status" on type "Project".')
            node = {"id": "p1", "name": "Q4 launch", "url": "https://linear.app/acme/project/q4",
                    "updatedAt": ISO_RECENT, "targetDate": "2025-12-15", "progress": 0.4,
                    "lead": {"name": "Priya Natarajan"}, "teams": {"nodes": [{"key": "ENG"}]},
                    "description": "Ship it."}
            if "status { name }" in query:
                node["status"] = {"name": "In Progress"}
            else:
                node["state"] = "started"
            return {"projects": {"nodes": [node],
                                 "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        raise AssertionError(query)
    return gql


class LinearMappingTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        ln.linear._save_tokens({"access_token": "lin_oauth_x", "expires_in": 315360000})

    def test_issues_and_projects_shape_and_paging(self) -> None:
        queries: list = []
        with patch.object(ln.LinearConnector, "_gql", _fake_linear(queries)):
            items, cursor = ln.linear.fetch_items(cursor={"since": NOW - 86400}, now=NOW)
        self.assertEqual([i["kind"] for i in items], ["issue", "issue", "project"])
        issue = items[0]
        self.assertEqual(issue["external_id"], "ENG-12")
        self.assertEqual(issue["thread_id"], "ENG")
        self.assertEqual(issue["people"], ["Priya Natarajan", "Sam Ortiz"])
        self.assertIn("Linear issue ENG-12: Rate limiter", issue["text"])
        self.assertIn("Status: In Progress · Priority: High · Assignee: Priya Natarajan",
                      issue["text"])
        self.assertEqual(issue["meta"]["project"], "Q4 launch")
        self.assertEqual(items[1]["people"], ["Sam Ortiz"])
        proj = items[2]
        self.assertEqual(proj["external_id"], "project:p1")
        self.assertIn("Status: In Progress · 40% done · Lead: Priya Natarajan · Target: 2025-12-15",
                      proj["text"])
        # The since filter is inlined as an ISO string; paging uses the cursor.
        first_q, first_v = queries[0]
        self.assertIn('updatedAt: {gt: "2025-11-', first_q)
        self.assertEqual([v.get("after") for q, v in queries if q.lstrip().startswith("query Issues")],
                         [None, "c1"])

    def test_project_status_falls_back_to_state(self) -> None:
        queries: list = []
        with patch.object(ln.LinearConnector, "_gql",
                          _fake_linear(queries, project_status_ok=False)):
            rows = ln.linear.fetch_projects(since=NOW - 86400)
        self.assertEqual(rows[0]["state"], "started")
        self.assertEqual(ln.linear._project_item(rows[0])["meta"]["status"], "started")

    def test_graphql_errors_surface(self) -> None:
        with patch.object(ln.LinearConnector, "_request",
                          lambda self, url, **kw: ({"errors": [{"message": "unauthorized"}]}, {})):
            with self.assertRaises(ob.OAuthApiError):
                ln.linear._whoami()


# --- HubSpot ---------------------------------------------------------------------------
HS_CONTACTS = {"results": [
    {"id": "101", "updatedAt": ISO_RECENT, "properties": {
        "firstname": "Priya", "lastname": "Natarajan", "email": "priya@contoso.com",
        "company": "Contoso", "jobtitle": "VP Ops", "lifecyclestage": "customer",
        "hubspot_owner_id": "9", "lastmodifieddate": ISO_RECENT}}],
    "paging": {"next": {"after": "101"}}}
HS_CONTACTS_2 = {"results": [
    {"id": "102", "updatedAt": ISO_RECENT, "properties": {"email": "lead@x.com",
                                                          "lastmodifieddate": ISO_RECENT}}]}
HS_DEALS = {"results": [
    {"id": "501", "updatedAt": ISO_RECENT, "properties": {
        "dealname": "Contoso renewal", "dealstage": "st-3", "pipeline": "pl-1",
        "amount": "48000", "closedate": "2025-12-31T00:00:00Z", "hubspot_owner_id": "9",
        "hs_lastmodifieddate": "1762776000000"}}]}
HS_COMPANIES = {"results": [
    {"id": "301", "updatedAt": ISO_RECENT, "properties": {
        "name": "Contoso", "domain": "contoso.com", "industry": "Logistics",
        "city": "Austin", "country": "US", "hs_lastmodifieddate": ISO_RECENT}}]}


def _fake_hubspot(calls: list, *, owners_ok: bool = True):
    def req(self, url, *, method="GET", body=None, headers=None):
        calls.append((method, url, body))
        if url.endswith("/contacts/search"):
            return (HS_CONTACTS_2 if (body or {}).get("after") else HS_CONTACTS), {}
        if url.endswith("/deals/search"):
            return HS_DEALS, {}
        if url.endswith("/companies/search"):
            return HS_COMPANIES, {}
        if "/pipelines/deals" in url:
            return {"results": [{"id": "pl-1", "label": "Sales",
                                 "stages": [{"id": "st-3", "label": "Contract sent"}]}]}, {}
        if "/owners/" in url:
            if not owners_ok:
                raise ob.OAuthApiError(403, "missing scope")
            return {"results": [{"id": "9", "firstName": "Sam", "lastName": "Ortiz"}]}, {}
        if "/account-info/" in url:
            return {"portalId": 777, "companyName": "Acme"}, {}
        raise AssertionError(url)
    return req


class HubspotMappingTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        hs.hubspot._save_tokens({"access_token": "hs_x", "refresh_token": "r",
                                 "expires_in": 1800})

    def test_contacts_deals_companies_shape(self) -> None:
        calls: list = []
        with patch.object(hs.HubspotConnector, "_request", _fake_hubspot(calls)):
            items, cursor = hs.hubspot.fetch_items(cursor={"since": NOW - 86400}, now=NOW)
        self.assertEqual([(i["kind"], i["external_id"]) for i in items],
                         [("contact", "contact:101"), ("contact", "contact:102"),
                          ("deal", "deal:501"), ("company", "company:301")])
        c = items[0]
        self.assertEqual(c["title"], "Priya Natarajan")
        self.assertEqual(c["people"], ["Priya Natarajan", "Sam Ortiz"])
        self.assertIn("priya@contoso.com · VP Ops · at Contoso · Lifecycle: customer · Owner: Sam Ortiz",
                      c["text"])
        self.assertEqual(c["url"], "https://app.hubspot.com/contacts/777/record/0-1/101")
        self.assertEqual(items[1]["title"], "lead@x.com")
        d = items[2]
        self.assertIn("Stage: Contract sent · Pipeline: Sales · Amount: 48000 · Close: 2025-12-31 · Owner: Sam Ortiz",
                      d["text"])
        self.assertEqual(d["thread_id"], "Sales")
        self.assertAlmostEqual(d["ts"], 1762776000.0)
        self.assertEqual(items[3]["entities"], ["Contoso"])
        self.assertEqual(cursor["portal_id"], "777")
        # Search bodies: GTE on the modified property in ms, paged with `after`.
        searches = [(u, b) for m, u, b in calls if u.endswith("/search")]
        f = searches[0][1]["filterGroups"][0]["filters"][0]
        self.assertEqual((f["propertyName"], f["operator"], f["value"]),
                         ("lastmodifieddate", "GTE", str(int((NOW - 86400) * 1000))))
        self.assertEqual(searches[1][1].get("after"), "101")
        self.assertEqual(searches[2][1]["filterGroups"][0]["filters"][0]["propertyName"],
                         "hs_lastmodifieddate")

    def test_owner_lookup_failure_degrades_to_ids(self) -> None:
        with patch.object(hs.HubspotConnector, "_request", _fake_hubspot([], owners_ok=False)):
            items, _ = hs.hubspot.fetch_items(cursor={"since": NOW - 86400,
                                                      "portal_id": "777"}, now=NOW)
        self.assertEqual(items[0]["people"], ["Priya Natarajan"])
        self.assertNotIn("Owner:", items[2]["text"])

    def test_empty_window_makes_no_lookups(self) -> None:
        calls: list = []

        def req(self, url, **kw):
            calls.append(url); return {"results": []}, {}
        with patch.object(hs.HubspotConnector, "_request", req):
            items, _ = hs.hubspot.fetch_items(cursor={"since": NOW - 60}, now=NOW)
        self.assertEqual(items, [])
        self.assertEqual(len(calls), 3)  # three searches, no owners/pipelines/account


# --- landing + scheduler --------------------------------------------------------------
class LandingTests(_Sandbox):
    def _store(self):
        from app.storage import Store
        return Store(self.data / "t.db")

    def test_items_land_as_observed_events_and_resync_is_flat(self) -> None:
        from app.services.connectors import scheduler
        gh.github._save_tokens({"access_token": "gho_x"})
        store = self._store()
        with patch.object(gh.GithubConnector, "_request", _fake_github([])):
            r1 = scheduler.sync_connector(gh.github, store=store, now=NOW, force=True)
            r2 = scheduler.sync_connector(gh.github, store=store, now=NOW + 600, force=True)
        self.assertEqual(r1["landed"], 3)
        self.assertEqual(r2["landed"], 0)
        rows = store.recent_events(source_substr="github.", limit=10)
        sources = sorted(r["source"] for r in rows)
        self.assertEqual(sources, ["github.issue", "github.pull_request", "github.repo"])
        issue = next(r for r in rows if r["source"] == "github.issue")
        meta = issue["meta"]
        self.assertEqual(meta["connector_id"], "github")
        self.assertEqual(meta["external_id"], "acme/web#42")
        self.assertTrue(meta["never_authorizes"])
        self.assertEqual(meta["url"], "https://github.com/acme/web/issues/42")
        self.assertEqual(meta["labels"], ["bug", "p1"])
        self.assertEqual(meta["repo"], "acme/web")
        self.assertEqual(store.connector_sync_get("github")["items_landed"], 3)

    def test_syncable_only_when_connected_and_consented(self) -> None:
        from app.services import capture_consent
        from app.services.connectors import scheduler
        self.assertFalse(scheduler.syncable(ln.linear))
        ln.linear._save_tokens({"access_token": "x"})
        with patch.object(capture_consent, "allows_connector", lambda cid: cid == "linear"):
            self.assertTrue(scheduler.syncable(ln.linear))
            self.assertFalse(scheduler.syncable(hs.hubspot))
        sentence = scheduler.consent_sentence(hs.hubspot)
        self.assertIn("HubSpot contacts, deals and companies", sentence)
        self.assertIn("10 minutes", sentence)

    def test_manual_sync_runs_scheduler_in_background(self) -> None:
        import threading
        hs.hubspot._save_tokens({"access_token": "x"})
        done = threading.Event()
        seen = {}

        def fake_sync(connector, *, store=None, now=None, force=False):
            seen["id"], seen["force"] = connector.id, force
            done.set()
            return {"ok": True, "landed": 0}
        from app.services.connectors import scheduler
        with patch.object(scheduler, "sync_connector", fake_sync):
            r = hs.hubspot.sync()
            self.assertTrue(r.get("started"))
            self.assertTrue(done.wait(5))
        self.assertEqual(seen, {"id": "hubspot", "force": True})
        self.assertFalse(ln.linear.sync()["ok"])  # not connected


class CallbackRouteTests(_Sandbox):
    """The generic /oauth/{provider}/callback serves the three like outlook."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except Exception as exc:  # pragma: no cover
            raise unittest.SkipTest(f"fastapi TestClient unavailable: {exc}")

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.adoption import router
        app = FastAPI(); app.include_router(router)
        return TestClient(app)

    def test_callback_completes_and_returns_for_each(self) -> None:
        for mod, cls in ((gh, gh.GithubConnector), (ln, ln.LinearConnector),
                         (hs, hs.HubspotConnector)):
            c = connectors.get(cls.id)
            start = c.start_oauth_redirect("https://seat.example.com", return_path="/console")
            with patch.object(cls, "_exchange_code",
                              lambda self, code, r: {"access_token": "a", "refresh_token": "r"}):
                resp = self._client().get(
                    f"/oauth/{cls.id}/callback?code=abc&state={start['state']}",
                    headers={"origin": "https://seat.example.com"}, follow_redirects=False)
            self.assertEqual(resp.status_code, 302, cls.id)
            self.assertEqual(resp.headers["location"],
                             f"https://seat.example.com/console?connected={cls.id}")
            self.assertTrue(c.connected())

    def test_callback_relays_to_minting_origin(self) -> None:
        state = ex._encode_state("https://rot-9.trycloudflare.com")
        resp = self._client().get(f"/oauth/linear/callback?code=abc&state={state}",
                                  headers={"origin": "https://gb10.ts.net"},
                                  follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["location"].startswith(
            "https://rot-9.trycloudflare.com/oauth/linear/callback?"))

    def test_connect_route_records_consent(self) -> None:
        from app.services import capture_consent
        recorded = {}
        with patch.object(capture_consent, "record_connector",
                          lambda cid, **kw: recorded.update({cid: kw})):
            resp = self._client().post("/connectors/github/connect",
                                       headers={"x-public-base": "https://seat.example.com",
                                                "x-return-path": "/privacy"})
        body = resp.json()
        self.assertEqual(body["mode"], "redirect")
        self.assertTrue(body["background_sync"])
        self.assertIn("GitHub issues", recorded["github"]["sentence"])


class ParseTests(unittest.TestCase):
    def test_parse_iso_and_ms(self) -> None:
        self.assertEqual(ob.parse_iso("2025-11-10T12:00:00Z"), 1762776000.0)
        self.assertEqual(ob.parse_iso("2025-11-10T12:00:00.1234567Z"), 1762776000.123456)
        self.assertEqual(ob.parse_iso(1762776000000), 1762776000.0)
        self.assertEqual(ob.parse_iso("1762776000000"), 1762776000.0)
        self.assertEqual(ob.parse_iso("2025-11-10"), 1762732800.0)
        self.assertEqual(ob.parse_iso("garbage"), 0.0)
        self.assertEqual(ob.clip(" a\r\nb  ", 100), "a\nb")
        self.assertEqual(ob.clip("x" * 10, 5), "xxxx…")


if __name__ == "__main__":
    unittest.main()
