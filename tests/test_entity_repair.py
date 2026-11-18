"""Entity model, Phase 4: repair the rows a store already has.

`entity_repair.plan` is the diff over every visible row (keep, re-kind,
re-parent, merge, hide, drop, each with its rule, reason and evidence
counts); `apply` makes exactly that plan and refuses one made for another
database or gone stale; `auto_run` is the automatic path (high-confidence,
reversible changes only, after a backup, once per rules version, the rest
in the Inbox); `undo` puts a run or a change back unless the user changed
the field since. `scripts/entity_repair.py` drives the reviewed path.

The worlds the rules are judged on are invented here: no name comes from
any seat's data. Every test runs against a real Store on a temp db.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Imported here, before any test patches `app.storage.get_store`.
import app.api.routes as routes_mod
from app import storage as storage_mod
from app.events import Event, Modality
from app.services import entity_repair as er
from app.services import inbox
from app.services import projects
from app.storage import Store

NOW = 1_700_000_000.0
DAY = 86400.0
ROOT = Path(__file__).resolve().parents[1]
NOTE = "assistant.claude"       # an accepted assistant note
SCREEN = "desktop.screen"
MAIL = "google.mail"
CHAT = "chat.user"


def _mk(tmp, **kw) -> Store:
    return Store(db_path=Path(tmp) / "quill.db",
                 audio_dir=Path(tmp) / "audio", **kw)


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_repair_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        env = patch.dict(os.environ, {"QUILL_DATA_DIR": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        for var in ("QUILL_ENTITY_REPAIR_AUTO",
                    "QUILL_ENTITY_REPAIR_AUTO_MIN_CONF",
                    "QUILL_ENTITY_REPAIR_NOTE_ONLY", "QUILL_ENTITY_SCOPE",
                    "QUILL_ENTITY_SCOPE_MIN_EVENTS",
                    "QUILL_ENTITY_SCOPE_MIN_EVENTS_LABEL"):
            os.environ.pop(var, None)
        for p in (patch.object(projects, "_scopes", lambda: []),
                  patch("app.services.identity.user_identity",
                        return_value={}),
                  patch.object(storage_mod, "_store", None)):
            p.start()
            self.addCleanup(p.stop)
        projects.invalidate_filed_keys()
        self.addCleanup(projects.invalidate_filed_keys)
        self._t = NOW

    # -- the world -----------------------------------------------------
    def ent(self, name, kind, source="model", ts=NOW) -> int:
        return self.store.resolve_entity(name, kind, ts=ts,
                                         kind_source=source)

    def capture(self, name):
        """The filing registry's `filed` tier: an accepted note under it."""
        from app.services.remote_mcp import connect
        with connect() as c:
            c.execute("INSERT INTO captures (created_at, project, kind, text, "
                      "status, decided_at) VALUES (?, ?, 'note', 'x', "
                      "'accepted', ?)", (NOW, name, NOW))
            c.commit()
        projects.invalidate_filed_keys()

    def filed(self, name) -> int:
        self.capture(name)
        return projects.ensure_filed(name, store=self.store, now=NOW,
                                     user_accept=True)

    def event(self, source=MAIL, project=None, gap=600.0) -> int:
        self._t += gap
        meta = {"project": project} if project else {}
        return int(self.store.insert(Event(
            time=self._t, modality=Modality.TEXT, raw="r", source=source,
            meta=meta)))

    def fact(self, eid, *, source=MAIL, project=None, review=None,
             origin="derived", gap=600.0) -> int:
        ev = self.event(source, project, gap=gap)
        with self.store._lock:
            cur = self.store._conn.execute(
                "INSERT INTO facts (kind, text, source_event_id, "
                "extracted_at, review) VALUES ('claim', 'x', ?, ?, ?)",
                (ev, self._t, review))
            fid = int(cur.lastrowid)
            self.store._conn.commit()
        self.store.add_relation("fact", fid, "about", "entity", eid,
                                origin=origin, ts=self._t)
        return ev

    def minted_by(self, eid, source=NOTE, project=None) -> int:
        """The extraction of a new event minted the row (first_seen ==
        extracted_at)."""
        ev = self.event(source, project)
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE events SET extracted_at = ? WHERE id = ?",
                (self._t + 0.5, ev))
            self.store._conn.execute(
                "UPDATE entities SET first_seen = ? WHERE id = ?",
                (self._t + 0.5, eid))
            self.store._conn.commit()
        return ev

    def person(self, name) -> int:
        return self.store.resolve_person(name, ts=NOW)

    def email(self, person, address):
        pid = self.store.find_person_exact(person) or self.person(person)
        self.store.upsert_contact_point(
            person_id=pid, type_="email", value_display=address,
            value_normalized=address, confidence=0.9,
            attribution_method="email_header",
            verification_status="attributed", source_event_id=None,
            evidence_quote=None, discourse_role=None, ts=NOW,
            created_by="system", pipeline_version="t")

    def self_name(self, name):
        fid = self.store.add_claim(f"The user's name is {name}.",
                                   confidence=1.0, extracted_at=NOW)
        self.store.review_fact(fid, "approved")

    def plan(self, **kw) -> dict:
        return er.plan(self.store, **kw)

    def acts(self, p, eid) -> list[dict]:
        return [a for a in p["actions"] if a["entity_id"] == eid]

    def one(self, p, eid, action=None) -> dict:
        rows = [a for a in self.acts(p, eid)
                if action is None or a["action"] == action]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def row(self, eid) -> dict | None:
        e = self.store.get_entity(eid)
        if e is not None:
            e["hidden"] = self.store.entity_is_hidden(eid)
        return e


