"""Person details — phone/email/role/org/location mined from a person's
facts, with user edits winning as overrides. Miner (pure) first, then the
storage attr table, then the endpoints via TestClient."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import person_details, self_profile
from app.storage import Store

NOW = 1_000_000_000.0


def _fact(fid, text, review=None, updated_at=NOW):
    return {"fact_id": fid, "kind": "claim", "text": text,
            "review": review, "updated_at": updated_at}


class MinerTests(unittest.TestCase):
    def test_phone_email_location(self):
        facts = [
            _fact(1, "Marc's phone number is 610-555-0143"),
            _fact(2, "Marc's email address is marc@foundry.io"),
            _fact(3, "Marc is based in Philadelphia."),
        ]
        d = person_details.mine("Marc Sullivan", [], facts)
        self.assertEqual(d["phone"]["value"], "610-555-0143")
        self.assertEqual(d["phone"]["fact_id"], 1)
        self.assertEqual(d["email"]["value"], "marc@foundry.io")
        self.assertEqual(d["location"]["value"], "Philadelphia")

    def test_team_from_text(self):
        d = person_details.mine(
            "Justin Adorante", [],
            [_fact(1, "Justin is on the Platform team at Dell")])
        self.assertEqual(d["team"]["value"], "Platform")

    def test_detail_lists_multi_phone_and_org(self):
        merged = {"phone": {"value": "111", "source": "you"}}
        attrs = {"phone": {"value": "111", "fact_id": 1}}
        cps = [
            {"contact_point_id": 9, "type": "phone",
             "value_display": "222-222-2222", "value_normalized": "2222222222",
             "created_by": "user", "confidence": 1.0},
            {"contact_point_id": 10, "type": "email",
             "value_display": "a@b.com", "value_normalized": "a@b.com",
             "created_by": "pipeline", "confidence": 0.7,
             "evidence_quote": "emailed"},
        ]
        aff = [
            {"id": 3, "name": "Dell", "predicate": "works_at"},
            {"id": 4, "name": "Platform", "predicate": "member_of"},
            {"id": 5, "name": "Villanova", "predicate": "works_at"},
        ]
        lists = person_details.detail_lists(
            merged=merged, attrs=attrs, contact_points=cps, affiliations=aff)
        phones = [r["value"] for r in lists["phone"]]
        self.assertIn("111", phones)
        self.assertIn("222-222-2222", phones)
        orgs = [r["value"] for r in lists["org"]]
        self.assertEqual(set(orgs), {"Dell", "Villanova"})
        teams = [r["value"] for r in lists["team"]]
        self.assertEqual(teams, ["Platform"])
        self.assertTrue(any(r["ref"].startswith("cp:") for r in lists["email"]))

    def test_role_and_org_from_text(self):
        d = person_details.mine("Alice Chen", [],
                                [_fact(1, "Alice is the CTO at Foundry")])
        self.assertEqual(d["role"]["value"], "CTO")
        self.assertEqual(d["org"]["value"], "Foundry")

    def test_org_from_graph_edge_beats_regex(self):
        d = person_details.mine(
            "Alice Chen", [], [_fact(1, "Alice works at Foundry")],
            affiliations=[{"name": "Acme Corp", "predicate": "works_at"}])
        self.assertEqual(d["org"]["value"], "Acme Corp")

    def test_money_is_not_a_phone_number(self):
        d = person_details.mine(
            "Marc", [], [_fact(1, "Marc said pricing is $4,900,000 per year")])
        self.assertNotIn("phone", d)

    def test_bare_digits_need_phone_context(self):
        d = person_details.mine("Marc", [], [_fact(1, "order 6105550143 shipped")])
        self.assertNotIn("phone", d)
        d = person_details.mine("Marc", [], [_fact(1, "reach Marc at 6105550143")])
        self.assertEqual(d["phone"]["value"], "6105550143")

    def test_human_reviewed_beats_newer_unreviewed(self):
        facts = [
            _fact(1, "Marc's phone number is 111-111-1111",
                  review="approved", updated_at=NOW - 100),
            _fact(2, "Marc's phone number is 222-222-2222", updated_at=NOW),
        ]
        d = person_details.mine("Marc", [], facts)
        self.assertEqual(d["phone"]["value"], "111-111-1111")

    def test_name_anchored_fact_beats_co_mention(self):
        facts = [
            _fact(1, "Justin is based in Boston", updated_at=NOW),
            _fact(2, "Marc is based in Philadelphia.", updated_at=NOW - 100),
        ]
        d = person_details.mine("Marc Sullivan", [], facts)
        self.assertEqual(d["location"]["value"], "Philadelphia")

    def test_co_mention_email_does_not_steal(self):
        # "Justin will email marc@…" must not put Marc's address on Justin.
        facts = [
            _fact(1, "Justin will email marc@foundry.io about the deck"),
            _fact(2, "Marc's email address is marc@foundry.io"),
        ]
        d_j = person_details.mine("Justin Adorante", [], facts)
        self.assertNotIn("email", d_j)
        d_m = person_details.mine("Marc Sullivan", ["Marc"], facts)
        self.assertEqual(d_m["email"]["value"], "marc@foundry.io")

    def test_claim_text_round_trips_through_miner(self):
        # The claims written on edit must be re-minable (self-healing store).
        for key, value in [("phone", "610-555-0143"), ("email", "a@b.co"),
                           ("role", "CTO"), ("org", "Foundry"),
                           ("location", "Philadelphia")]:
            text = person_details.claim_text(key, "Marc", value)
            d = person_details.mine("Marc", [], [_fact(9, text)])
            self.assertEqual(d[key]["value"], value, msg=key)

    def test_merge_override_wins(self):
        mined = {"phone": {"value": "111", "fact_id": 1, "quote": "x"}}
        attrs = {"phone": {"value": "222", "fact_id": 5}}
        out = person_details.merge(mined, attrs)
        self.assertEqual(out["phone"]["value"], "222")
        self.assertEqual(out["phone"]["source"], "you")
        out = person_details.merge(mined, {})
        self.assertEqual(out["phone"]["source"], "memory")


class StoreAttrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_set_get_clear_and_prev_fact(self):
        pid = self.store.resolve_person("Marc", ts=NOW)
        self.assertIsNone(self.store.set_person_attr(pid, "phone", "111", 7, NOW))
        prev = self.store.set_person_attr(pid, "phone", "222", 9, NOW + 1)
        self.assertEqual(prev, 7)   # caller supersedes the old claim
        attrs = self.store.person_attrs(pid)
        self.assertEqual(attrs["phone"]["value"], "222")
        self.assertEqual(self.store.clear_person_attr(pid, "phone"), 9)
        self.assertEqual(self.store.person_attrs(pid), {})
        self.assertIsNone(self.store.clear_person_attr(pid, "phone"))

    def test_delete_person_removes_attrs(self):
        pid = self.store.resolve_person("Marc", ts=NOW)
        self.store.set_person_attr(pid, "phone", "111", None, NOW)
        self.store.delete_person(pid)
        self.assertEqual(self.store.person_attrs(pid), {})


class DetailEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")
        import app.api.routes as routes_mod
        patcher = patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        idx = patch("app.api.routes.memory.index_fact", lambda *a, **k: None)
        idx.start()
        self.addCleanup(idx.stop)

    def _person_with_fact(self, name="Marc", text=None):
        pid = self.store.resolve_person(name, ts=NOW)
        if text:
            fid = self.store.add_claim(text, extracted_at=NOW)
            self.store.add_relation("person", pid, "mentioned_in", "fact",
                                    fid, ts=NOW)
        return pid

    def test_get_mines_details_from_facts(self):
        pid = self._person_with_fact(text="Marc's phone number is 610-555-0143")
        d = self.client.get(f"/people/{pid}").json()["details"]
        self.assertEqual(d["phone"]["value"], "610-555-0143")
        self.assertEqual(d["phone"]["source"], "memory")
        self.assertTrue(d["phone"]["fact_id"])

    def test_edit_overrides_writes_claim_and_supersedes_on_reedit(self):
        pid = self._person_with_fact(text="Marc's phone number is 610-555-0143")
        r = self.client.post(f"/people/{pid}/detail",
                             json={"key": "phone", "value": "484-555-0100"}).json()
        self.assertTrue(r["ok"])
        first_claim = r["fact_id"]
        fact = self.store.get_fact(first_claim)
        self.assertEqual(fact["review"], "approved")     # user's word = gold
        self.assertIn("484-555-0100", fact["text"])
        d = self.client.get(f"/people/{pid}").json()["details"]
        self.assertEqual(d["phone"]["value"], "484-555-0100")
        self.assertEqual(d["phone"]["source"], "you")
        # Re-edit: the previous edit's claim is superseded, not duplicated.
        r2 = self.client.post(f"/people/{pid}/detail",
                              json={"key": "phone", "value": "484-555-0999"}).json()
        old = self.store.get_fact(first_claim)
        self.assertEqual(old["state"], "superseded")
        self.assertEqual(old["superseded_by"], r2["fact_id"])

    def test_clear_falls_back_to_memory(self):
        pid = self._person_with_fact(text="Marc's phone number is 610-555-0143")
        r = self.client.post(f"/people/{pid}/detail",
                             json={"key": "phone", "value": "484-555-0100"}).json()
        self.client.post(f"/people/{pid}/detail",
                         json={"key": "phone", "value": ""})
        d = self.client.get(f"/people/{pid}").json()["details"]
        self.assertEqual(d["phone"]["value"], "610-555-0143")   # mined again
        self.assertEqual(d["phone"]["source"], "memory")
        gone = self.store.get_fact(r["fact_id"])
        self.assertEqual(gone["state"], "archived")   # edit's claim retired

    def test_bad_key_400_and_unknown_person_404(self):
        pid = self._person_with_fact()
        r = self.client.post(f"/people/{pid}/detail",
                             json={"key": "shoe_size", "value": "11"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/people/99999/detail",
                             json={"key": "phone", "value": "1"})
        self.assertEqual(r.status_code, 404)

    def test_add_second_phone_and_team(self):
        pid = self._person_with_fact()
        self.client.post(f"/people/{pid}/detail",
                         json={"key": "phone", "value": "610-555-0001", "op": "add"})
        self.client.post(f"/people/{pid}/detail",
                         json={"key": "phone", "value": "610-555-0002", "op": "add"})
        self.client.post(f"/people/{pid}/detail",
                         json={"key": "team", "value": "Platform", "op": "add"})
        self.client.post(f"/people/{pid}/detail",
                         json={"key": "org", "value": "Dell", "op": "add"})
        body = self.client.get(f"/people/{pid}").json()
        phones = [r["value"] for r in body["detail_lists"]["phone"]]
        self.assertTrue(any("0001" in p for p in phones))
        self.assertTrue(any("0002" in p for p in phones))
        self.assertEqual(len(self.store.list_contact_points(pid, type_="phone")), 2)
        teams = [r["value"] for r in body["detail_lists"]["team"]]
        self.assertIn("Platform", teams)
        orgs = [r["value"] for r in body["detail_lists"]["org"]]
        self.assertIn("Dell", orgs)
        # Team shows as member_of in affiliations
        preds = {a["predicate"] for a in body["affiliations"]}
        self.assertIn("member_of", preds)
        self.assertIn("works_at", preds)


if __name__ == "__main__":
    unittest.main()
