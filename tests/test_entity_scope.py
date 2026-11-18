"""Entity model, Phase 3: scope.

A label, a topic or an unfiled "project" that only ever comes up inside one
filed project goes inside it (`entity_scope`, the `graph` job, after the
rollup). The parent is stored on the row (`parent_id`, `parent_source`) and
mirrored as one `part_of` relation with origin `context`. A parent the user
set or removed is never touched by the rule; orgs, tools and places are
never children. The rollup's homes are never subjects.

Every test runs against a real Store on a temp db; facts, events and
sightings are written the way the pipeline writes them.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Imported here, before any test patches `app.storage.get_store`.
import app.api.routes  # noqa: F401
from app.events import Event, Modality
from app.services import entity_scope as scope
from app.services import project_rollup
from app.services import projects
from app.storage import Store

NOW = 1_700_000_000.0
DAY = 86400.0
NOTE = "assistant.claude"       # an accepted note, filed under a project
MAIL = "google.mail"
SCREEN = "desktop.screen"


def _mk(tmp: str, **kw) -> Store:
    return Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "audio",
                 **kw)


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_scope_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        # The filed tier lives in this seat's assistant database; no org
        # scopes (they come from a membership file).
        env = patch.dict(os.environ, {"QUILL_DATA_DIR": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        for var in ("QUILL_ENTITY_SCOPE", "QUILL_ENTITY_SCOPE_MIN_EVENTS",
                    "QUILL_ENTITY_SCOPE_MIN_EVENTS_LABEL",
                    "QUILL_ROLLUP_FILED_HOMES", "QUILL_ROLLUP_HOME_KINDS"):
            os.environ.pop(var, None)
        p = patch.object(projects, "_scopes", lambda: [])
        p.start()
        self.addCleanup(p.stop)
        projects.invalidate_filed_keys()
        self.addCleanup(projects.invalidate_filed_keys)
        self._t = NOW

    # -- the world -----------------------------------------------------
    def filed(self, name: str) -> int:
        """A project the user filed a note under (the accept's two writes:
        the accepted capture and `ensure_filed`)."""
        from app.services.remote_mcp import connect
        with connect() as c:
            c.execute("INSERT INTO captures (created_at, project, kind, text, "
                      "status, decided_at) VALUES (?, ?, 'note', 'x', "
                      "'accepted', ?)", (NOW, name, NOW))
            c.commit()
        return projects.ensure_filed(name, store=self.store, now=NOW,
                                     user_accept=True)

    def ent(self, name: str, kind: str, source: str | None = "model") -> int:
        return self.store.resolve_entity(name, kind, ts=NOW,
                                         kind_source=source)

    def event(self, *, project: str | None = None, source: str = NOTE,
              raw: str = "a note") -> int:
        self._t += 600
        meta = {"project": project} if project else {}
        return int(self.store.insert(Event(
            time=self._t, modality=Modality.TEXT, raw=raw, source=source,
            meta=meta)))

    def fact(self, eid: int, *, project: str | None = None,
             source: str = NOTE, origin: str = "derived") -> int:
        """A fact `about` the entity, drawn from a new event (filed under
        `project` when given)."""
        ev = self.event(project=project, source=source)
        with self.store._lock:
            cur = self.store._conn.execute(
                "INSERT INTO facts (kind, text, source_event_id, "
                "extracted_at) VALUES ('claim', 'x', ?, ?)", (ev, self._t))
            fid = int(cur.lastrowid)
            self.store._conn.commit()
        self.store.add_relation("fact", fid, "about", "entity", eid,
                                origin=origin, ts=self._t)
        return ev

    def sighting(self, eid: int, name: str, *, project: str | None = None,
                 parent: str | None = None, source: str = MAIL) -> int:
        ev = self.event(project=project, source=source)
        self.store.record_entity_mention(
            name, proposed_kind="label", confidence=0.8,
            source_class="connector_mail", event_source=source, event_id=ev,
            observed_at=self._t, entity_id=eid, filing_project=project,
            parent=parent)
        return ev

    def parent(self, eid: int) -> tuple:
        row = self.store.get_entity(eid)
        return row["parent_id"], row["parent_source"]

    def mirrors(self, eid: int) -> list[tuple]:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT obj_id, origin, weight FROM relations "
                "WHERE subj_type = 'entity' AND subj_id = ? "
                "AND predicate = 'part_of' AND obj_type = 'entity' "
                "ORDER BY obj_id", (eid,)).fetchall()
        return [(int(r["obj_id"]), r["origin"], float(r["weight"]))
                for r in rows]

    def plan_row(self, eid: int) -> dict:
        return next(r for r in scope.compute(self.store)
                    if r["entity_id"] == eid)


# ------------------------------------------------------- the store write --
class SetEntityParentTests(_Case):
    def test_set_reads_back_and_mirrors_one_context_edge(self):
        p = self.ent("Harbor Line", "project", "user")
        x = self.ent("Wave 4", "label")
        before = self.store.memory_version()
        self.assertTrue(self.store.set_entity_parent(x, p, source="rule"))
        self.assertEqual(self.parent(x), (p, "rule"))
        listed = {e["id"]: e for e in self.store.all_entities()}
        self.assertEqual((listed[x]["parent_id"], listed[x]["parent_source"]),
                         (p, "rule"))
        self.assertEqual(self.mirrors(x), [(p, "context", 1.0)])
        self.assertNotEqual(self.store.memory_version(), before)

    def test_a_move_takes_the_mirror_along_and_leaves_asserted_edges(self):
        p1 = self.ent("Harbor Line", "project", "user")
        p2 = self.ent("Dock Seven", "project", "user")
        x = self.ent("Wave 4", "label")
        # The extractor asserted "Wave 4 part_of Dock Seven" on its own.
        self.store.add_relation("entity", x, "part_of", "entity", p2,
                                origin="asserted", ts=NOW)
        self.assertTrue(self.store.set_entity_parent(x, p1, source="rule"))
        self.assertTrue(self.store.set_entity_parent(x, p2, source="rule"))
        self.assertEqual(self.parent(x), (p2, "rule"))
        # The context edge to p1 is gone; the asserted one to p2 stays as is.
        self.assertEqual(self.mirrors(x), [(p2, "asserted", 1.0)])
        self.assertTrue(self.store.set_entity_parent(x, p1, source="rule"))
        self.assertEqual(self.mirrors(x), [(p1, "context", 1.0),
                                           (p2, "asserted", 1.0)])

    def test_a_rule_removal_leaves_no_source(self):
        p = self.ent("Harbor Line", "project", "user")
        x = self.ent("Wave 4", "label")
        self.store.set_entity_parent(x, p, source="rule")
        self.assertTrue(self.store.set_entity_parent(x, None, source="rule"))
        self.assertEqual(self.parent(x), (None, None))
        self.assertEqual(self.mirrors(x), [])

    def test_a_user_parent_is_never_overwritten_by_the_rule(self):
        p1 = self.ent("Harbor Line", "project", "user")
        p2 = self.ent("Dock Seven", "project", "user")
        x = self.ent("Wave 4", "label")
        self.assertTrue(self.store.set_entity_parent(x, p1, source="user"))
        self.assertFalse(self.store.set_entity_parent(x, p2, source="rule"))
        self.assertFalse(self.store.set_entity_parent(x, None, source="rule"))
        self.assertEqual(self.parent(x), (p1, "user"))
        # The user may move it.
        self.assertTrue(self.store.set_entity_parent(x, p2, source="user"))
        self.assertEqual(self.parent(x), (p2, "user"))

    def test_a_user_removal_is_kept(self):
        p = self.ent("Harbor Line", "project", "user")
        x = self.ent("Wave 4", "label")
        self.store.set_entity_parent(x, p, source="rule")
        self.assertTrue(self.store.set_entity_parent(x, None, source="user"))
        self.assertEqual(self.parent(x), (None, "user"))
        self.assertFalse(self.store.set_entity_parent(x, p, source="rule"))
        self.assertEqual(self.parent(x), (None, "user"))

    def test_refusals(self):
        p = self.ent("Harbor Line", "project", "user")
        x = self.ent("Wave 4", "label")
        hidden = self.ent("Old Pier", "project", "user")
        self.store.set_entity_hidden(hidden)
        self.assertFalse(self.store.set_entity_parent(x, x))       # itself
        self.assertFalse(self.store.set_entity_parent(424242, p))  # no child
        self.assertFalse(self.store.set_entity_parent(x, 424242))  # no parent
        self.assertFalse(self.store.set_entity_parent(x, hidden))  # hidden
        for kind in ("org", "tool", "place"):
            k = self.ent(f"A {kind} row", kind)
            self.assertFalse(self.store.set_entity_parent(k, p,
                                                          source="user"))
            self.assertEqual(self.parent(k), (None, None))
        self.assertEqual(self.parent(x), (None, None))

    def test_no_cycles(self):
        a = self.ent("Alpha Track", "project")
        b = self.ent("Beta Track", "project")
        c = self.ent("Gamma Track", "project")
        self.assertTrue(self.store.set_entity_parent(a, b, source="user"))
        self.assertFalse(self.store.set_entity_parent(b, a, source="user"))
        self.assertTrue(self.store.set_entity_parent(b, c, source="user"))
        self.assertFalse(self.store.set_entity_parent(c, a, source="user"))
        self.assertEqual(self.parent(c), (None, None))

    def test_the_same_parent_again_writes_nothing(self):
        p = self.ent("Harbor Line", "project", "user")
        x = self.ent("Wave 4", "label")
        self.store.set_entity_parent(x, p, source="rule")
        n = self.store._conn.total_changes
        self.assertTrue(self.store.set_entity_parent(x, p, source="rule"))
        self.assertEqual(self.store._conn.total_changes, n)
        # The user taking it over moves only the source; the edge is not
        # added a second time (its weight would add up).
        self.assertTrue(self.store.set_entity_parent(x, p, source="user"))
        self.assertEqual(self.parent(x), (p, "user"))
        self.assertEqual(self.mirrors(x), [(p, "context", 1.0)])

    def test_forgetting_the_parent_frees_the_child(self):
        p = self.ent("Harbor Line", "project", "user")
        x = self.ent("Wave 4", "label")
        self.store.set_entity_parent(x, p, source="user")
        self.store.delete_entity(p, tombstone=True, reason="forget")
        self.assertEqual(self.parent(x), (None, None))
        self.assertEqual(self.mirrors(x), [])

    def test_a_read_only_store_refuses_and_still_reads(self):
        p = self.ent("Harbor Line", "project", "user")
        x = self.ent("Wave 4", "label")
        self.store.close()
        ro = _mk(self.tmp, readonly=True)
        self.addCleanup(ro.close)
        self.assertFalse(ro.set_entity_parent(x, p, source="user"))
        ev = ro.entity_scope_evidence()
        self.assertEqual((ev["facts"], ev["mentions"], ev["affiliated"]),
                         ([], [], []))

    def test_a_store_without_the_ledger_reads_no_sightings(self):
        self.store.close()
        raw = sqlite3.connect(Path(self.tmp) / "t.db")
        raw.execute("DROP TABLE entity_mentions")
        raw.commit()
        raw.close()
        ro = _mk(self.tmp, readonly=True)
        self.addCleanup(ro.close)
        self.assertEqual(ro.entity_scope_evidence()["mentions"], [])


# ------------------------------------------------------------- the rule --
class ScopeRuleTests(_Case):
    def test_a_label_filed_once_goes_inside_its_project(self):
        p = self.filed("Harbor Line")
        x = self.ent("Wave 4", "label")
        self.fact(x, project="Harbor Line")
        res = scope.run(self.store)
        self.assertEqual((res["set"], res["cleared"]), (1, 0))
        self.assertEqual(self.parent(x), (p, "rule"))
        self.assertEqual(self.mirrors(x), [(p, "context", 1.0)])

    def test_a_topic_needs_two_events(self):
        p = self.filed("Harbor Line")
        x = self.ent("Pallet audit", "idea")
        self.fact(x, project="Harbor Line")
        self.assertEqual(self.plan_row(x)["reason"], "too_few_events")
        scope.run(self.store)
        self.assertEqual(self.parent(x), (None, None))
        self.fact(x, project="Harbor Line")
        scope.run(self.store)
        self.assertEqual(self.parent(x), (p, "rule"))

    def test_an_unfiled_model_project_and_a_thing_go_inside(self):
        p = self.filed("Harbor Line")
        x = self.ent("Crane refit", "project")
        y = self.ent("Spare gearbox", "thing")
        for _ in range(2):
            self.fact(x, project="Harbor Line")
            self.fact(y, project="Harbor Line")
        scope.run(self.store)
        self.assertEqual(self.parent(x), (p, "rule"))
        self.assertEqual(self.parent(y), (p, "rule"))

    def test_the_thresholds_are_read_from_the_environment(self):
        p = self.filed("Harbor Line")
        x = self.ent("Wave 4", "label")
        y = self.ent("Pallet audit", "idea")
        for _ in range(2):
            self.fact(x, project="Harbor Line")
            self.fact(y, project="Harbor Line")
        with patch.dict(os.environ, {"QUILL_ENTITY_SCOPE_MIN_EVENTS": "3",
                                     "QUILL_ENTITY_SCOPE_MIN_EVENTS_LABEL":
                                     "3"}):
            scope.run(self.store)
            self.assertEqual(self.parent(x), (None, None))
            self.assertEqual(self.parent(y), (None, None))
        scope.run(self.store)
        self.assertEqual((self.parent(x)[0], self.parent(y)[0]), (p, p))

    def test_a_filed_project_and_an_org_tool_or_place_are_never_children(self):
        self.filed("Harbor Line")
        q = self.filed("Dock Seven")
        rows = [q] + [self.ent(f"Kind {k}", k) for k in ("org", "tool",
                                                        "place")]
        for eid in rows:
            for _ in range(3):
                self.fact(eid, project="Harbor Line")
        scope.run(self.store)
        for eid in rows:
            self.assertEqual(self.parent(eid), (None, None))
        self.assertEqual(self.plan_row(q)["reason"], "filed")
        self.assertEqual(self.plan_row(rows[1])["reason"], "kind")

    def test_evidence_outside_the_project_blocks_and_then_removes(self):
        p = self.filed("Harbor Line")
        x = self.ent("Pallet audit", "idea")
        y = self.ent("Berth plan", "idea")
        for eid in (x, y):
            for _ in range(2):
                self.fact(eid, project="Harbor Line")
        self.fact(y, source=MAIL)              # y comes up elsewhere too
        scope.run(self.store)
        self.assertEqual(self.parent(x), (p, "rule"))
        self.assertEqual(self.parent(y), (None, None))
        self.assertEqual(self.plan_row(y)["reason"], "outside_project")
        # Later, x comes up outside the project: the rule takes it back.
        self.fact(x, source=MAIL)
        res = scope.run(self.store)
        self.assertEqual(res["cleared"], 1)
        self.assertEqual(self.parent(x), (None, None))
        self.assertEqual(self.mirrors(x), [])

    def test_two_projects_or_miscellaneous_is_no_parent(self):
        self.filed("Harbor Line")
        self.filed("Dock Seven")
        x = self.ent("Pallet audit", "idea")
        self.fact(x, project="Harbor Line")
        self.fact(x, project="Dock Seven")
        y = self.ent("Berth plan", "label")
        self.fact(y, project="Harbor Line")
        self.fact(y, project=projects.MISC)
        scope.run(self.store)
        self.assertEqual(self.parent(x), (None, None))
        self.assertEqual(self.parent(y), (None, None))
        self.assertEqual(self.plan_row(y)["outside"], 1)

    def test_ledger_sightings_count_and_the_model_parent_only_when_filed(self):
        p = self.filed("Harbor Line")
        x = self.ent("Wave 4", "label")
        self.sighting(x, "Wave 4", project="Harbor Line")
        self.sighting(x, "Wave 4", parent="harbor line")   # filed: counts
        y = self.ent("Wave 5", "label")
        self.sighting(y, "Wave 5", project="Harbor Line")
        self.sighting(y, "Wave 5", parent="Somewhere Else")  # not filed
        scope.run(self.store)
        self.assertEqual(self.parent(x), (p, "rule"))
        self.assertEqual(self.plan_row(x)["inside"], 2)
        self.assertEqual(self.parent(y), (None, None))
        self.assertEqual(self.plan_row(y)["outside"], 1)

    def test_a_filing_wins_over_the_model_parent(self):
        self.filed("Harbor Line")
        self.filed("Dock Seven")
        x = self.ent("Wave 4", "label")
        self.sighting(x, "Wave 4", project="Harbor Line", parent="Dock Seven")
        scope.run(self.store)
        self.assertEqual(self.parent(x)[0],
                         self.store.find_entity_exact("Harbor Line"))

    def test_screen_text_and_a_purged_source_are_neutral(self):
        p = self.filed("Harbor Line")
        x = self.ent("Wave 4", "label")
        self.fact(x, project="Harbor Line")
        self.fact(x, source=SCREEN)
        gone = self.fact(x, source=MAIL)
        with self.store._lock:              # its source event was purged
            self.store._conn.execute("DELETE FROM events WHERE id = ?",
                                     (gone,))
            self.store._conn.commit()
        row = self.plan_row(x)
        self.assertEqual((row["inside"], row["outside"], row["neutral"]),
                         (1, 0, 1))
        scope.run(self.store)
        self.assertEqual(self.parent(x), (p, "rule"))

    def test_a_renamed_project_keeps_its_children(self):
        p = self.filed("Harbor Line")
        x = self.ent("Wave 4", "label")
        self.fact(x, project="Harbor Line")
        self.assertTrue(self.store.rename_entity(p, "Harbor Line Two"))
        scope.run(self.store)
        self.assertEqual(self.parent(x), (p, "rule"))

    def test_a_name_twin_of_the_project_does_not_hide_it(self):
        # "Heron" and "Project Heron" share a name key and both are filing
        # targets: a note filed under "Project Heron" still goes to that
        # row, and a rule parent moves there when the evidence does.
        p = self.filed("Harbor Line")
        q = self.filed("Project Heron")
        twin = self.ent("Heron", "project")
        x = self.ent("Wave 4", "label")
        ev = self.fact(x, project="Harbor Line")
        scope.run(self.store)
        self.assertEqual(self.parent(x), (p, "rule"))
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE events SET meta = ? WHERE id = ?",
                (json.dumps({"project": "Project Heron"}), ev))
            self.store._conn.commit()
        scope.run(self.store)
        self.assertEqual(self.parent(x), (q, "rule"))
        self.assertEqual(self.mirrors(x), [(q, "context", 1.0)])
        self.assertNotEqual(twin, q)

    def test_a_pair_the_user_unlinked_is_never_nested(self):
        from app.services import graph
        p = self.filed("Harbor Line")
        x = self.ent("Wave 4", "label")
        y = self.ent("Wave 5", "label")
        for eid in (x, y):
            self.fact(eid, project="Harbor Line")
        graph.unlink_constellation_edge(self.store, f"entity:{x}",
                                        f"entity:{p}")
        scope.run(self.store)
        self.assertEqual(self.parent(x), (None, None))
        self.assertEqual(self.plan_row(x)["reason"], "user_unlinked")
        self.assertEqual(self.parent(y), (p, "rule"))
        # Unlinking a rule parent in the graph takes it out as well.
        graph.unlink_constellation_edge(self.store, f"entity:{p}",
                                        f"entity:{y}")
        scope.run(self.store)
        self.assertEqual(self.parent(y), (None, None))
        self.assertEqual(self.mirrors(y), [])

    def test_a_user_parent_or_removal_is_held(self):
        p = self.filed("Harbor Line")
        q = self.filed("Dock Seven")
        x = self.ent("Wave 4", "label")
        y = self.ent("Wave 5", "label")
        for eid in (x, y):
            self.fact(eid, project="Harbor Line")
        self.store.set_entity_parent(x, q, source="user")
        self.store.set_entity_parent(y, None, source="user")
        res = scope.run(self.store)
        self.assertEqual((res["set"], res["held"]), (0, 2))
        self.assertEqual(self.parent(x), (q, "user"))
        self.assertEqual(self.parent(y), (None, "user"))
        self.assertNotEqual(p, q)

    def test_orgs_by_evidence_are_never_children(self):
        self.filed("Harbor Line")
        works = self.ent("Tessaly", "idea")         # someone works there
        person = self.store.resolve_person("Ines Marlow", ts=NOW)
        self.store.add_relation("person", person, "works_at", "entity", works,
                                origin="asserted", ts=NOW)
        short = self.ent("Halvard", "idea")         # "Halvard Freight" is an org
        self.ent("Halvard Freight", "org")
        legal = self.ent("Corvex Ltd", "idea")      # a legal form
        for eid in (works, short, legal):
            for _ in range(2):
                self.fact(eid, project="Harbor Line")
        scope.run(self.store)
        for eid, why in ((works, "people_work_there"),
                         (short, "org_short_form"),
                         (legal, "org_by_evidence")):
            self.assertEqual(self.parent(eid), (None, None))
            self.assertEqual(self.plan_row(eid)["reason"], why)

    def test_a_shortening_of_the_project_name_and_a_value_stay_out(self):
        self.filed("Atlas Rollout")
        short = self.ent("Atlas", "project")         # the project itself
        value = self.ent("2026-11-12", "idea")
        part = self.ent("Atlas Rollout budget", "idea")   # a part of it
        for eid in (short, value, part):
            for _ in range(2):
                self.fact(eid, project="Atlas Rollout")
        scope.run(self.store)
        self.assertEqual(self.parent(short), (None, None))
        self.assertEqual(self.plan_row(short)["reason"], "names_the_project")
        self.assertEqual(self.parent(value), (None, None))
        self.assertEqual(self.plan_row(value)["reason"], "value")
        self.assertEqual(self.parent(part)[0],
                         self.store.find_entity_exact("Atlas Rollout"))

    def test_a_rule_parent_goes_when_the_row_becomes_a_tool(self):
        p = self.filed("Harbor Line")
        x = self.ent("Wave 4", "label")
        self.fact(x, project="Harbor Line")
        scope.run(self.store)
        self.assertEqual(self.parent(x), (p, "rule"))
        self.store.set_entity_kind(x, "tool", source="user")
        scope.run(self.store)
        self.assertEqual(self.parent(x), (None, None))

    def test_a_graph_rebuild_keeps_the_mirror_edge(self):
        from app.services import graph
        p = self.filed("Harbor Line")
        x = self.ent("Wave 4", "label")
        self.fact(x, project="Harbor Line")
        scope.run(self.store)
        graph.rebuild(self.store)
        self.assertEqual(self.mirrors(x), [(p, "context", 1.0)])
        self.assertEqual(self.parent(x), (p, "rule"))

    def test_a_second_run_writes_nothing(self):
        self.filed("Harbor Line")
        x = self.ent("Wave 4", "label")
        self.fact(x, project="Harbor Line")
        scope.run(self.store)
        n = self.store._conn.total_changes
        v = self.store.memory_version()
        res = scope.run(self.store)
        self.assertEqual((res["set"], res["cleared"]), (0, 0))
        self.assertEqual(self.store._conn.total_changes, n)
        self.assertEqual(self.store.memory_version(), v)

    def test_the_kill_switch_writes_nothing(self):
        self.filed("Harbor Line")
        x = self.ent("Wave 4", "label")
        self.fact(x, project="Harbor Line")
        with patch.dict(os.environ, {"QUILL_ENTITY_SCOPE": "0"}):
            self.assertFalse(scope.enabled())
            res = scope.run(self.store)
        self.assertFalse(res["enabled"])
        self.assertEqual(self.parent(x), (None, None))

    def test_no_filed_project_means_no_parent(self):
        p = self.ent("Harbor Line", "project")          # a model project
        x = self.ent("Wave 4", "label")
        self.fact(x, project="Harbor Line")
        scope.run(self.store)
        self.assertEqual(self.parent(x), (None, None))
        row = self.plan_row(x)
        self.assertEqual((row["reason"], row["outside"]),
                         ("no_filed_evidence", 1))
        self.assertTrue(p)

    def test_the_graph_job_runs_scope_after_the_rollup(self):
        src = (Path(__file__).resolve().parent.parent / "app"
               / "main.py").read_text(encoding="utf-8")
        job = src.split("def _graph_job(_payload)")[1].split(
            'worker.register("graph", _graph_job)')[0]
        self.assertIn("entity_scope.run()", job)
        self.assertLess(job.index("project_rollup.run()"),
                        job.index("entity_scope.run()"))


# ------------------------------------------------------------ rollup fix --
class RollupHomesNeverSubjectsTests(_Case):
    def shared_facts(self, *eids, n: int = 3) -> None:
        for _ in range(n):
            with self.store._lock:
                cur = self.store._conn.execute(
                    "INSERT INTO facts (kind, text, extracted_at) "
                    "VALUES ('claim', 'x', ?)", (NOW,))
                fid = int(cur.lastrowid)
                self.store._conn.commit()
            for eid in eids:
                self.store.add_relation("fact", fid, "about", "entity", eid,
                                        origin="derived", ts=NOW)

    def test_two_filed_projects_do_not_home_each_other(self):
        a = self.filed("Harbor Line")
        b = self.filed("Dock Seven")
        tool = self.ent("Craneview", "tool")
        self.shared_facts(a, b)
        self.shared_facts(a, tool)
        homes = {r["entity_id"]: r["project_id"]
                 for r in project_rollup.compute(self.store)}
        self.assertNotIn(a, homes)
        self.assertNotIn(b, homes)
        self.assertEqual(homes.get(tool), a)

    def test_without_filing_project_rows_do_not_home_each_other(self):
        a = self.ent("Harbor Line", "project")
        b = self.ent("Dock Seven", "project")
        tool = self.ent("Craneview", "tool")
        self.shared_facts(a, b, tool)
        homes = {r["entity_id"]: r["project_id"]
                 for r in project_rollup.compute(self.store)}
        self.assertNotIn(a, homes)
        self.assertNotIn(b, homes)
        # The tool is split between two homes now that neither home takes a
        # vote as a subject: no dominant home, no edge.
        self.assertNotIn(tool, homes)
        self.shared_facts(a, tool)
        homes = {r["entity_id"]: r["project_id"]
                 for r in project_rollup.compute(self.store)}
        self.assertEqual(homes.get(tool), a)


# ---------------------------------------------------------------- routes --
class ScopeRouteTests(_Case):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        super().setUp()
        import app.api.routes as routes_mod
        p = patch.object(routes_mod.memory, "_ensure_store",
                         return_value=self.store)
        p.start()
        self.addCleanup(p.stop)

    def listed(self) -> dict:
        return {e["name"]: e for e in
                self.client.get("/entities/list").json()["entities"]}

    def test_list_and_detail_show_the_parent_and_the_children(self):
        p = self.filed("Harbor Line")
        x = self.ent("Wave 4", "label")
        y = self.ent("Wave 5", "label")
        for eid in (x, y):
            self.fact(eid, project="Harbor Line")
        scope.run(self.store)
        rows = self.listed()
        self.assertEqual(rows["Wave 4"]["parent"],
                         {"id": p, "name": "Harbor Line", "source": "rule"})
        self.assertEqual(rows["Harbor Line"]["children"], 2)
        self.assertTrue(rows["Harbor Line"]["filed"])
        self.assertFalse(rows["Wave 4"]["filed"])
        self.assertEqual(rows["Wave 4"]["children"], 0)
        self.assertEqual(rows["Harbor Line"]["kind_source"], "filing")
        self.assertIsNone(rows["Harbor Line"]["parent"])
        d = self.client.get(f"/entities/{p}").json()
        self.assertEqual([c["name"] for c in d["children"]],
                         ["Wave 4", "Wave 5"])
        d = self.client.get(f"/entities/{x}").json()
        self.assertEqual(d["parent"]["name"], "Harbor Line")

    def test_the_user_sets_moves_and_removes_a_parent(self):
        p = self.filed("Harbor Line")
        q = self.ent("Dock Seven", "project")        # any project row
        x = self.ent("Wave 4", "label")
        r = self.client.post(f"/entities/{x}/parent", json={"parent_id": p})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["parent"]["id"], r.json()["parent_source"]),
                         (p, "user"))
        r = self.client.post(f"/entities/{x}/parent", json={"parent_id": q})
        self.assertEqual(r.json()["parent"]["id"], q)
        r = self.client.post(f"/entities/{x}/parent", json={"parent_id": None})
        self.assertEqual((r.json()["parent"], r.json()["parent_source"]),
                         (None, "user"))
        # The rule does not put it back.
        self.fact(x, project="Harbor Line")
        scope.run(self.store)
        self.assertEqual(self.parent(x), (None, "user"))

    def test_the_route_refuses_what_cannot_be(self):
        p = self.filed("Harbor Line")
        x = self.ent("Wave 4", "label")
        topic = self.ent("Tidal data", "idea")
        org = self.ent("Tessaly Freight", "org")
        post = self.client.post
        self.assertEqual(post("/entities/424242/parent",
                              json={"parent_id": p}).status_code, 404)
        self.assertEqual(post(f"/entities/{x}/parent",
                              json={"parent_id": 424242}).status_code, 404)
        self.assertEqual(post(f"/entities/{x}/parent",
                              json={"parent_id": topic}).status_code, 400)
        self.assertEqual(post(f"/entities/{org}/parent",
                              json={"parent_id": p}).status_code, 400)
        # A cycle: the project inside its own label.
        self.assertEqual(post(f"/entities/{x}/parent",
                              json={"parent_id": p}).status_code, 200)
        q = self.ent("Dock Seven", "project")
        self.assertEqual(post(f"/entities/{q}/parent",
                              json={"parent_id": p}).status_code, 200)
        self.assertEqual(post(f"/entities/{p}/parent",
                              json={"parent_id": q}).status_code, 409)
        self.assertEqual(self.parent(p), (None, None))


class ScopeTabTests(unittest.TestCase):
    def test_children_nest_under_their_project_as_a_fold(self):
        from app.api import profile_page
        html = profile_page.PROFILE_PAGE
        self.assertIn("function entityTreeHtml(", html)
        self.assertIn('data-efold="', html)
        self.assertIn("kidsSummary(kids)", html)
        # The groups, compact rows and the Rarely seen fold stay; a row
        # folds into it only when all that is nested under it is rare too.
        self.assertIn("['label','Labels']", html)
        self.assertIn("Rarely seen", html)
        self.assertIn("const rareTree=x=>entityRare(x)&&(kidsOf[x.id]||[])"
                      ".every(rareTree);", html)
        self.assertIn("const rare=top.filter(rareTree), "
                      "main=top.filter(x=>!rareTree(x));", html)
        # The detail panel: "Part of <project>" with change and remove.
        self.assertIn('<div class="plabel">Part of</div>', html)
        self.assertIn('data-eact="parent-set"', html)
        self.assertIn('data-eact="parent-remove"', html)
        self.assertIn("'/entities/'+eid+'/parent'", html)
        self.assertNotIn("/entities/candidates", html)


if __name__ == "__main__":
    unittest.main()