# ----------------------------------------------------------- the rules --
class PlanRulesTests(_Case):
    def test_every_visible_row_is_accounted_for_and_hidden_rows_are_not(self):
        a = self.ent("Vesper Logistics", "org")
        self.fact(a)
        b = self.ent("Wave 4", "project")
        h = self.ent("Friday", "thing", "rule")
        self.store.set_entity_hidden(h, hidden=True)
        p = self.plan()
        self.assertEqual({x["entity_id"] for x in p["actions"]}, {a, b})
        self.assertEqual(self.one(p, a)["action"], "keep")
        self.assertEqual(p["db"]["visible"], 2)
        self.assertEqual(p["rules_version"], er.RULES_VERSION)
        # A plan reads only.
        n = self.store._conn.total_changes
        self.plan()
        self.assertEqual(self.store._conn.total_changes, n)

    def test_a_kind_the_user_set_is_kept_whatever_it_looks_like(self):
        a = self.ent("c4", "project", "user")
        b = self.ent("$49", "idea", "user")
        p = self.plan()
        for eid in (a, b):
            x = self.one(p, eid)
            self.assertEqual((x["action"], x["rule"]), ("keep", "user"))

    def test_a_row_the_user_touched_keeps_its_kind_but_takes_its_filing(self):
        t = self.ent("Marlow Deck", "tool", None)
        self.fact(t, review="approved", origin="asserted")   # the user's note
        self.capture("Marlow Deck")
        u = self.ent("Quarry Plan", "project", None)
        self.store.set_entity_attr(u, "status", "active", None, NOW)
        p = self.plan()
        x = self.one(p, t)
        self.assertEqual((x["action"], x["to_kind"], x["route"]),
                         ("rekind", "project", "review"))
        self.assertIn("note", x["evidence"]["user_touched"])
        y = self.one(p, u)
        self.assertEqual((y["action"], y["rule"]), ("keep", "user_touched"))

    def test_a_value_is_hidden_and_becomes_a_thing(self):
        v = self.ent("$412,000", "idea", None)
        p = self.plan()
        self.assertEqual({a["action"] for a in self.acts(p, v)},
                         {"hide", "rekind"})
        self.assertEqual(self.one(p, v, "rekind")["to_kind"], "thing")
        self.assertTrue(all(a["route"] == "auto" for a in self.acts(p, v)))

    def test_an_implausible_name_is_dropped_and_tombstoned(self):
        a = self.ent("$4,200 per month", "idea", None)
        b = self.ent("Brightwater.io", "org", None)      # named by its domain
        c = self.ent("notes.md", "idea", None)
        d = self.ent("Unknown", "org", None)              # a placeholder
        self.fact(d)
        p = self.plan()
        self.assertEqual((self.one(p, d)["rule"], self.one(p, d)["tombstone"]),
                         ("implausible", True))
        x = self.one(p, a)
        self.assertEqual((x["action"], x["rule"], x["tombstone"]),
                         ("drop", "implausible", True))
        self.assertEqual(self.one(p, c)["rule"], "implausible")
        self.assertEqual(self.one(p, b)["action"], "keep")

    def test_merge_survivor_order_filed_user_plain_facts_oldest(self):
        f = self.filed("Kestrel Rollout")
        d = self.ent("The Kestrel Rollout project", "idea")
        self.fact(d)
        self.fact(d)
        u = self.ent("Orrin Labs", "org", "user")
        o = self.ent("Orrin-Labs", "org")
        for _ in range(3):
            self.fact(o)
        x1 = self.ent("Tallyway", "tool", ts=NOW - 5 * DAY)
        x2 = self.ent("Tally Way", "tool", ts=NOW)
        p = self.plan()
        m = self.one(p, d, "merge")
        self.assertEqual((m["into"], m["rule"]), (f, "merge_dressed_name"))
        self.assertEqual(self.one(p, o, "merge")["into"], u)
        self.assertEqual(self.one(p, x2, "merge")["into"], x1)
        for eid in (f, u, x1):
            self.assertFalse([a for a in self.acts(p, eid)
                              if a["action"] == "merge"])
        merges = [a for a in p["actions"] if a["action"] == "merge"]
        self.assertTrue(merges)
        self.assertTrue(all(a["route"] == "review" for a in merges))
        self.assertTrue(all(a["confidence"] >= er.auto_min_conf()
                            for a in merges if a["rule"] != "merge_org_short_form"))

    def test_a_user_row_is_never_absorbed(self):
        a = self.ent("Pellam Group", "org", "user")
        b = self.ent("Pellam  Group.", "org", "user")
        p = self.plan()
        self.assertFalse([x for x in p["actions"] if x["action"] == "merge"])
        self.assertEqual({self.one(p, a)["rule"], self.one(p, b)["rule"]},
                         {"user"})

    def test_the_users_own_possessive_becomes_an_alias_of_the_row(self):
        self.self_name("Sam Okafor")
        plain = self.ent("Lantern", "project", None)
        self.store.set_entity_hidden(plain, hidden=True)
        poss = self.ent("Sam Okafor's Lantern", "project", None)
        for _ in range(3):
            self.fact(poss)
        given = self.ent("Sam's Lantern", "idea")
        other = self.ent("Dana's Lantern", "idea")       # not the user
        p = self.plan()
        self.assertTrue(p["self_known"])
        m = self.one(p, poss, "merge")
        self.assertEqual((m["into"], m["rule"], m["survivor_hidden"]),
                         (plain, "merge_self_possessive", True))
        self.assertEqual(self.one(p, given, "merge")["into"], plain)
        self.assertFalse([a for a in self.acts(p, other)
                          if a["action"] == "merge"])
        er.apply(self.store, p)
        self.assertEqual(self.store.resolve_entity(
            "Sam Okafor's Lantern", "project", ts=NOW), plain)
        self.assertTrue(self.store.entity_is_hidden(plain))

    def test_a_short_form_used_only_inside_a_filed_project_merges_into_it(self):
        f = self.filed("Atlas Rollout")
        short = self.ent("Atlas", "idea")
        self.fact(short, source=NOTE, project="Atlas Rollout")
        self.fact(short, source=MAIL, project="Atlas Rollout")
        outside = self.ent("Rollout", "idea")
        self.fact(outside, source=MAIL)
        p = self.plan()
        m = self.one(p, short, "merge")
        self.assertEqual((m["into"], m["rule"]),
                         (f, "merge_names_filed_project"))
        self.assertFalse([a for a in self.acts(p, outside)
                          if a["action"] == "merge"])

    def test_the_short_form_of_an_org_row_merges_into_it(self):
        org = self.ent("Ridgeline Rate Advisors", "org", None)
        short = self.ent("Ridgeline", "project", None)
        work = self.ent("Ridgeline Rate", "idea", None)   # a piece of work
        self.ent("Vendor Review working group", "org", None)
        review = self.ent("Vendor Review", "project", None)
        two = self.ent("Marrow", "idea", None)          # two orgs: unsure
        self.ent("Marrow Labs", "org", None)
        self.ent("Marrow Freight", "org", None)
        tool = self.ent("Ridgeline Rate Advisors app", "tool", None)
        p = self.plan()
        m = self.one(p, short, "merge")
        self.assertEqual((m["into"], m["rule"], m["route"]),
                         (org, "merge_org_short_form", "review"))
        for eid in (work, review):
            self.assertFalse([a for a in self.acts(p, eid)
                              if a["action"] == "merge"])
        self.assertFalse([a for a in self.acts(p, two)
                          if a["action"] == "merge"])
        self.assertFalse([a for a in self.acts(p, tool)
                          if a["action"] == "merge"])

    def test_a_filed_name_is_a_project_and_a_filed_company_an_org(self):
        t = self.ent("Harbor Line", "tool", None)
        self.capture("Harbor Line")
        c = self.ent("Merrow & Tate LLP", "org", None)
        self.capture("Merrow & Tate LLP")
        p = self.plan()
        x = self.one(p, t)
        self.assertEqual((x["to_kind"], x["kind_source"], x["rule"],
                          x["route"]),
                         ("project", "filing", "filed_project", "auto"))
        y = self.one(p, c)
        self.assertEqual((y["to_kind"], y["kind_source"], y["rule"]),
                         ("org", "filing", "filed_org"))

    def test_org_evidence_makes_an_org(self):
        a = self.ent("Fernbrook Print Co.", "project", None)
        self.fact(a)
        self.fact(a)
        self.email("Ana Brightfield", "ana@harborlight.example")
        b = self.ent("Harborlight", "idea", None)
        p = self.plan()
        self.assertEqual((self.one(p, a)["rule"], self.one(p, a)["to_kind"]),
                         ("legal_form", "org"))
        self.assertEqual((self.one(p, b)["rule"], self.one(p, b)["to_kind"]),
                         ("email_domain", "org"))
        # On a legacy row the email domain is a prior the kind vote applies
        # too (automatic); a legal form that is not filed is not (asked).
        self.assertEqual((self.one(p, a)["route"], self.one(p, b)["route"]),
                         ("review", "auto"))
        c = self.ent("Quillon Paper Co.", "project", "model")
        self.fact(c)
        self.assertEqual(self.one(self.plan(), c)["route"], "auto")

    def test_people_who_work_there_make_an_org_but_it_is_asked(self):
        a = self.ent("Calder Works", "idea", None)
        b = self.ent("Dock Survey", "project", "model")
        c = self.ent("support material", "idea", None)     # a phrase
        d = self.ent("Peak capacity planning", "idea", None)
        pid = self.person("Ilse Varga")
        for eid in (a, b, c, d):
            self.store.add_relation("person", pid, "works_at", "entity", eid,
                                    origin="asserted", source_event_id=None)
            self.fact(eid)
            self.fact(eid)
        p = self.plan()
        x = self.one(p, a)
        self.assertEqual((x["rule"], x["to_kind"], x["route"]),
                         ("people_work_there", "org", "review"))
        # People "work at" the work they do: a model project stays one of
        # the kind rules' business.
        self.assertEqual(self.one(p, b)["rule"], "project_unfiled")
        for eid in (c, d):
            self.assertNotEqual(self.one(p, eid)["rule"], "people_work_there")


