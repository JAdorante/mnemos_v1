"""Team search (step 3 of Google-style search): pointers, permissions before
ranking, prompt withdrawal, and the rung setting.

What this pins, without Postgres (tests/test_team_search_org.py runs the
same rules against the real Org Record Service):
  * a pointer carries owner, tags, type, people, time and location, and a
    title or snippet only inside a shared project; addresses never;
  * search puts each item in its tier BEFORE ranking: open only for a shared
    project both reader and owner read, otherwise at most "ask <owner>",
    matched on tags and people and naming only the reader's own words;
  * a seat withdraws a deleted item as it goes, retries a failed
    withdrawal, takes a project's pointers back when it stops sharing, and
    takes everything back when the setting goes off or the rung tightens;
  * Sealed and Perimeter never publish: a teammate's search reaches the
    seat as a pointer_query, and the owner approves each answer.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

import app.config  # noqa: E402
from app.events import Event, Modality  # noqa: E402
from app.services import egress, team_index, team_search  # noqa: E402
from app.services.records import org_client  # noqa: E402
from app.services.records import pointers as P  # noqa: E402
from app.storage import Store, remove_delete_hook  # noqa: E402

NOW = 1_794_000_000.0
TEAM, BAY, ORB = "scp_team", "scp_bayview", "scp_orbital"


def ptr(ref, owner, *, scope=None, tags=(), people=(), title=None,
        snippet=None, at=NOW, kind="email", refs=0):
    p = P.clean({"item_ref": ref, "kind": kind, "scope_id": scope,
                 "tags": list(tags), "people": list(people), "at": at,
                 "source": "google.mail", "title": title, "snippet": snippet,
                 "refs": refs})
    return {**p, "owner": owner, "node_id": f"node-{owner}"}


# --------------------------------------------------------------- shape --
class PointerShapeTests(unittest.TestCase):
    def test_no_title_or_snippet_outside_a_shared_project(self):
        p = P.clean({"item_ref": "ev:1", "kind": "email", "at": NOW,
                     "tags": ["Orbital"], "title": "Breach notice",
                     "snippet": "Orbital considers the POC a breach"})
        self.assertIsNone(p["title"])
        self.assertIsNone(p["snippet"])
        self.assertEqual(P.keys_for(p)[1], [])
        shared = P.clean({"item_ref": "ev:2", "kind": "email", "at": NOW,
                          "scope_id": BAY, "title": "Pricing - approved"})
        self.assertEqual(shared["title"], "Pricing - approved")

    def test_people_are_names_never_addresses(self):
        p = P.clean({"item_ref": "ev:1", "at": NOW,
                     "people": ["Dana Lee", "dana@x.com", "Sam <s@x.com>",
                                "dana lee"]})
        self.assertEqual(p["people"], ["Dana Lee"])

    def test_unknown_fields_and_kinds_do_not_survive(self):
        p = P.clean({"item_ref": "ev:1", "at": NOW, "kind": "salary_letter",
                     "raw": "everything", "body": "x", "owner": "someone"})
        self.assertEqual(p["kind"], "item")
        self.assertEqual(set(p), {"item_ref", "kind", "scope_id", "tags",
                                  "people", "at", "source", "refs", "title",
                                  "snippet"})
        for bad in ({"at": NOW}, {"item_ref": "ev:1"},
                    {"item_ref": "ev:1", "at": "soon"}):
            with self.assertRaises(ValueError):
                P.clean(bad)

    def test_match_keys_split_names_and_skip_generic_words(self):
        keys = P.match_keys(["Bayview Clinic Group", "The Review Project"])
        self.assertIn("bayview clinic group", keys)
        self.assertIn("bayview", keys)
        self.assertNotIn("group", keys)
        self.assertNotIn("review", keys)


class RankingTests(unittest.TestCase):
    def run_search(self, rows, q, *, reader="bob", readable=(BAY,),
                   owner_reads=None, **kw):
        owner_reads = {("dana", BAY)} if owner_reads is None else owner_reads
        return P.search(rows, q, reader=reader, readable=set(readable),
                        owner_can_read=lambda o, s: (o, s) in owner_reads,
                        owner_name=str.title, scope_name=lambda s: "Bayview",
                        **kw)

    def test_shared_project_item_opens_and_the_rest_routes(self):
        rows = [ptr("ev:1", "dana", scope=BAY, tags=["Bayview"],
                    people=["Rachel Osei"], title="Pricing - approved",
                    snippet="Core plus the intake add-on"),
                ptr("ev:2", "sam", tags=["Bayview"], people=["Rachel Osei"])]
        out = self.run_search(rows, "bayview pricing")
        self.assertEqual([p["location"]["ref"] for p in out["pointers"]],
                         ["ev:1"])
        self.assertEqual(out["pointers"][0]["title"], "Pricing - approved")
        self.assertEqual(out["ask"], [{"owner": "sam", "owner_name": "Sam",
                                       "matched": ["bayview"],
                                       "route": "ask"}])

    def test_a_routed_item_is_never_found_by_its_title(self):
        # A pointer in a project the reader cannot open keeps its text in the
        # index (for members), but that text must not route anyone.
        rows = [ptr("ev:9", "dana", scope=ORB, tags=["Orbital"],
                    title="Breach notice", snippet="considers it a breach")]
        out = self.run_search(rows, "breach notice", readable=(BAY,),
                              owner_reads={("dana", ORB)})
        self.assertEqual(out, {"pointers": [], "ask": []})
        out = self.run_search(rows, "orbital", readable=(BAY,),
                              owner_reads={("dana", ORB)})
        self.assertEqual(out["pointers"], [])
        self.assertEqual(out["ask"][0]["matched"], ["orbital"])
        self.assertNotIn("title", out["ask"][0])

    def test_owner_who_lost_the_project_only_routes(self):
        rows = [ptr("ev:1", "dana", scope=BAY, tags=["Bayview"],
                    title="Pricing - approved")]
        out = self.run_search(rows, "bayview", owner_reads=set())
        self.assertEqual(out["pointers"], [])
        self.assertEqual(out["ask"][0]["owner"], "dana")

    def test_hidden_items_do_not_change_what_the_reader_sees(self):
        open_rows = [ptr("ev:1", "dana", scope=BAY, tags=["Bayview"],
                         title="Kickoff notes"),
                     ptr("ev:2", "dana", scope=BAY, tags=["Bayview"],
                         title="Pricing", at=NOW - 50)]
        hidden = [ptr(f"ev:h{i}", "eve", tags=["Bayview"], people=["X"])
                  for i in range(40)]
        a = self.run_search(open_rows, "bayview", limit=2)
        b = self.run_search(open_rows + hidden, "bayview", limit=2)
        self.assertEqual(a["pointers"], b["pointers"])
        self.assertEqual([r["owner"] for r in b["ask"]], ["eve"])

    def test_reader_never_sees_their_own_items(self):
        rows = [ptr("ev:1", "bob", scope=BAY, tags=["Bayview"])]
        self.assertEqual(self.run_search(rows, "bayview"),
                         {"pointers": [], "ask": []})

    def test_people_and_recency_lift_the_right_item(self):
        rows = [ptr("ev:old", "dana", scope=BAY, tags=["Bayview"],
                    people=["Sam Ito"], at=NOW - 9000),
                ptr("ev:new", "dana", scope=BAY, tags=["Bayview"],
                    people=["Rachel Osei"], at=NOW)]
        out = self.run_search(rows, "latest from rachel on bayview")
        self.assertEqual(out["pointers"][0]["location"]["ref"], "ev:new")
        self.assertEqual(out["pointers"][0]["signals"]["person"], 1.0)


# ----------------------------------------------------------- seat index --
class FakeOrg:
    """The org service's pointer endpoints, recorded. `fail` makes the next
    calls look unreachable."""

    def __init__(self):
        self.pointers: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail = False
        self.common = {"member_active": True, "scopes": [{"id": BAY}]}

    def __call__(self, method, path, body, headers):
        self.calls.append((method, path, body))
        if path == "/auth/token":
            return 200, {"access_token": "t", "expires_at": time.time() + 900}
        if self.fail:
            return 503, {"detail": "down"}
        if path == "/pointers":
            for p in body["pointers"]:
                self.pointers[p["item_ref"]] = p
            return 200, {"ok": True, "published": len(body["pointers"])}
        if path == "/pointers/withdraw":
            n = sum(self.pointers.pop(r, None) is not None
                    for r in body["item_refs"])
            return 200, {"ok": True, "withdrawn": n}
        if path == "/pointers/reconcile":
            keep = set(body["item_refs"])
            gone = [r for r in self.pointers if r not in keep]
            for r in gone:
                del self.pointers[r]
            return 200, {"ok": True, "withdrawn": len(gone),
                         "kept": len(self.pointers)}
        if path == "/pointers/unshare":
            gone = [r for r, p in self.pointers.items()
                    if p.get("scope_id") == body["scope_id"]]
            for r in gone:
                del self.pointers[r]
            return 200, {"ok": True, "withdrawn": len(gone)}
        if path.startswith("/scopes/common"):
            return 200, self.common
        if path.startswith("/pointers/search"):
            return 200, {"index": "open", "pointers": [], "ask": [
                {"owner": "mem_dana", "owner_name": "Dana Lee",
                 "matched": ["bayview"], "route": "ask"}]}
        return 404, {"detail": {"code": "not_found"}}

    def paths(self):
        return [p for _m, p, _b in self.calls if p != "/auth/token"]


MEMBERSHIP = {
    "service_url": "http://org.test", "org_id": "org_1",
    "member_id": "mem_me", "node_id": "node-me", "credential": "cred",
    "rung": "connected", "team_index": "open", "policy": {},
    "scopes": [
        {"id": TEAM, "kind": "team", "name": "Team", "parent_id": "scp_root",
         "permissions": ["propose", "read"]},
        {"id": BAY, "kind": "project", "name": "Bayview", "parent_id": TEAM,
         "permissions": ["approve", "propose", "read"]}],
    "roster": [{"scope_id": TEAM, "name": "Team", "members": [
        {"member_id": "mem_me", "display_name": "Me Myself",
         "peer_url": "http://me.test"},
        {"member_id": "mem_dana", "display_name": "Dana Lee",
         "peer_url": "http://dana.test"}]}],
}


class SeatBase(unittest.TestCase):
    setting = "on"
    rung = "connected"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="team-index-")
        self.store = Store(db_path=Path(self.dir) / "quill.db",
                           audio_dir=Path(self.dir) / "audio")
        real = app.config.settings
        self.settings = dataclasses.replace(
            real, storage=dataclasses.replace(real.storage, data_dir=self.dir))
        from app.services.model_log import model_log
        self._trail = model_log._path
        model_log._path = Path(self.dir) / "model_calls.jsonl"
        self._patches = [
            mock.patch.object(app.config, "settings", self.settings),
            mock.patch.dict(os.environ, {
                "QUILL_TEAM_SEARCH": self.setting, "QUILL_DATA_DIR": self.dir,
                "QUILL_EGRESS_RUNG": self.rung}),
            mock.patch.object(egress, "_org_rung", (0.0, "")),
        ]
        for p in self._patches:
            p.start()
        self.org = FakeOrg()
        org_client.set_transport(self.org)
        org_client._save(json.loads(json.dumps(MEMBERSHIP)))
        team_index.attach(self.store)
        self.store.resolve_entity("Bayview", kind="project", ts=NOW)
        self.store.resolve_entity("Orbital", kind="project", ts=NOW)

    def tearDown(self):
        remove_delete_hook(team_index._on_store_change)
        team_index._attached_store = None
        org_client.set_transport(None)
        from app.services.model_log import model_log
        model_log._path = self._trail
        for p in reversed(self._patches):
            p.stop()
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def mail(self, subject, body, *, entities=(), people=("Rachel Osei",),
             source="google.mail", t=NOW - 3600, **meta):
        ev = Event(time=t, modality=Modality.DOCUMENT, raw=body,
                   summary=f"[{source}] {subject}", source=source,
                   people=list(people), entities=list(entities),
                   meta={"origin": "connector", "connector_id": "google",
                         "title": subject, "from": "Rachel Osei <r@bay.test>",
                         "to": ["me@ravenry.test"], **meta})
        return int(self.store.insert(ev))

    def flush_threads(self):
        import threading
        for t in threading.enumerate():
            if t.name == "team-index-withdraw":
                t.join(timeout=5)


class BuildTests(SeatBase):
    def test_shared_project_item_carries_title_and_snippet(self):
        eid = self.mail("Pricing - approved", "We agreed Core plus intake.",
                        entities=["Bayview"])
        p = team_index.build(self.store, self.store._conn.execute(
            "SELECT * FROM events WHERE id=?", (eid,)).fetchone())
        self.assertEqual(p["scope_id"], BAY)
        self.assertEqual(p["tags"], ["Bayview"])
        self.assertEqual(p["title"], "Pricing - approved")
        self.assertIn("Core plus intake", p["snippet"])
        self.assertEqual(p["people"], ["Rachel Osei"])
        self.assertEqual((p["kind"], p["source"]), ("email", "google.mail"))

    def test_private_project_item_is_a_bare_pointer(self):
        eid = self.mail("Breach notice", "Orbital considers it a breach.",
                        entities=["Orbital"])
        p = team_index.build(self.store, self.store._conn.execute(
            "SELECT * FROM events WHERE id=?", (eid,)).fetchone())
        self.assertEqual((p["scope_id"], p["title"], p["snippet"]),
                         (None, None, None))
        self.assertEqual(p["tags"], ["Orbital"])
        self.assertNotIn("Breach", json.dumps(p))

    def test_personal_sources_and_sensitive_items_get_nothing(self):
        row = lambda eid: self.store._conn.execute(  # noqa: E731
            "SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        texts = int(self.store.insert(Event(
            time=NOW, modality=Modality.TEXT, raw="see you at 6",
            source="imessage.message",
            meta={"origin": "connector", "connector_id": "imessage"})))
        screen = int(self.store.insert(Event(
            time=NOW, modality=Modality.VISION, raw="Bayview deck",
            source="screen.ocr", entities=["Bayview"])))
        secret = self.mail("Payroll", "Priya's salary is $185,000 and her "
                           "SSN is 123-45-6789.", entities=["Bayview"])
        self.assertIsNone(team_index.build(self.store, row(texts)))
        self.assertIsNone(team_index.build(self.store, row(screen)))
        self.assertEqual(self.store._conn.execute(
            "SELECT privacy_class FROM events WHERE id=?", (secret,)
        ).fetchone()[0], "never-send")
        self.assertIsNone(team_index.build(self.store, row(secret)))

    def test_title_that_fails_the_privacy_check_stays_home(self):
        # The item itself may leave (internal), but its subject line is
        # judged on its own before it goes anywhere.
        eid = self.mail("Bayview pricing", "Core plus intake.",
                        entities=["Bayview"])
        with mock.patch("app.services.privacy_class.egress_refusal",
                        return_value="privacy_class=sensitive"):
            p = team_index.build(self.store, self.store._conn.execute(
                "SELECT * FROM events WHERE id=?", (eid,)).fetchone())
        self.assertEqual(p["scope_id"], BAY)
        self.assertIsNone(p["title"])
        self.assertIsNone(p["snippet"])

    def test_accepted_note_files_under_its_project(self):
        eid = int(self.store.insert(Event(
            time=NOW, modality=Modality.TEXT, raw="Decision: ship Dec 12.",
            summary="[Claude] Decision: ship Dec 12.",
            source="assistant.claude", meta={"project": "Bayview"})))
        p = team_index.build(self.store, self.store._conn.execute(
            "SELECT * FROM events WHERE id=?", (eid,)).fetchone())
        self.assertEqual((p["kind"], p["scope_id"]), ("note", BAY))
        self.assertEqual(p["title"], "Decision: ship Dec 12.")

    def test_an_item_filed_nowhere_gets_no_pointer(self):
        # Tags are the index: untagged mail stays home, and so do its people.
        eid = self.mail("Dinner Sunday", "Mom says 6pm.", people=("Mom",))
        self.assertIsNone(team_index.build(self.store, self.store._conn.execute(
            "SELECT * FROM events WHERE id=?", (eid,)).fetchone()))

    def test_personal_class_items_get_no_pointer(self):
        eid = self.mail("Pricing", "Core.", entities=["Bayview"])
        self.store._conn.execute(
            "UPDATE events SET privacy_class='personal' WHERE id=?", (eid,))
        self.assertIsNone(team_index.build(self.store, self.store._conn.execute(
            "SELECT * FROM events WHERE id=?", (eid,)).fetchone()))

    def test_address_lists_become_names(self):
        eid = self.mail("Kickoff", "Agenda", people=(),
                        cc="Sam Ito <sam@x.test>, Lee Park <lee@y.test>",
                        entities=["Bayview"])
        p = team_index.build(self.store, self.store._conn.execute(
            "SELECT * FROM events WHERE id=?", (eid,)).fetchone())
        self.assertEqual(p["people"], ["Rachel Osei", "Sam Ito", "Lee Park"])
        self.assertNotIn("@", json.dumps(p["people"]))


class CentralSyncTests(SeatBase):
    def test_publish_then_delete_withdraws_at_once(self):
        a = self.mail("Pricing - approved", "Core plus intake.",
                      entities=["Bayview"])
        b = self.mail("Breach notice", "privileged", entities=["Orbital"])
        out = team_index.sync(self.store)
        self.assertEqual(out["mode"], "central")
        self.assertEqual(out["published"], 2)
        self.assertEqual(set(self.org.pointers), {f"ev:{a}", f"ev:{b}"})
        self.assertIsNone(self.org.pointers[f"ev:{b}"]["title"])
        self.assertEqual(self.org.pointers[f"ev:{a}"]["title"],
                         "Pricing - approved")
        # Nothing changed: nothing is sent again.
        self.org.calls.clear()
        team_index.sync(self.store)
        self.assertNotIn("/pointers", self.org.paths())

        self.store.erase_event(a)
        self.flush_threads()
        self.assertNotIn(f"ev:{a}", self.org.pointers)
        self.assertIn("/pointers/withdraw", self.org.paths())

    def test_failed_withdrawal_is_retried_before_publishing(self):
        a = self.mail("Pricing", "Core.", entities=["Bayview"])
        team_index.sync(self.store)
        self.org.fail = True
        self.store.erase_event(a)
        self.flush_threads()
        self.assertIn(f"ev:{a}", self.org.pointers)
        self.assertEqual(team_index.status()["pending"], 1)
        self.org.fail = False
        self.org.calls.clear()
        team_index.sync(self.store)
        self.assertNotIn(f"ev:{a}", self.org.pointers)
        self.assertEqual(team_index.status()["pending"], 0)
        self.assertEqual(self.org.paths()[0], "/pointers/withdraw")

    def test_a_delete_the_hook_missed_comes_out_on_the_full_pass(self):
        a = self.mail("Pricing", "Core.", entities=["Bayview"])
        team_index.sync(self.store)
        remove_delete_hook(team_index._on_store_change)
        self.store.erase_event(a)
        team_index.sync(self.store, full=True)
        self.assertNotIn(f"ev:{a}", self.org.pointers)

    def test_stop_sharing_a_project_takes_its_pointers_back(self):
        a = self.mail("Pricing - approved", "Core.", entities=["Bayview"])
        team_index.sync(self.store)
        out = team_index.set_project_shared("bayview", False)
        self.assertTrue(out["ok"], out)
        self.assertIn("/pointers/unshare", self.org.paths())
        self.assertNotIn(f"ev:{a}", self.org.pointers)
        team_index.sync(self.store)
        p = self.org.pointers[f"ev:{a}"]
        self.assertEqual((p["scope_id"], p["title"]), (None, None))
        team_index.set_project_shared("bayview", True)
        team_index.sync(self.store)
        self.assertEqual(self.org.pointers[f"ev:{a}"]["title"],
                         "Pricing - approved")

    def test_turning_the_setting_off_withdraws_everything(self):
        self.mail("Pricing", "Core.", entities=["Bayview"])
        team_index.sync(self.store)
        self.assertTrue(self.org.pointers)
        self.assertTrue(team_index.holds_published())
        with mock.patch.dict(os.environ, {"QUILL_TEAM_SEARCH": "off"}):
            out = team_index.sync(self.store)
        self.assertEqual(out["mode"], "off")
        self.assertEqual(self.org.pointers, {})
        self.assertFalse(team_index.holds_published())

    def test_rung_tightening_withdraws_and_stops_publishing(self):
        self.mail("Pricing", "Core.", entities=["Bayview"])
        team_index.sync(self.store)
        with mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": "sealed"}):
            out = team_index.sync(self.store)
            self.assertEqual(out["mode"], "federated")
            self.assertEqual(self.org.pointers, {})
            self.mail("Kickoff", "Agenda", entities=["Bayview"])
            team_index.sync(self.store)
            self.assertEqual(self.org.pointers, {})
            self.assertEqual(team_index.status()["pointers"], 2)

    def test_joining_a_project_rebuilds_old_items_with_titles(self):
        m = json.loads(json.dumps(MEMBERSHIP))
        m["scopes"] = [s for s in m["scopes"] if s["id"] != BAY]
        org_client._save(m)
        a = self.mail("Pricing - approved", "Core.", entities=["Bayview"])
        team_index.sync(self.store)
        self.assertIsNone(self.org.pointers[f"ev:{a}"]["title"])
        org_client._save(json.loads(json.dumps(MEMBERSHIP)))
        # Old items sit outside the recent-id window; only a full pass
        # reaches them, and a change of projects must trigger one.
        with mock.patch.object(team_index, "_RESCAN_IDS", 0), \
                mock.patch.object(team_index, "_FULL_S", 1e12):
            out = team_index.sync(self.store)
        self.assertTrue(out["full"])
        self.assertEqual(self.org.pointers[f"ev:{a}"]["title"],
                         "Pricing - approved")

    def test_a_large_backlog_publishes_over_several_ticks(self):
        for i in range(5):
            self.mail(f"Item {i}", "x", entities=["Bayview"])
        with mock.patch.object(team_index, "_MAX_PER_TICK", 2):
            self.assertEqual(team_index.sync(self.store)["published"], 2)
            self.assertEqual(team_index.sync(self.store)["published"], 2)
            self.assertEqual(team_index.sync(self.store)["published"], 1)
        self.assertEqual(len(self.org.pointers), 5)

    def test_reconcile_drops_what_the_seat_no_longer_has(self):
        self.mail("Pricing", "Core.", entities=["Bayview"])
        team_index.sync(self.store)
        self.org.pointers["ev:99999"] = {"item_ref": "ev:99999"}
        with team_index._lock, team_index._open() as c:
            team_index._set(c, "last_reconcile", 0)
        team_index.sync(self.store)
        self.assertNotIn("ev:99999", self.org.pointers)

    def test_grounding_lines_route_to_the_owner(self):
        lines = team_search.grounding_lines("who has the bayview pricing?")
        self.assertTrue(lines[0].startswith("TEAM SEARCH"))
        self.assertIn("Ask Dana Lee", lines[1])
        self.assertIn("“bayview”", lines[1])

    def test_setting_off_means_no_team_search_at_all(self):
        with mock.patch.dict(os.environ, {"QUILL_TEAM_SEARCH": "off"}):
            self.assertEqual(team_search.grounding_lines("bayview"), [])
            self.assertEqual(team_search.search("bayview")["mode"], "off")
        self.assertNotIn("/pointers/search", self.org.paths())


class LocalSearchTests(SeatBase):
    setting = "federated"

    def test_answer_for_a_teammate_follows_the_same_rule(self):
        self.mail("Pricing - approved", "Core plus intake.",
                  entities=["Bayview"])
        self.mail("Breach notice", "privileged", entities=["Orbital"])
        team_index.sync(self.store)
        self.assertEqual(self.org.pointers, {})          # never published
        ans = team_search.answer_for("mem_dana", "bayview orbital pricing")
        self.assertEqual(len(ans["pointers"]), 1)
        self.assertIn("Pricing - approved", ans["text"])
        self.assertIn("“orbital”", ans["text"])
        self.assertNotIn("Breach", ans["text"])

    def test_nothing_proven_shared_when_the_org_is_down(self):
        self.mail("Pricing - approved", "Core plus intake.",
                  entities=["Bayview"])
        team_index.sync(self.store)
        self.org.fail = True
        ans = team_search.answer_for("mem_dana", "bayview pricing")
        self.assertEqual(ans["pointers"], [])
        self.assertNotIn("Pricing", ans["text"])
        self.assertIn("ask directly", ans["text"])


# ------------------------------------------------- federated: peer path --
class FederatedPeerTests(SeatBase):
    """The responder side of a pointer_query under each rung."""
    setting = "federated"

    def setUp(self):
        super().setUp()
        tmp = Path(self.dir)
        self._env = mock.patch.dict(os.environ, {
            "QUILL_PEER_REGISTRY": str(tmp / "peers.json"),
            "QUILL_PEER_ASKS": str(tmp / "asks.json"),
            "QUILL_PEER_SENT": str(tmp / "sent.json"),
            "QUILL_PEER_MAILBOX": str(tmp / "mailbox.json"),
            "QUILL_PEER_TEAMS": str(tmp / "teams.json"),
            "QUILL_PEER_LOOPS": str(tmp / "loops.json"),
            "QUILL_PEER_TELEMETRY_PATH": str(tmp / "telemetry.jsonl"),
            "QUILL_PEER_CLIP_GRANTS": str(tmp / "clip_grants.json"),
            "QUILL_PEER_INGEST": "0", "QUILL_PEER_AUTO_ANSWER": "0"})
        self._env.start()
        self.mail("Pricing - approved", "Core plus intake.",
                  entities=["Bayview"])
        team_index.sync(self.store)
        self.peer = {"peer_id": "p_dana", "name": "Dana Lee",
                     "base_url": "http://dana.test"}
        self.delivered = []
        from app.services import peer_channel as pch
        self.pch = pch
        self._deliver = mock.patch.object(
            pch, "_deliver", side_effect=lambda rec, out:
            self.delivered.append(out) or True)
        self._deliver.start()
        self._announce = mock.patch.object(pch, "_announce_pending")
        self._announce.start()

    def tearDown(self):
        self._announce.stop()
        self._deliver.stop()
        self._env.stop()
        super().tearDown()

    def ask(self, q="bayview pricing"):
        return self.pch.handle_ask(self.peer, {"ask_id": "a1", "question": q,
                                               "kind": "pointer_query"})

    def test_connected_by_choice_answers_at_once(self):
        res = self.ask()
        self.assertEqual(res["status"], "answered", res)
        self.assertIn("Pricing - approved", res["answer"])

    def test_unknown_asker_gets_nothing(self):
        res = self.pch.handle_ask({**self.peer, "base_url": "http://x.test"},
                                  {"ask_id": "a2", "question": "bayview",
                                   "kind": "pointer_query"})
        self.assertEqual(res["status"], "declined")

    def test_no_match_is_a_typed_miss(self):
        res = self.ask("kubernetes")
        self.assertEqual(res["response_kind"], "null_result")
        self.assertEqual(res["null_result"]["reason"], "no_memory")

    def _approve_flow(self, rung):
        with mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": rung}):
            res = self.ask()
            self.assertEqual(res["status"], "pending", res)
            pend = self.pch.pending_asks()
            self.assertEqual(pend[0]["kind"], "pointer_query")
            self.assertIn("searching the team", self.pch._ask_line(pend[0]))
            self.assertEqual(self.delivered, [])
            self.pch._save(self.pch._peers_path(), {"p_dana": dict(self.peer)})
            out = self.pch.decide_ask(pend[0]["id"], True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.delivered[-1]["response_kind"], "answer")
        self.assertIn("Pricing - approved", self.delivered[-1]["answer"])

    def test_sealed_waits_for_the_owner(self):
        self._approve_flow("sealed")

    def test_perimeter_waits_for_the_owner_too(self):
        self._approve_flow("perimeter")


class ChatCommandTests(SeatBase):
    """Team search is a chat command that runs, not a chip."""

    def setUp(self):
        super().setUp()
        tmp = Path(self.dir)
        self._env = mock.patch.dict(os.environ, {
            "QUILL_PEER_REGISTRY": str(tmp / "peers.json"),
            "QUILL_PEER_ASKS": str(tmp / "asks.json"),
            "QUILL_PEER_SENT": str(tmp / "sent.json"),
            "QUILL_PEER_TELEMETRY_PATH": str(tmp / "telemetry.jsonl"),
            "QUILL_PEER_CLIP_GRANTS": str(tmp / "clip_grants.json")})
        self._env.start()
        from app.services import peer_channel as pch
        pch._save(pch._peers_path(), {"p_dana": {
            "name": "Dana Lee", "base_url": "http://dana.test",
            "created_at": 1.0}})

    def tearDown(self):
        self._env.stop()
        super().tearDown()

    def test_command_forms(self):
        p = team_search.parse_command
        self.assertEqual(p("/team Bayview pricing"), "Bayview pricing")
        self.assertEqual(p("team search: who owns GIS?"), "who owns GIS")
        self.assertEqual(p("Search the team for the Orbital contract."),
                         "the Orbital contract")
        self.assertEqual(p("who on my team knows about Rachel Osei?"),
                         "Rachel Osei")
        self.assertEqual(p("/team"), "")
        for not_one in ("who knows about bayview", "ask #team: pricing?",
                        "/teammates", "search for bayview",
                        "team searching is fun", "/plan book a room"):
            self.assertIsNone(p(not_one), not_one)

    def test_central_reply_routes_with_a_one_tap_ask(self):
        text, chips = team_search.command_reply("bayview pricing")
        self.assertIn("Team search for “bayview pricing”", text)
        self.assertIn("Ask Dana Lee", text)
        self.assertEqual(chips, [{
            "label": "Ask Dana Lee's Sparrow", "action": "peer.ask",
            "kind": "peer_ask", "payload": {
                "peer_id": "p_dana", "peer_name": "Dana Lee",
                "question": "bayview pricing", "kind": "question"}}])

    def test_bare_command_off_and_outside_an_org(self):
        self.assertIn("/team Bayview pricing",
                      team_search.command_reply("")[0])
        with mock.patch.dict(os.environ, {"QUILL_TEAM_SEARCH": "off"}):
            self.assertIn("isn't turned on", team_search.command_reply("x")[0])
        org_client._save({})
        self.assertIn("isn't in an org", team_search.command_reply("x")[0])

    def test_federated_reply_asks_each_teammate(self):
        replies = {"p_dana": {"ok": True, "status": "pending"}}
        with mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": "sealed"}), \
                mock.patch("app.services.peer_channel.ask",
                           side_effect=lambda pid, q, **kw: replies[pid]) as ask:
            text, chips = team_search.command_reply("bayview pricing")
        ask.assert_called_once_with("p_dana", "bayview pricing",
                                    kind="pointer_query", mint_waiting=False)
        self.assertIn("Asked 1 teammate's Sparrow about “bayview pricing”",
                      text)
        self.assertIn("Waiting for approval: Dana Lee", text)
        self.assertEqual(chips, [])

    def test_chat_route_runs_the_command_without_the_agent(self):
        from fastapi.testclient import TestClient

        from app.main import app
        from app.services import chat_log
        from app.services.agent_bridge import worker
        chat_log.reset_log(Path(self.dir) / "chat_log.db")
        with worker.lock:
            saved_events, saved_next = list(worker.events), worker.next_id
        try:
            with mock.patch.object(worker, "send") as send:
                r = TestClient(app).post(
                    "/chat", json={"message": "/team bayview pricing"},
                    headers={"Origin": "http://testserver"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["routed"], "team_search")
            send.assert_not_called()
            with worker.lock:
                got = [e for e in worker.events if e.get("kind") == "result"]
            self.assertIn("Ask Dana Lee", got[-1]["text"])
            self.assertEqual(got[-1]["stream"]["actions"][0]["action"],
                             "peer.ask")
        finally:
            with worker.lock:
                worker.events[:] = saved_events
                worker.next_id = saved_next


class EgressRuleTests(unittest.TestCase):
    def test_perimeter_lets_only_approved_pointer_answers_cross(self):
        with mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": "perimeter"}):
            no = egress.decide(path="pointers", destination="d",
                               policy="allow", text="x", log=False)
            yes = egress.decide(path="pointers", destination="d",
                                policy="allow", text="x", approved=True,
                                log=False)
            index = egress.decide(path="index", destination="org",
                                  policy="allow", approved=True, log=False)
        self.assertEqual((no.action, yes.action, index.action),
                         ("deny", "allow", "deny"))

    def test_sealed_never_auto_publishes(self):
        with mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": "sealed"}):
            v = egress.decide(path="index", destination="org", policy="allow",
                              log=False)
        self.assertEqual(v.action, "offer")


class McpToolTests(SeatBase):
    GRANT = {"id": "g1", "assistant": "claude", "label": "Claude"}

    def test_team_scope_is_required(self):
        from app.services import remote_mcp
        names = {t["name"] for t in remote_mcp.tools_for({"read"})}
        self.assertNotIn("team_search", names)
        names = {t["name"] for t in remote_mcp.tools_for({"team"})}
        self.assertIn("team_search", names)
        out = remote_mcp.call_tool({**self.GRANT, "scopes": {"read"}},
                                   "team_search", {"query": "bayview"})
        self.assertFalse(out["ok"])

    def test_assistant_gets_what_the_user_gets(self):
        from app.services import remote_mcp
        out = remote_mcp.call_tool({**self.GRANT, "scopes": {"team"}},
                                   "team_search", {"query": "bayview"})
        self.assertEqual(out["ask"], [{"owner_name": "Dana Lee",
                                       "matched": ["bayview"],
                                       "route": "ask"}])

    def test_assistant_never_fans_out(self):
        from app.services import remote_mcp
        with mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": "sealed"}), \
                mock.patch("app.services.peer_channel.ask") as ask:
            out = remote_mcp.call_tool({**self.GRANT, "scopes": {"team"}},
                                       "team_search", {"query": "bayview"})
        self.assertEqual(out["mode"], "federated")
        ask.assert_not_called()


if __name__ == "__main__":
    unittest.main()