class NoteOnlyTests(_Case):
    def test_a_row_only_notes_or_screen_text_named_is_dropped_untombstoned(self):
        a = self.ent("Ravel Pilot", "project", None)
        self.fact(a, source=NOTE, project="Harbor Line")
        b = self.ent("Quayside", "org", None)
        self.minted_by(b, source=SCREEN)
        c = self.ent("Mixed Bag", "idea", None)
        self.fact(c, source=NOTE)
        self.fact(c, source=MAIL)
        p = self.plan()
        for eid in (a, b):
            x = self.one(p, eid)
            self.assertEqual((x["action"], x["rule"], x["tombstone"],
                              x["route"]),
                             ("drop", "note_only", False, "auto"))
            self.assertEqual(x["evidence"]["eligible_events"], 0)
        self.assertNotEqual(self.one(p, c)["rule"], "note_only")
        # Applied: deleted, no tombstone, so a real sighting can mint it.
        er.apply(self.store, p)
        self.assertIsNone(self.store.get_entity(a))
        self.assertFalse(self.store.is_entity_tombstoned("Ravel Pilot"))

    def test_people_and_email_domains_vouch_for_a_row(self):
        a = self.ent("Ostrava Mills", "org", None)
        self.minted_by(a, source=SCREEN)
        self.store.add_relation("person", self.person("Lena Hart"),
                                "member_of", "entity", a, origin="asserted")
        self.email("Ana Brightfield", "ana@quaymark.example")
        b = self.ent("Quaymark", "org", None)
        self.minted_by(b, source=NOTE)
        p = self.plan()
        self.assertEqual(self.one(p, a)["action"], "keep")
        self.assertEqual(self.one(p, b)["action"], "keep")

    def test_the_label_option_keeps_them_under_their_filing_project(self):
        f = self.filed("Harbor Line")
        a = self.ent("Ravel Pilot", "project", None)
        self.fact(a, source=NOTE, project="Harbor Line")
        c = self.ent("c7", "project", None)
        self.minted_by(c, source=NOTE, project="Harbor Line")
        t = self.ent("Quillmate", "tool", None)
        self.fact(t, source=NOTE, project="Harbor Line")
        p = self.plan(note_only="label")
        self.assertEqual(p["options"]["note_only"], "label")
        for eid in (a, c):
            kinds = {x["action"]: x for x in self.acts(p, eid)}
            self.assertEqual(kinds["rekind"]["to_kind"], "label")
            self.assertEqual(kinds["reparent"]["parent_id"], f)
        self.assertEqual(self.one(p, t)["rule"], "note_only_kept")
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_NOTE_ONLY": "label"}):
            self.assertEqual(self.plan()["options"]["note_only"], "label")


class ShapeRuleTests(_Case):
    def test_a_lowercase_description_seen_once_is_dropped_and_tombstoned(self):
        a = self.ent("pet groomers", "project", None)
        self.fact(a)
        b = self.ent("bowling alleys", "idea", None)
        self.fact(b)
        self.fact(b)
        c = self.ent("dataloom", "tool", None)        # a tool's own name
        p = self.plan()
        x = self.one(p, a)
        self.assertEqual((x["action"], x["rule"], x["tombstone"]),
                         ("drop", "common_noun", True))
        self.assertEqual(self.one(p, b)["action"], "keep")
        self.assertEqual(self.one(p, c)["action"], "keep")

    def test_a_code_shaped_name_is_a_label(self):
        a = self.ent("Sprint 9", "project", None)
        self.fact(a)
        b = self.ent("9bc12da", "idea", None)
        o = self.ent("QX9", "org", None)
        self.fact(o)
        p = self.plan()
        for eid in (a, b):
            x = self.one(p, eid)
            self.assertEqual((x["to_kind"], x["rule"], x["route"]),
                             ("label", "code_like", "auto"))
        y = self.one(p, o)
        self.assertEqual((y["to_kind"], y["rule"], y["route"]),
                         ("label", "code_like_named", "review"))

    def test_a_code_shaped_org_two_sightings_agree_on_stays_an_org(self):
        o = self.ent("QX9", "org", "model")
        for i, src in enumerate((MAIL, CHAT)):
            ev = self.event(src, gap=2 * DAY)
            self.store.record_entity_mention(
                "QX9", proposed_kind="org", confidence=0.9,
                source_class="connector_mail" if i == 0 else "direct_message",
                event_source=src, event_id=ev, observed_at=self._t,
                entity_id=o)
        self.assertEqual(self.one(self.plan(), o)["action"], "keep")

    def test_a_model_project_no_filing_made_is_a_label_or_a_topic(self):
        f = self.filed("Harbor Line")
        a = self.ent("Dock Survey", "project", "model")
        self.fact(a, project="Harbor Line")
        b = self.ent("Ocean Freight", "project", "model")
        self.fact(b)
        self.fact(b)
        c = self.ent("Coastal Review", "project", None)      # legacy
        self.fact(c)
        self.fact(c)
        p = self.plan()
        xa = {x["action"]: x for x in self.acts(p, a)}
        self.assertEqual((xa["rekind"]["to_kind"], xa["rekind"]["rule"]),
                         ("label", "project_scoped"))
        self.assertEqual(xa["reparent"]["parent_id"], f)
        xb = self.one(p, b)
        self.assertEqual((xb["to_kind"], xb["rule"], xb["route"]),
                         ("idea", "project_unfiled", "auto"))
        # A legacy row (its kind may be the user's own) is never demoted
        # for lacking a filing: nothing says it is not a project.
        xc = self.one(p, c)
        self.assertEqual((xc["action"], xc["rule"]), ("keep", "project_legacy"))

    def test_a_legacy_project_changes_only_on_positive_evidence(self):
        f = self.filed("Harbor Line")
        once = self.ent("Dock Survey", "project", None)      # one event in P
        self.fact(once, project="Harbor Line")
        twice = self.ent("Pier Survey", "project", None)     # two in P
        self.fact(twice, project="Harbor Line")
        self.fact(twice, project="Harbor Line")
        phrase = self.ent("coastal survey", "project", None)  # a description
        self.fact(phrase)
        self.fact(phrase)
        proper = self.ent("Coastal Review", "project", None)
        self.fact(proper)
        self.fact(proper, project="Harbor Line")
        p = self.plan()
        self.assertEqual(self.one(p, once)["rule"], "project_legacy")
        x = {a["action"]: a for a in self.acts(p, twice)}
        self.assertEqual((x["rekind"]["to_kind"], x["rekind"]["rule"],
                          x["rekind"]["route"]),
                         ("label", "project_scoped", "review"))
        self.assertEqual((x["reparent"]["parent_id"], x["reparent"]["route"]),
                         (f, "review"))
        y = self.one(p, phrase)
        self.assertEqual((y["to_kind"], y["rule"], y["route"]),
                         ("idea", "project_description", "review"))
        self.assertEqual(self.one(p, proper)["rule"], "project_legacy")
        # Applied, and nothing left to ask.
        er.apply(self.store, p)
        self.assertEqual([a for a in self.plan()["actions"]
                          if a["action"] != "keep"], [])

    def test_the_event_that_minted_a_row_is_evidence(self):
        f = self.filed("Harbor Line")
        a = self.ent("Wave 4", "label", "model")
        self.minted_by(a, source=MAIL, project="Harbor Line")
        p = self.plan()
        x = self.one(p, a, "reparent")
        self.assertEqual((x["parent_id"], x["rule"]), (f, "scope"))
        self.assertEqual(x["evidence"]["created"], 1)
        # The graph job's scope pass reads the same evidence.
        from app.services import entity_scope
        row = next(r for r in entity_scope.compute(self.store)
                   if r["entity_id"] == a)
        self.assertEqual((row["action"], row["parent_id"]), ("set", f))


class CreationBatchTests(_Case):
    """An extraction pass stamps every event it read with one clock value:
    the row was minted from one of them, not from all."""

    def batch(self, eid, sources_projects):
        """The row's first_seen equals the extracted_at of every event of a
        batch: [(source, project), ...]."""
        evs = [self.event(src, proj) for src, proj in sources_projects]
        stamp = self._t + 0.25
        with self.store._lock:
            for ev in evs:
                self.store._conn.execute(
                    "UPDATE events SET extracted_at = ? WHERE id = ?",
                    (stamp, ev))
            self.store._conn.execute(
                "UPDATE entities SET first_seen = ? WHERE id = ?",
                (stamp, eid))
            self.store._conn.commit()
        return evs

    def created(self, eid):
        return [r for r in self.store.creation_events()
                if r["entity_id"] == eid]

    def test_a_batch_is_one_sighting_or_none(self):
        same = self.ent("Tern Board", "idea", None)
        evs = self.batch(same, [(MAIL, "Harbor Line")] * 3)
        mixed = self.ent("Kite Board", "idea", None)
        self.batch(mixed, [(MAIL, None), (NOTE, None), (MAIL, "Harbor Line")])
        one = self.ent("Gull Board", "idea", None)
        self.minted_by(one, source=MAIL)
        r = self.created(same)
        self.assertEqual([(x["event_id"], x["batch"], x["mixed"])
                          for x in r], [(evs[0], 3, False)])
        r = self.created(mixed)
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0]["mixed"])
        self.assertIsNone(r[0]["event_source"])
        self.assertEqual(self.created(one)[0]["batch"], 1)

    def test_a_batch_does_not_inflate_the_evidence(self):
        f = self.filed("Harbor Line")
        # Three events of one batch, all filed under P, and nothing else: one
        # sighting, under a topic's bar of two, so no parent.
        topic = self.ent("Tern Board", "idea", None)
        self.batch(topic, [(MAIL, "Harbor Line")] * 3)
        # A lowercase description minted in a batch of four: one event.
        noun = self.ent("tern boards", "idea", None)
        self.batch(noun, [(MAIL, None)] * 4)
        p = self.plan()
        self.assertEqual(self.one(p, topic)["action"], "keep")
        self.assertEqual(self.one(p, topic)["evidence"]["events"], 1)
        x = self.one(p, noun)
        self.assertEqual((x["action"], x["rule"]), ("drop", "common_noun"))
        from app.services import entity_scope
        self.assertFalse([r for r in entity_scope.compute(self.store)
                          if r["entity_id"] == topic
                          and r.get("parent_id") == f])

    def test_a_mixed_batch_places_the_name_in_no_project(self):
        f = self.filed("Harbor Line")
        topic = self.ent("Tern Board", "idea", None)
        self.fact(topic, project="Harbor Line")
        self.fact(topic, project="Harbor Line")
        self.batch(topic, [(MAIL, None), (NOTE, "Harbor Line")])
        from app.services import entity_scope
        row = next(r for r in entity_scope.compute(self.store)
                   if r["entity_id"] == topic)
        self.assertEqual((row["action"], row["parent_id"]), ("set", f))
        x = self.one(self.plan(), topic, "reparent")
        self.assertEqual(x["parent_id"], f)

    def test_a_mixed_batch_is_a_sighting_that_is_not_a_note(self):
        a = self.ent("Ravel Pilot", "project", None)
        self.fact(a, source=NOTE)
        self.batch(a, [(MAIL, None), (NOTE, None)])
        p = self.plan()
        self.assertNotEqual(self.one(p, a)["rule"], "note_only")
        b = self.ent("Ravel Board", "project", None)
        self.fact(b, source=NOTE)
        self.batch(b, [(NOTE, None), (NOTE, None)])     # all notes: one
        self.assertEqual(self.one(self.plan(), b)["rule"], "note_only")


class PersonNameTests(_Case):
    def test_a_persons_name_stored_as_a_topic_is_dropped(self):
        self.person("Omar Haddad")
        self.person("Mona Reyes")
        self.person("Mona Lind")
        full = self.ent("Omar Haddad", "idea", None)
        given = self.ent("Omar", "idea", None)
        shared = self.ent("Mona", "idea", None)          # two Monas: unsure
        org = self.ent("Haddad", "org", None)
        proj = self.ent("Mona Lind", "project", "user")   # the user's call
        # A description in the people table gives no given name, and an
        # org's short form is no person's name.
        self.person("Kestrel's CFO")
        short = self.ent("Kestrel", "idea", None)
        self.person("Brennans' CFO")
        plural = self.ent("Brennans", "idea", None)
        self.person("Brindle Okoro")
        brindle = self.ent("Brindle", "idea", None)
        self.ent("Brindle Analytics", "org", None)
        p = self.plan()
        x = self.one(p, full)
        self.assertEqual((x["action"], x["rule"], x["route"]),
                         ("drop", "person_name", "auto"))
        self.assertEqual(x["person"]["name"], "Omar Haddad")
        y = self.one(p, given)
        self.assertEqual((y["rule"], y["person"]["name"]),
                         ("person_given_name", "Omar Haddad"))
        self.assertEqual(self.one(p, shared)["action"], "keep")
        self.assertEqual(self.one(p, org)["action"], "keep")
        self.assertIn(proj, [a["entity_id"] for a in p["actions"]])
        self.assertEqual(self.one(p, proj)["rule"], "user")
        for eid in (short, plural):
            self.assertNotIn(self.one(p, eid)["rule"],
                             ("person_name", "person_given_name"))
        self.assertNotIn(self.one(p, brindle)["rule"],
                         ("person_name", "person_given_name"))

    def test_on_the_automatic_path_it_is_hidden_and_never_deleted(self):
        self.person("Omar Haddad")
        full = self.ent("Omar Haddad", "idea", None)
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            res = er.auto_run(self.store)
        self.assertTrue(res["ran"])
        self.assertTrue(self.row(full)["hidden"])
        self.assertFalse(self.store.is_entity_tombstoned("Omar Haddad"))


# --------------------------------------------------------- the routing --
class RoutingTests(_Case):
    def test_the_bar_decides_what_is_automatic(self):
        a = self.ent("Ocean Freight", "project", "model")
        self.fact(a)
        self.fact(a)
        self.assertEqual(self.one(self.plan(), a)["route"], "auto")
        with patch.dict(os.environ,
                        {"QUILL_ENTITY_REPAIR_AUTO_MIN_CONF": "0.95"}):
            self.assertEqual(self.one(self.plan(), a)["route"], "review")

    def test_evidence_rules_on_a_merge_group_wait_for_the_merge(self):
        a = self.ent("Ocean Freight", "project", "model")
        b = self.ent("Ocean-Freight", "project", "model")
        self.fact(a)
        self.fact(b)
        p = self.plan()
        self.assertEqual(self.one(p, b, "merge")["into"], a)
        self.assertEqual(self.one(p, a, "rekind")["route"], "review")

    def test_a_parent_link_waits_for_the_kind_change_it_needs(self):
        self.filed("Harbor Line")
        o = self.ent("QX9", "org", None)
        self.fact(o, project="Harbor Line")
        p = self.plan()
        self.assertEqual(self.one(p, o, "rekind")["route"], "review")
        self.assertEqual(self.one(p, o, "reparent")["route"], "review")

    def test_counts_split_automatic_and_asked(self):
        a = self.ent("Sprint 9", "project", None)
        b = self.ent("Ocean Freight", "org")
        c = self.ent("Ocean-Freight", "org")
        p = self.plan()
        self.assertEqual(p["counts"]["auto"].get("rekind"), 1)
        self.assertEqual(p["counts"]["review"].get("merge"), 1)
        self.assertEqual(p["counts"]["keep"], 1)
        self.assertEqual({a, b, c},
                         {x["entity_id"] for x in p["actions"]})


# ----------------------------------------------------------- applying --
class ApplyTests(_Case):
    def world(self):
        self.filed("Harbor Line")
        ids = {
            "value": self.ent("$49", "idea", None),
            "frag": self.ent("$4,200 per month", "idea", None),
            "dup_a": self.ent("Vesper Logistics", "org"),
            "dup_b": self.ent("Vesper-Logistics", "org"),
            "code": self.ent("Sprint 9", "project", None),
            "scoped": self.ent("Dock Survey", "project", "model"),
            "note": self.ent("Ravel Pilot", "project", None),
            "noun": self.ent("pet groomers", "idea", None),
        }
        self.fact(ids["dup_a"])
        self.fact(ids["dup_b"])
        self.fact(ids["code"], project="Harbor Line")
        self.fact(ids["scoped"], project="Harbor Line")
        self.fact(ids["note"], source=NOTE)
        self.fact(ids["noun"])
        return ids

    def test_the_reviewed_path_makes_the_whole_plan_and_a_replan_is_a_noop(self):
        ids = self.world()
        p = self.plan()
        res = er.apply(self.store, p)
        self.assertFalse(res["skipped"], res["skipped"])
        self.assertTrue(self.row(ids["value"])["hidden"])
        self.assertEqual(self.row(ids["value"])["kind"], "thing")
        self.assertIsNone(self.row(ids["frag"]))
        self.assertTrue(self.store.is_entity_tombstoned("$4,200 per month"))
        self.assertIsNone(self.row(ids["dup_b"]))
        self.assertEqual(self.store.resolve_entity("Vesper-Logistics", "org",
                                                   ts=NOW), ids["dup_a"])
        code = self.row(ids["code"])
        self.assertEqual((code["kind"], code["kind_source"]),
                         ("label", "rule"))
        self.assertIsNotNone(code["parent_id"])
        self.assertIsNone(self.row(ids["note"]))
        self.assertIsNone(self.row(ids["noun"]))
        # Integrity, and nothing left to do.
        with self.store._lock:
            self.assertEqual(self.store._conn.execute(
                "PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(self.store._conn.execute(
                "PRAGMA foreign_key_check").fetchall(), [])
        again = self.plan()
        self.assertEqual([a for a in again["actions"]
                          if a["action"] != "keep"], [])
        # The graph job after it agrees.
        from app.services import entity_scope, graph, project_rollup
        graph.rebuild(self.store)
        project_rollup.run(self.store)
        self.assertEqual(entity_scope.run(self.store)["set"], 0)
        self.assertEqual([a for a in self.plan()["actions"]
                          if a["action"] != "keep"], [])

    def test_the_log_holds_before_values_and_no_forgotten_name(self):
        ids = self.world()
        res = er.apply(self.store, self.plan())
        log = self.store.entity_repair_log(run_id=res["run_id"])
        kinds = [r for r in log if r["field"] == "kind"
                 and r["entity_id"] == ids["code"]]
        self.assertEqual(kinds[0]["before"]["kind"], "project")
        self.assertEqual(kinds[0]["after"]["kind"], "label")
        drop = [r for r in log if r["entity_id"] == ids["frag"]][0]
        self.assertNotIn("4,200", json.dumps(drop))
        self.assertEqual(drop["name"],
                         self.store.entity_tombstone_hash("$4,200 per month"))
        note = [r for r in log if r["entity_id"] == ids["note"]][0]
        self.assertEqual(note["name"], "Ravel Pilot")   # no tombstone: kept
        self.assertEqual(self.store.entity_merges()[0]["actor"], "repair")

    def test_a_plan_for_another_database_is_refused(self):
        self.world()
        p = self.plan()
        other_dir = tempfile.mkdtemp(prefix="quill_repair_other_")
        other = _mk(other_dir)
        self.addCleanup(other.close)
        for name in ("$49", "$4,200 per month", "Vesper Logistics",
                     "Vesper-Logistics", "Sprint 9", "Dock Survey",
                     "Ravel Pilot", "pet groomers", "Harbor Line"):
            other.resolve_entity(name, "idea", ts=NOW)
        n = other._conn.total_changes
        with self.assertRaises(er.PlanRefused) as cm:
            er.apply(other, p)
        self.assertIn("another database", cm.exception.reason)
        self.assertEqual(other._conn.total_changes, n)

    def test_a_stale_plan_is_refused(self):
        ids = self.world()
        p = self.plan()
        self.store.set_entity_kind(ids["code"], "org", source="user")
        n = self.store._conn.total_changes
        with self.assertRaises(er.PlanRefused) as cm:
            er.apply(self.store, p)
        self.assertIn("changed", cm.exception.reason)
        self.assertEqual(self.store._conn.total_changes, n)
        self.assertEqual(er.check(self.store, p)[0]["problem"], "stale")


class PlanFileTests(_Case):
    """What a reviewed plan must still be when it is applied."""

    def world(self):
        self.filed("Harbor Line")
        ids = {"code": self.ent("Sprint 9", "project", None),
               "phrase": self.ent("coastal survey", "project", None),
               "note": self.ent("Ravel Pilot", "project", None),
               "dup_a": self.ent("Vesper Logistics", "org"),
               "dup_b": self.ent("Vesper-Logistics", "org"),
               "keep": self.ent("Orrin Labs", "org")}
        self.fact(ids["phrase"])
        self.fact(ids["phrase"])
        self.fact(ids["note"], source=NOTE)
        self.fact(ids["keep"])
        return ids

    def refused(self, p, problem):
        n = self.store._conn.total_changes
        with self.assertRaises(er.PlanRefused) as cm:
            er.apply(self.store, p)
        self.assertEqual(self.store._conn.total_changes, n)
        self.assertIn(problem, {x["problem"] for x in cm.exception.problems})
        return cm.exception

    def test_a_hand_edited_action_is_refused(self):
        ids = self.world()
        p = self.plan()
        a = self.one(p, ids["phrase"], "rekind")
        a["to_kind"] = "org"                  # edited by hand
        exc = self.refused(p, "not_in_plan")
        self.assertIn("edited by hand", exc.reason)

    def test_an_action_added_by_hand_is_refused(self):
        ids = self.world()
        p = self.plan()
        keep = self.one(p, ids["keep"])
        p["actions"].append({**keep, "action": "drop", "rule": "implausible",
                             "tombstone": True})
        self.refused(p, "not_in_plan")
        self.assertIsNotNone(self.row(ids["keep"]))

    def test_deleting_an_action_keeps_that_row_and_applies_the_rest(self):
        ids = self.world()
        p = self.plan()
        p["actions"] = [a for a in p["actions"]
                        if a["entity_id"] != ids["dup_b"]]
        res = er.apply(self.store, p)
        self.assertFalse(res["skipped"])
        self.assertIsNotNone(self.row(ids["dup_b"]))   # the merge left out
        self.assertEqual(self.row(ids["code"])["kind"], "label")

    def test_new_evidence_since_the_plan_refuses_it(self):
        ids = self.world()
        p = self.plan()
        self.assertEqual(self.one(p, ids["note"])["rule"], "note_only")
        self.fact(ids["note"], source=MAIL)       # a real sighting since
        self.refused(p, "not_in_plan")
        self.assertIsNotNone(self.row(ids["note"]))

    def test_another_rules_version_is_refused(self):
        self.world()
        p = self.plan()
        p["rules_version"] = "entity-repair-v0"
        exc = self.refused(p, "rules_changed")
        self.assertIn("another version", exc.reason)

    def test_the_script_refuses_a_hand_edit_and_writes_nothing(self):
        ids = self.world()
        spec = importlib.util.spec_from_file_location(
            "entity_repair_script_pf", ROOT / "scripts" / "entity_repair.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        out = Path(self.tmp) / "plan.json"
        with patch("sys.stdout"), patch("sys.stderr"):
            self.assertEqual(script.main(["--db", str(self.store.db_path),
                                          "--out", str(out)]), 0)
        rep = json.loads(out.read_text())
        for a in rep["plan"]["actions"]:
            if a["entity_id"] == ids["code"]:
                a["to_kind"] = "org"
        out.write_text(json.dumps(rep))
        with sqlite3.connect(f"file:{self.store.db_path}?mode=ro",
                             uri=True) as c:
            before = list(c.iterdump())
        with patch("sys.stdout"), patch("sys.stderr"):
            rc = script.main(["--db", str(self.store.db_path), "--plan-file",
                              str(out), "--apply"])
        self.assertEqual(rc, 2)
        with sqlite3.connect(f"file:{self.store.db_path}?mode=ro",
                             uri=True) as c:
            self.assertEqual(list(c.iterdump()), before)
        self.assertFalse(list(Path(self.tmp).glob("*.bak-entity-repair-*")))

    def test_the_report_keeps_a_persons_id_not_their_name(self):
        self.person("Omar Haddad")
        self.ent("Omar Haddad", "idea", None)
        p = er.report_safe(self.plan())
        drop = next(a for a in p["actions"] if a["rule"] == "person_name")
        self.assertEqual(set(drop["person"]), {"id"})


class RunGuardTests(_Case):
    """One run at a time, never on a read-only store, backups retried
    slowly."""

    def test_an_unfinished_run_blocks_the_others(self):
        self.ent("Sprint 9", "project", None)
        self.store.entity_repair_start_run("reviewed-x", mode="reviewed")
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            res = er.auto_run(self.store)
        self.assertEqual((res["ran"], res["why"]), (False, "busy"))
        self.assertFalse(list(Path(self.tmp).glob("*.bak-entity-repair-*")))
        with self.assertRaises(er.PlanRefused) as cm:
            er.apply(self.store, self.plan())
        self.assertIn("in progress", cm.exception.reason)
        self.assertEqual(er.undo(self.store, run_id="reviewed-x")["busy"],
                         "reviewed-x")
        # An Inbox answer meanwhile writes nothing and stays open.
        a = next(x for x in self.plan()["actions"] if x["action"] != "keep")
        pid = er.propose(self.store, a, run_id="reviewed-x")
        res = er.decide(self.store, pid, "accept")
        self.assertEqual((res["ok"], res["status"]), (False, "open"))
        self.assertEqual([x["id"] for x in er.proposals(self.store)], [pid])
        # A run that never finished an hour ago crashed: it blocks nothing.
        self.assertIsNotNone(self.store.entity_repair_active_run())
        self.assertIsNone(self.store.entity_repair_active_run(
            now=time.time() + 3601))
        self.store.entity_repair_finish_run("reviewed-x", {})
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            self.assertTrue(er.auto_run(self.store)["ran"])
        self.assertIsNone(self.store.entity_repair_active_run())

    def test_a_sighting_between_plan_and_apply_skips_the_hide(self):
        a = self.ent("Ravel Pilot", "project", None)
        self.fact(a, source=NOTE)
        b = self.ent("Ravel Board", "project", None)
        self.fact(b, source=NOTE)
        p = self.plan()
        self.assertEqual(self.one(p, a)["rule"], "note_only")
        self.store.touch_entity(a, ts=NOW + 99 * DAY)   # the app saw it
        res = er.apply(self.store, p, mode="auto")
        why = {x["entity_id"]: x["why"] for x in res["skipped"]}
        self.assertEqual(why.get(a), "seen_since")
        self.assertFalse(self.row(a)["hidden"])
        self.assertTrue(self.row(b)["hidden"])

    def test_never_on_a_read_only_store(self):
        self.ent("Sprint 9", "project", None)
        ro = _mk(self.tmp, readonly=True)
        self.addCleanup(ro.close)
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            self.assertEqual(er.auto_run(ro)["why"], "read_only")
        with self.assertRaises(er.PlanRefused):
            er.apply(ro, er.plan(ro))
        self.assertFalse(list(Path(self.tmp).glob("*.bak-entity-repair-*")))

    def test_a_failed_backup_is_not_retried_at_every_graph_job(self):
        self.ent("Sprint 9", "project", None)
        calls = []

        def boom(dest):
            calls.append(dest)
            raise OSError("disk full")
        er._BACKUP_FAILED.clear()
        self.addCleanup(er._BACKUP_FAILED.clear)
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}), \
                patch.object(self.store, "backup_to", side_effect=boom):
            self.assertEqual(er.auto_run(self.store)["why"], "no_backup")
            self.assertEqual(er.auto_run(self.store)["why"], "no_backup")
            self.assertEqual(len(calls), 1)
            with patch.dict(os.environ,
                            {"QUILL_ENTITY_REPAIR_BACKUP_RETRY_S": "0"}):
                er.auto_run(self.store)
            self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.entity_repair_runs(), [])


# ------------------------------------------------------ automatic path --
class AutoTests(_Case):
    def world(self):
        ids = {"code": self.ent("Sprint 9", "project", None),
               "legacy": self.ent("Coastal Review", "project", None),
               "phrase": self.ent("coastal survey", "project", None),
               "note": self.ent("Ravel Pilot", "project", None),
               "dup_a": self.ent("Vesper Logistics", "org"),
               "dup_b": self.ent("Vesper-Logistics", "org")}
        for k in ("legacy", "phrase"):
            self.fact(ids[k])
            self.fact(ids[k])
        self.fact(ids["note"], source=NOTE)
        return ids

    def test_it_is_off_unless_the_flag_says_so(self):
        self.world()
        self.assertFalse(er.auto_enabled())
        self.assertEqual(er.auto_run(self.store)["why"], "off")
        self.assertEqual(self.store.entity_repair_runs(), [])

    def test_it_never_runs_without_a_backup(self):
        ids = self.world()
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}), \
                patch.object(self.store, "backup_to",
                             side_effect=OSError("disk full")):
            res = er.auto_run(self.store)
        self.assertEqual((res["ran"], res["why"]), (False, "no_backup"))
        self.assertEqual(self.store.entity_repair_runs(), [])
        self.assertEqual(self.row(ids["code"])["kind"], "project")

    def test_it_applies_the_sure_changes_and_asks_the_rest(self):
        ids = self.world()
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            res = er.auto_run(self.store)
        self.assertTrue(res["ran"])
        bak = Path(res["backup"])
        self.assertTrue(bak.exists())
        with sqlite3.connect(bak) as c:      # the backup is the pre-run store
            self.assertEqual(c.execute(
                "SELECT kind FROM entities WHERE id = ?",
                (ids["code"],)).fetchone()[0], "project")
        self.assertEqual(self.row(ids["code"])["kind"], "label")
        note = self.row(ids["note"])           # a drop is a hide here
        self.assertTrue(note["hidden"])
        self.assertEqual(self.row(ids["legacy"])["kind"], "project")  # D4
        self.assertEqual(self.row(ids["phrase"])["kind"], "project")  # asked
        self.assertIsNotNone(self.row(ids["dup_b"]))     # merges are asked
        props = {(p["action"], p["entity_id"]) for p in er.proposals(
            self.store)}
        self.assertIn(("merge", ids["dup_b"]), props)
        self.assertIn(("rekind", ids["phrase"]), props)
        # A legacy project nothing speaks against is not even asked.
        self.assertNotIn(ids["legacy"], {e for _, e in props})
        self.assertEqual(res["proposed"], len(props))

    def test_it_runs_once_per_rules_version(self):
        self.world()
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            first = er.auto_run(self.store)
            second = er.auto_run(self.store)
        self.assertTrue(first["ran"])
        self.assertEqual((second["ran"], second["why"], second["run_id"]),
                         (False, "done", first["run_id"]))
        with patch.object(er, "RULES_VERSION", "entity-repair-next"), \
                patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            self.assertTrue(er.auto_run(self.store)["ran"])

    def test_a_dismissed_proposal_is_not_asked_again(self):
        ids = self.world()
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            er.auto_run(self.store)
        pid = next(p["id"] for p in er.proposals(self.store)
                   if p["entity_id"] == ids["dup_b"])
        self.assertTrue(er.decide(self.store, pid, "dismiss")["ok"])
        er.auto_run(self.store, force=True)
        self.assertNotIn(ids["dup_b"], [p["entity_id"] for p in
                                        er.proposals(self.store)])

    def test_the_graph_job_runs_it_after_the_scope_pass(self):
        src = (ROOT / "app/main.py").read_text()
        i_scope = src.index("entity_scope.run()")
        i_repair = src.index("entity_repair.auto_run(")
        self.assertLess(i_scope, i_repair)
        self.assertIn("entity_repair.auto_enabled()", src)


# ---------------------------------------------------------------- undo --
class UndoTests(_Case):
    def run_auto(self):
        self.filed("Harbor Line")
        ids = {"code": self.ent("Sprint 9", "project", None),
               "label": self.ent("Wave 4", "label", "model"),
               "note": self.ent("Ravel Pilot", "project", None)}
        self.fact(ids["label"], project="Harbor Line")
        self.fact(ids["note"], source=NOTE)
        before = {k: self.row(v) for k, v in ids.items()}
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            res = er.auto_run(self.store)
        return ids, before, res

    def test_undoing_a_run_puts_every_logged_field_back(self):
        ids, before, res = self.run_auto()
        self.assertIsNotNone(self.row(ids["label"])["parent_id"])
        out = er.undo(self.store, run_id=res["run_id"])
        self.assertEqual(out["refused"], [])
        for k, eid in ids.items():
            now = self.row(eid)
            for f in ("kind", "kind_source", "kind_confidence", "hidden",
                      "parent_id", "parent_source"):
                self.assertEqual(now[f], before[k][f], (k, f))
        with self.store._lock:
            self.assertFalse(self.store._conn.execute(
                "SELECT 1 FROM relations WHERE predicate = 'part_of'"
            ).fetchall())
        run = [r for r in self.store.entity_repair_runs()
               if r["run_id"] == res["run_id"]][0]
        self.assertIsNotNone(run["undone_at"])

    def test_a_field_the_user_changed_since_is_refused(self):
        ids, before, res = self.run_auto()
        self.store.set_entity_kind(ids["code"], "tool", source="user")
        self.store.resolve_entity("Ravel Pilot", "project", ts=NOW,
                                  kind_source="user")          # unhides it
        out = er.undo(self.store, run_id=res["run_id"])
        why = {r["entity_id"]: r["why"] for r in out["refused"]}
        self.assertEqual(why.get(ids["code"]), "changed_by_user")
        self.assertEqual(why.get(ids["note"]), "changed_by_user")
        self.assertEqual(self.row(ids["code"])["kind"], "tool")
        self.assertIsNone(self.row(ids["label"])["parent_id"])   # undone

    def test_one_change_at_a_time(self):
        ids, before, res = self.run_auto()
        log = self.store.entity_repair_log(run_id=res["run_id"])
        cid = next(r["id"] for r in log if r["entity_id"] == ids["code"])
        out = er.undo(self.store, change_id=cid)
        self.assertEqual([r["change_id"] for r in out["undone"]], [cid])
        self.assertEqual(self.row(ids["code"])["kind"], "project")
        self.assertEqual(self.row(ids["label"])["kind"], "label")
        self.assertEqual(er.undo(self.store, change_id=cid)["undone"], [])

    def test_undoing_a_run_withdraws_its_inbox_questions(self):
        self.ent("Vesper Logistics", "org")
        dup = self.ent("Vesper-Logistics", "org")
        ids, before, res = self.run_auto()
        self.assertTrue(er.proposals(self.store))
        out = er.undo(self.store, run_id=res["run_id"])
        self.assertGreaterEqual(out["withdrawn"], 1)
        self.assertEqual(er.proposals(self.store), [])
        data = inbox.collect(self.store)
        self.assertFalse([i for i in data["needs_you"] + data["can_wait"]
                          if i["id"].startswith("entityfix:")])
        # Never answered, so a later run may ask again.
        er.auto_run(self.store, force=True)
        self.assertIn(dup, [p["entity_id"] for p in er.proposals(self.store)])

    def test_a_parent_that_is_gone_is_not_put_back(self):
        f = self.filed("Harbor Line")
        other = self.ent("Kestrel Rollout", "project", "user")
        lab = self.ent("Wave 4", "label", "model")
        self.assertTrue(self.store.set_entity_parent(lab, other,
                                                     source="rule"))
        self.fact(lab, project="Harbor Line")     # evidence moved to P
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            res = er.auto_run(self.store)
        self.assertEqual(self.row(lab)["parent_id"], f)
        self.store.delete_entity(other)
        out = er.undo(self.store, run_id=res["run_id"])
        self.assertEqual([r["why"] for r in out["refused"]], ["gone"])
        self.assertEqual(self.row(lab)["parent_id"], f)

    def test_a_merge_or_a_deletion_points_at_the_backup(self):
        a, b = self.ent("Vesper Logistics", "org"), \
            self.ent("Vesper-Logistics", "org")
        res = er.apply(self.store, self.plan(), backup="/tmp/x.bak")
        out = er.undo(self.store, run_id=res["run_id"])
        self.assertEqual(len(out["refused"]), 1)
        self.assertIn("/tmp/x.bak", out["refused"][0]["why"])
        self.assertIsNone(self.row(b))

    def test_the_undo_route(self):
        ids, before, res = self.run_auto()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(routes_mod.router)
        with patch.object(routes_mod.memory, "_ensure_store",
                          return_value=self.store):
            c = TestClient(app)
            r = c.get("/entities/repair/runs", params={"run_id": res["run_id"]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["changes"])
            r = c.post("/entities/repair/undo", json={"run_id": res["run_id"]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["undone"])
            self.assertEqual(c.post("/entities/repair/undo",
                                    json={}).status_code, 400)
            self.assertEqual(c.post("/entities/repair/undo",
                                    json={"run_id": "nope"}).status_code, 404)
            r = c.get("/entities/repair/plan")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("actions", r.json())
            # Another run in flight: refused, nothing undone.
            self.store.entity_repair_start_run("auto-busy", mode="auto")
            self.assertEqual(c.post("/entities/repair/undo",
                                    json={"run_id": res["run_id"]}).status_code,
                             409)
            self.store.entity_repair_finish_run("auto-busy", {})
        self.assertEqual(self.row(ids["code"])["kind"], "project")


# --------------------------------------------------------------- inbox --
class InboxTests(_Case):
    def setUp(self):
        super().setUp()
        self.a = self.ent("Vesper Logistics", "org")
        self.b = self.ent("Vesper-Logistics", "org")
        self.c = self.ent("coastal survey", "project", None)
        self.fact(self.c)
        self.fact(self.c)
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            er.auto_run(self.store)

    def items(self):
        data = inbox.collect(self.store)
        return {i["id"]: i for i in data["needs_you"] + data["can_wait"]
                if i["id"].startswith("entityfix:")}

    def test_proposals_are_inbox_items_and_their_answers_apply(self):
        items = self.items()
        merge = next(i for i in items.values() if i["kind"] == "confirm")
        self.assertIn("Vesper-Logistics", merge["title"])
        self.assertEqual([a["key"] for a in merge["actions"]],
                         ["yes", "no", "later"])
        rek = next(i for i in items.values() if i["kind"] == "review")
        self.assertIn("a topic instead of a project", rek["title"])
        out = inbox.act(self.store, merge["id"], "yes")
        self.assertTrue(out["ok"], out)
        self.assertNotIn("undo_token", out)        # a merge has no undo
        self.assertIsNone(self.store.get_entity(self.b))
        out = inbox.act(self.store, rek["id"], "yes")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.store.get_entity(self.c)["kind"], "idea")
        self.assertIn("undo_token", out)
        self.assertEqual(self.items(), {})
        inbox.undo(out["undo_token"])
        self.assertEqual(self.store.get_entity(self.c)["kind"], "project")
        # Undo puts the question back; the merge, answered, stays answered.
        self.assertEqual(set(self.items()), {rek["id"]})
        # "Keep as is" has an undo too: the question comes back again.
        out = inbox.act(self.store, rek["id"], "no")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.items(), {})
        inbox.undo(out["undo_token"])
        self.assertEqual(set(self.items()), {rek["id"]})

    def test_no_closes_it_and_a_stale_one_writes_nothing(self):
        items = self.items()
        merge = next(i for i in items.values() if i["kind"] == "confirm")
        rek = next(i for i in items.values() if i["kind"] == "review")
        self.assertTrue(inbox.act(self.store, merge["id"], "no")["ok"])
        self.assertIsNotNone(self.store.get_entity(self.b))
        self.store.set_entity_kind(self.c, "org", source="user")
        out = inbox.act(self.store, rek["id"], "yes")
        self.assertFalse(out["ok"])
        self.assertEqual(self.store.get_entity(self.c)["kind"], "org")
        self.assertEqual(self.items(), {})

    def test_a_merge_of_two_kinds_names_them(self):
        a = self.ent("Tidewell", "org", None)
        b = self.ent("Tide Well", "project", None)
        er.auto_run(self.store, force=True)
        m = next(i for i in self.items().values() if i["kind"] == "confirm"
                 and "Tide" in i["title"])
        self.assertIn("(a project)", m["title"])
        self.assertIn("(an org)", m["title"])
        self.assertTrue({a, b})

    def test_a_merge_into_a_hidden_row_says_so(self):
        self.self_name("Sam Okafor")
        plain = self.ent("Lantern", "project", None)
        self.store.set_entity_hidden(plain, hidden=True)
        poss = self.ent("Sam Okafor's Lantern", "project", None)
        filed = self.ent("Harbor Line", "project", None)
        self.store.set_entity_attr(filed, "status", "active", None, NOW)
        self.capture("Harbor Line")          # filed, and the user touched it
        er.auto_run(self.store, force=True)
        items = self.items()
        m = next(i for i in items.values() if i["kind"] == "confirm"
                 and "Lantern" in i["title"])
        self.assertIn("hidden", m["title"])
        self.assertIn("leave the tab", m["evidence"][0]["value"])
        src = next(i for i in items.values() if "Harbor Line" in i["title"])
        self.assertIn("for good", src["title"])
        self.assertNotIn("instead of", src["title"])
        self.assertIsNotNone(self.store.get_entity(poss))

    def test_handled_counts_what_was_tidied_on_its_own(self):
        h = inbox.handled(self.store, now=time.time())
        kinds = {b["kind"]: b["count"] for b in h["breakdown"]}
        self.assertNotIn("Entities tidied on their own", kinds)  # all asked
        d = self.ent("Sprint 9", "project", None)
        er.auto_run(self.store, force=True)
        self.assertEqual(self.store.get_entity(d)["kind"], "label")
        h = inbox.handled(self.store, now=time.time())
        kinds = {b["kind"]: b["count"] for b in h["breakdown"]}
        self.assertGreaterEqual(kinds.get("Entities tidied on their own", 0),
                                1)


# ------------------------------------------- a real sighting brings it back
class NoteOnlyLiftTests(_Case):
    def setUp(self):
        super().setUp()
        self.e = self.ent("Ravel Pilot", "project", None)
        self.fact(self.e, source=NOTE)
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            er.auto_run(self.store)
        self.assertTrue(self.row(self.e)["hidden"])

    def sight(self, day: float, source=MAIL, cls="connector_mail"):
        from app.services import entity_candidates as cand
        ev = self.event(source, gap=day * DAY)
        ctx = cand.Context(event_id=ev, event_source=source,
                           source_class=cls, mint_ok=True,
                           observed_at=self._t, now=time.time(), text="")
        cand.record_bound(self.store, "Ravel Pilot", self.e, ctx,
                          kind="project", confidence=0.8)

    def test_two_real_sessions_show_it_again(self):
        self.sight(1)
        self.assertTrue(self.row(self.e)["hidden"])
        self.sight(2)
        self.assertFalse(self.row(self.e)["hidden"])
        hold = self.store.entity_repair_hold(self.e)
        self.assertIsNone(hold)

    def test_more_notes_do_not(self):
        for d in (1, 2, 3):
            self.sight(d, source=NOTE, cls="assistant_capture")
        self.assertTrue(self.row(self.e)["hidden"])

    def test_another_hide_is_not_lifted(self):
        v = self.ent("Ravel Board", "project", None)
        self.store.set_entity_hidden(v, hidden=True)
        from app.services import entity_candidates as cand
        for d in (1, 2):
            ev = self.event(MAIL, gap=d * DAY)
            ctx = cand.Context(event_id=ev, event_source=MAIL,
                               source_class="connector_mail", mint_ok=True,
                               observed_at=self._t, now=time.time(), text="")
            cand.record_bound(self.store, "Ravel Board", v, ctx,
                              kind="project", confidence=0.8)
        self.assertTrue(self.row(v)["hidden"])


# -------------------------------------------------------------- script --
class ScriptTests(_Case):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location(
            "entity_repair_script", ROOT / "scripts" / "entity_repair.py")
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)
        self.code = self.ent("Sprint 9", "project", None)
        self.a = self.ent("Vesper Logistics", "org")
        self.b = self.ent("Vesper-Logistics", "org")
        self.db = self.store.db_path

    def dump(self):
        with sqlite3.connect(f"file:{self.db}?mode=ro", uri=True) as c:
            return list(c.iterdump())

    def run_script(self, *argv):
        with patch("sys.stdout"), patch("sys.stderr"):
            return self.script.main(["--db", str(self.db), *argv])

    def test_a_dry_run_writes_its_report_and_nothing_else(self):
        before = self.dump()
        out = Path(self.tmp) / "plan.json"
        self.assertEqual(self.run_script("--out", str(out)), 0)
        self.assertEqual(self.dump(), before)
        rep = json.loads(out.read_text())
        self.assertIn("plan", rep)
        self.assertFalse(list(Path(self.tmp).glob("*.bak-entity-repair-*")))

    def test_a_reviewed_plan_file_is_applied_after_a_backup(self):
        out = Path(self.tmp) / "plan.json"
        self.run_script("--out", str(out))
        self.assertEqual(self.run_script("--plan-file", str(out), "--apply",
                                         "--out", str(Path(self.tmp) / "r.json")),
                         0)
        baks = list(Path(self.tmp).glob("quill.db.bak-entity-repair-*"))
        self.assertEqual(len(baks), 1)
        with sqlite3.connect(baks[0]) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM entities")
                             .fetchone()[0], 3)
        self.assertIsNone(self.store.get_entity(self.b))
        self.assertEqual(self.store.get_entity(self.code)["kind"], "label")
        # Applied once; the same file again is stale now.
        before = self.dump()
        self.assertEqual(self.run_script("--plan-file", str(out), "--apply"),
                         2)
        self.assertEqual(self.dump(), before)

    def test_a_plan_from_another_database_is_refused(self):
        out = Path(self.tmp) / "plan.json"
        self.run_script("--out", str(out))
        other_dir = Path(tempfile.mkdtemp(prefix="quill_repair_o_"))
        other = _mk(other_dir)
        for n in ("Sprint 9", "Vesper Logistics", "Vesper-Logistics"):
            other.resolve_entity(n, "org", ts=NOW)
        other.close()
        with patch("sys.stdout"), patch("sys.stderr"):
            rc = self.script.main(["--db", str(other_dir / "quill.db"),
                                   "--plan-file", str(out), "--apply"])
        self.assertEqual(rc, 2)
        self.assertFalse(list(other_dir.glob("*.bak-entity-repair-*")))

    def test_the_backup_comes_before_any_write_even_a_migration(self):
        # A store from before Phase 4: opening it for writing creates the
        # repair tables, so a backup taken after the open would have them.
        self.store.close()
        with sqlite3.connect(self.db) as c:
            for t in ("entity_repair_runs", "entity_repair_log",
                      "entity_repair_proposals", "entity_merges"):
                c.execute(f"DROP TABLE {t}")
        for argv, tag in ((["--auto"], "auto"), (["--undo", "nope"], "undo")):
            rep = Path(self.tmp) / f"{tag}.json"
            self.run_script(*argv, "--out", str(rep))
            bak = sorted(Path(self.tmp).glob(
                f"quill.db.bak-entity-repair-{tag}-*"))
            self.assertEqual(len(bak), 1, tag)
            with sqlite3.connect(bak[0]) as c:
                names = {r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("entity_repair_runs", names, tag)
            with sqlite3.connect(self.db) as c:
                self.assertTrue(c.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = "
                    "'entity_repair_runs'").fetchone())
            # A rerun of the setup for the second case: drop again.
            with sqlite3.connect(self.db) as c:
                for t in ("entity_repair_runs", "entity_repair_log",
                          "entity_repair_proposals", "entity_merges"):
                    c.execute(f"DROP TABLE IF EXISTS {t}")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)

    def test_auto_then_undo_from_the_script(self):
        rep = Path(self.tmp) / "auto.json"
        self.assertEqual(self.run_script("--auto", "--out", str(rep)), 0)
        run_id = json.loads(rep.read_text())["auto"]["run_id"]
        self.assertEqual(self.store.get_entity(self.code)["kind"], "label")
        self.assertEqual(self.run_script("--undo", run_id, "--out",
                                         str(Path(self.tmp) / "u.json")), 0)
        self.assertEqual(self.store.get_entity(self.code)["kind"], "project")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
