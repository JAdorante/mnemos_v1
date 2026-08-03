"""Unified ranking pipeline — golden snapshots + property tests (WS1).

Changing weights requires updating golden JSON in the same PR so ranking
diffs are reviewable instead of silent drift.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.fixtures.ranking_corpus import CORPUS_BUILDERS, CORPUS_NOW

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ranking_corpus"
GOLDEN_DIR = FIXTURE_DIR / "goldens"

# Set True once to regenerate goldens, then commit and flip back to False.
UPDATE_GOLDENS = False


def _snapshot_from_result(result, *, scorer_name: str) -> dict:
    focus_ids = [n["id"] for n in result.focus]
    breakdowns = {}
    for nid, bd in result.breakdowns.items():
        if nid not in focus_ids:
            continue
        breakdowns[nid] = bd.to_dict()
    return {
        "scorer": scorer_name,
        "focus_ids": focus_ids,
        "focus_order": focus_ids,
        "admitted_by": {
            n["id"]: n.get("admitted_by", "score") for n in result.focus
        },
        "breakdowns": breakdowns,
    }


def _run_corpus(name: str, *, field_v2: bool, db_path: Path):
    from app.services import ranking
    from app.services.ranking.scorer import FieldV2Scorer, GravityScorer
    from app.services.ranking.types import PipelineContext
    from app.services.graph import constellation

    store = CORPUS_BUILDERS[name](db_path)
    try:
        with mock.patch("app.services.graph._field_v2_enabled",
                        return_value=field_v2), \
             mock.patch("app.services.ranking.scorer.get_scorer",
                        side_effect=lambda field_v2=None: (
                            FieldV2Scorer() if field_v2 else GravityScorer())):
            # Drive through constellation so candidate features match prod.
            # Freeze time for stable ages.
            with mock.patch("time.time", return_value=CORPUS_NOW):
                data = constellation(store, limit=28, record_impressions=False)
        focus = [n for n in data["nodes"] if n["layer"] == "focus"]
        # Re-run pipeline on the same store via constellation internals is
        # enough for membership; for breakdowns, call pipeline after a
        # second constellation would double-persist WM. Instead extract
        # from selection metadata + rebuild breakdowns via a direct score
        # of focus-adjacent candidates.
        scorer_name = "field_v2" if field_v2 else "gravity"
        # Pull breakdowns by re-scoring candidates from a lightweight path:
        # use ranking.run on nodes constellation just ranked — but those
        # are cleaned. So score via constellation's selection path result
        # stored on selection.scorer, and reconstruct breakdowns from a
        # dedicated helper below.
        from app.services.ranking.pipeline import run as rank_run
        from app.services.ranking.scorer import get_scorer

        # Build candidates the same way constellation does by calling it
        # with persist_wm via ensure we have full node dicts: monkey-patch
        # is heavy; instead re-invoke run after extracting features through
        # a private helper — simplest: call constellation and then score
        # the focus ids' gravity from data (breakdowns regenerated).
        #
        # Practical approach: instrument via ranking.run by rebuilding
        # feature-rich candidates from store through constellation's
        # candidate path — duplicate call with a test hook.
        cands = _candidates_for_store(store, now=CORPUS_NOW)
        scorer = FieldV2Scorer() if field_v2 else GravityScorer()
        ctx = PipelineContext(
            store=store, now=CORPUS_NOW, focus_k=10,
            persist_wm=False, mode=None,
        )
        # Disable mode to keep goldens mode-stable.
        result = rank_run(cands, ctx=ctx, scorer=scorer, persist_wm=False)
        snap = _snapshot_from_result(result, scorer_name=scorer_name)
        snap["constellation_focus"] = [n["id"] for n in focus]
        return snap, store, result
    finally:
        pass  # caller closes store


def _candidates_for_store(store, *, now: float) -> list[dict]:
    """Mirror constellation feature assembly for golden scoring."""
    from app.services.graph import (
        _age_days,
        _due_days,
        _parse_constellation_id,
        _real_entities,
        _real_people,
        _resolve_person,
        _short_constellation_label,
        entity_constellation_kind,
        temporal_salience,
    )

    pinned = store.user_pinned_nodes()
    node_hidden = store.constellation_hidden_nodes()
    people = [p for p in _real_people(store)
              if ("person", p["id"]) not in node_hidden]
    entities = [e for e in _real_entities(store)
                if ("entity", e["id"]) not in node_hidden]
    open_facts = [
        f for f in store.list_facts(status="open", limit=120, actionable=True)
        if f.get("kind") in ("task", "commitment")
    ]
    degree: dict[str, float] = {}
    rel_strength: dict[str, float] = {}
    for p in people:
        rel = store.relations_of("person", p["id"])
        src = f"person:{p['id']}"
        for e in (rel.get("out") or []) + (rel.get("in") or []):
            w = float(e.get("weight") or 1)
            degree[src] = degree.get(src, 0) + w
            if e["predicate"] == "co_occurs" and e.get("obj_type") == "person":
                rel_strength[src] = rel_strength.get(src, 0) + w

    resolve_cache: dict = {}
    unresolved: dict[str, int] = {}
    for f in open_facts:
        for role_key in ("owner", "from_person", "to_person"):
            name = (f.get(role_key) or "").strip()
            person = _resolve_person(store, name, resolve_cache) if name else None
            if person:
                unresolved[f"person:{person['id']}"] = (
                    unresolved.get(f"person:{person['id']}", 0) + 1)

    cands: list[dict] = []

    def _base(nid, label, kind, *, ts, confidence=0.7, due=None):
        age = _age_days(ts, now)
        return {
            "id": nid,
            "label": _short_constellation_label(label, kind=kind),
            "kind": kind,
            "recency": temporal_salience(age),
            "ts": ts,
            "confidence": max(0.05, min(1.0, float(confidence or 0.5))),
            "due": due,
            "meta": {},
            "_age": age,
            "why": [],
            "layer": "archive",
        }

    for p in people:
        nid = f"person:{p['id']}"
        n = _base(nid, p["name"], "person", ts=p.get("last_seen"),
                  confidence=0.9)
        parsed = _parse_constellation_id(nid)
        n["pinned"] = bool(parsed and parsed in pinned)
        cands.append(n)
    for e in entities:
        kind = entity_constellation_kind(e.get("kind"))
        nid = f"entity:{e['id']}"
        n = _base(nid, e["name"], kind, ts=e.get("last_seen"),
                  confidence=0.75)
        parsed = _parse_constellation_id(nid)
        n["pinned"] = bool(parsed and parsed in pinned)
        cands.append(n)
    for f in open_facts:
        if ("fact", int(f["fact_id"])) in node_hidden:
            continue
        nid = f"fact:{f['fact_id']}"
        fkind = "task" if f.get("kind") == "task" else "commitment"
        n = _base(
            nid, f.get("text") or "item", fkind,
            ts=f.get("extracted_at") or f.get("source_time"),
            confidence=float(f.get("confidence") or 0.5),
            due=f.get("due"),
        )
        parsed = _parse_constellation_id(nid)
        n["pinned"] = bool(parsed and parsed in pinned)
        cands.append(n)

    for n in cands:
        nid = n["id"]
        conf = n["confidence"]
        age = n["_age"]
        is_open = n["kind"] in ("commitment", "task")
        pros = 0.0
        if is_open:
            dd = _due_days(n.get("due"), now)
            if dd is None:
                pros = 0.45 + (1.0 - conf) * 0.15
            elif dd < 0:
                pros = min(1.0, 0.75 + min(14.0, -dd) * 0.04)
            elif dd < 2:
                pros = 0.85
            elif dd < 7:
                pros = 0.55
            else:
                pros = 0.25
        elif n["kind"] == "person":
            pros = min(1.0, unresolved.get(nid, 0) * 0.28)
        rel = min(1.0, (rel_strength.get(nid, 0) ** 0.5) / 4.0)
        fut = 0.0
        if is_open:
            dd = _due_days(n.get("due"), now)
            if dd is not None and 0 <= dd <= 14:
                fut = max(fut, 1.0 - dd / 14.0)
        unres = min(1.0, unresolved.get(nid, 0) / 4.0) if n["kind"] == "person" else (
            0.7 if is_open else 0.0)
        cent = min(1.0, (degree.get(nid, 0) ** 0.5) / 5.0)
        sem = {"person": 0.55, "project": 0.40, "tool": 0.32}.get(
            n["kind"], 0.35 if is_open else 0.20)
        rep = min(1.0, degree.get(nid, 0) / 12.0)
        temp = temporal_salience(age)
        n["_feat_pros"] = pros
        n["_feat_rel"] = rel
        n["_feat_fut"] = fut
        n["_feat_unres"] = unres
        n["_feat_cent"] = cent
        n["_feat_sem"] = sem
        n["_feat_rep"] = rep
        n["_feat_temp"] = temp
        n["_feat_act"] = 0.0
        n["prospective_risk"] = round(pros, 3)
        n["relationship_strength"] = round(rel, 3)
    return cands


class GoldenRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    def _golden_path(self, corpus: str, scorer: str) -> Path:
        return GOLDEN_DIR / f"{corpus}__{scorer}.json"

    def _assert_or_update(self, corpus: str, field_v2: bool):
        scorer = "field_v2" if field_v2 else "gravity"
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / f"{corpus}.db"
            store = CORPUS_BUILDERS[corpus](db)
            try:
                from app.services.ranking.pipeline import run as rank_run
                from app.services.ranking.scorer import (
                    FieldV2Scorer, GravityScorer)

                cands = _candidates_for_store(store, now=CORPUS_NOW)
                sc = FieldV2Scorer() if field_v2 else GravityScorer()
                from app.services.ranking.types import PipelineContext
                ctx = PipelineContext(
                    store=store, now=CORPUS_NOW, focus_k=10,
                    persist_wm=False, mode=None, wm_enabled=True,
                )
                # Force WM on for goldens regardless of env.
                with mock.patch(
                        "app.services.working_memory._wm_enabled",
                        return_value=True):
                    result = rank_run(
                        cands, ctx=ctx, scorer=sc, persist_wm=False)
                snap = _snapshot_from_result(result, scorer_name=scorer)
            finally:
                store.close()

        path = self._golden_path(corpus, scorer)
        if UPDATE_GOLDENS or not path.exists():
            path.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
            if UPDATE_GOLDENS:
                return
        expected = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            snap["focus_ids"], expected["focus_ids"],
            f"{corpus}/{scorer} focus_ids drifted — update golden deliberately")
        self.assertEqual(snap["admitted_by"], expected.get("admitted_by", {}))
        # Breakdown totals must match for every focus node.
        for nid, bd in snap["breakdowns"].items():
            exp = expected["breakdowns"].get(nid)
            self.assertIsNotNone(exp, f"missing golden breakdown for {nid}")
            self.assertAlmostEqual(
                bd["total"], exp["total"], places=4,
                msg=f"{nid} total drifted")

    def test_golden_small_gravity(self):
        self._assert_or_update("small", False)

    def test_golden_small_field_v2(self):
        self._assert_or_update("small", True)

    def test_golden_all_tasks_gravity(self):
        self._assert_or_update("all_tasks", False)

    def test_golden_all_tasks_field_v2(self):
        self._assert_or_update("all_tasks", True)

    def test_golden_one_cluster_gravity(self):
        self._assert_or_update("one_cluster", False)

    def test_golden_heavy_pins_gravity(self):
        self._assert_or_update("heavy_pins", False)

    def test_golden_medium_gravity(self):
        self._assert_or_update("medium", False)


class RankingPropertyTests(unittest.TestCase):
    def test_breakdown_sums_to_total(self):
        from app.services.ranking.config import BREAKDOWN_SUM_EPS
        from app.services.ranking.pipeline import run as rank_run
        from app.services.ranking.scorer import GravityScorer, FieldV2Scorer
        from app.services.ranking.types import PipelineContext

        with tempfile.TemporaryDirectory() as td:
            store = CORPUS_BUILDERS["small"](Path(td) / "t.db")
            try:
                cands = _candidates_for_store(store, now=CORPUS_NOW)
                for Sc in (GravityScorer, FieldV2Scorer):
                    ctx = PipelineContext(
                        store=store, now=CORPUS_NOW, focus_k=10,
                        persist_wm=False, mode=None)
                    with mock.patch(
                            "app.services.working_memory._wm_enabled",
                            return_value=True):
                        result = rank_run(
                            cands, ctx=ctx, scorer=Sc(), persist_wm=False)
                    for nid, bd in result.breakdowns.items():
                        s = sum(c.value for c in bd.components)
                        self.assertAlmostEqual(
                            s, bd.total, delta=BREAKDOWN_SUM_EPS,
                            msg=f"{Sc.__name__} {nid}: {s} != {bd.total}")
            finally:
                store.close()

    def test_pins_always_in_focus(self):
        from app.services.graph import constellation

        with tempfile.TemporaryDirectory() as td:
            store = CORPUS_BUILDERS["heavy_pins"](Path(td) / "t.db")
            try:
                with mock.patch("time.time", return_value=CORPUS_NOW), \
                     mock.patch("app.services.working_memory._wm_enabled",
                                return_value=True):
                    data = constellation(store, limit=28,
                                         record_impressions=False)
                focus_ids = {n["id"] for n in data["nodes"]
                             if n["layer"] == "focus"}
                pinned = store.user_pinned_nodes()
                for kind, iid in pinned:
                    self.assertIn(f"{kind}:{iid}", focus_ids)
            finally:
                store.close()

    def test_hidden_never_in_field(self):
        from app.services.graph import constellation

        with tempfile.TemporaryDirectory() as td:
            store = CORPUS_BUILDERS["small"](Path(td) / "t.db")
            try:
                people = store.all_people()
                self.assertTrue(people)
                pid = int(people[0]["id"])
                store.set_constellation_hidden("person", pid, True)
                with mock.patch("time.time", return_value=CORPUS_NOW):
                    data = constellation(store, limit=28,
                                         record_impressions=False)
                ids = {n["id"] for n in data["nodes"]}
                self.assertNotIn(f"person:{pid}", ids)
            finally:
                store.close()

    def test_hysteresis_churn_bound(self):
        from app.services.ranking.config import FOCUS_CHURN_K
        from app.services.ranking.pipeline import run as rank_run
        from app.services.ranking.scorer import GravityScorer
        from app.services.ranking.types import PipelineContext

        with tempfile.TemporaryDirectory() as td:
            store = CORPUS_BUILDERS["small"](Path(td) / "t.db")
            try:
                cands = _candidates_for_store(store, now=CORPUS_NOW)
                ctx = PipelineContext(
                    store=store, now=CORPUS_NOW, focus_k=10,
                    persist_wm=True, mode=None)
                with mock.patch(
                        "app.services.working_memory._wm_enabled",
                        return_value=True):
                    r1 = rank_run(
                        cands, ctx=ctx, scorer=GravityScorer(),
                        persist_wm=True)
                    ids1 = {n["id"] for n in r1.focus}
                    # Low-impact: tiny gravity nudge on a periphery-ish node.
                    cands2 = [dict(n) for n in r1.ranked]
                    for n in cands2:
                        if n["id"] not in ids1:
                            n["gravity"] = float(n.get("gravity") or 0) + 0.01
                            n["_feat_pros"] = float(
                                n.get("_feat_pros") or 0) + 0.02
                            break
                    ctx2 = PipelineContext(
                        store=store, now=CORPUS_NOW + 30, focus_k=10,
                        persist_wm=True, mode=None)
                    r2 = rank_run(
                        cands2, ctx=ctx2, scorer=GravityScorer(),
                        persist_wm=True)
                    ids2 = {n["id"] for n in r2.focus}
                churn = len(ids1.symmetric_difference(ids2)) // 1
                # Membership delta counted as slots that changed.
                left = len(ids1 - ids2)
                entered = len(ids2 - ids1)
                self.assertLessEqual(
                    max(left, entered), FOCUS_CHURN_K,
                    f"churn left={left} entered={entered} > K={FOCUS_CHURN_K}")
            finally:
                store.close()

    def test_field_v2_flag_only_swaps_scorer(self):
        from app.services.ranking.pipeline import run as rank_run
        from app.services.ranking.scorer import get_scorer
        from app.services.ranking.types import PipelineContext

        g = get_scorer(field_v2=False)
        v = get_scorer(field_v2=True)
        self.assertEqual(g.name, "gravity")
        self.assertEqual(v.name, "field_v2")

        with tempfile.TemporaryDirectory() as td:
            store = CORPUS_BUILDERS["small"](Path(td) / "t.db")
            try:
                cands = _candidates_for_store(store, now=CORPUS_NOW)
                ctx = PipelineContext(
                    store=store, now=CORPUS_NOW, focus_k=10,
                    persist_wm=False, mode=None)
                with mock.patch(
                        "app.services.working_memory._wm_enabled",
                        return_value=True):
                    r_g = rank_run(
                        cands, ctx=ctx, scorer=g, persist_wm=False)
                    r_v = rank_run(
                        [dict(n) for n in cands], ctx=ctx, scorer=v,
                        persist_wm=False)
                self.assertEqual(r_g.selection["path"], "pipeline")
                self.assertEqual(r_v.selection["path"], "pipeline")
                self.assertEqual(r_g.selection["scorer"], "gravity")
                self.assertEqual(r_v.selection["scorer"], "field_v2")
            finally:
                store.close()

    def test_admitter_quota_under_task_flood(self):
        from app.services.graph import constellation, _ENTITY_FOCUS_KINDS

        with tempfile.TemporaryDirectory() as td:
            store = CORPUS_BUILDERS["all_tasks"](Path(td) / "t.db")
            try:
                with mock.patch("time.time", return_value=CORPUS_NOW), \
                     mock.patch("app.services.working_memory._wm_enabled",
                                return_value=True):
                    data = constellation(store, limit=24,
                                         record_impressions=False)
                self.assertEqual(data["selection"]["path"], "pipeline")
                focus = [n for n in data["nodes"] if n["layer"] == "focus"]
                people = [n for n in focus if n["kind"] == "person"]
                entities = [n for n in focus
                            if n["kind"] in _ENTITY_FOCUS_KINDS]
                self.assertGreaterEqual(len(people), 2)
                self.assertGreaterEqual(len(entities), 3)
            finally:
                store.close()

    def test_wm_off_still_pipeline_with_admitter(self):
        from app.services.graph import constellation

        with tempfile.TemporaryDirectory() as td:
            store = CORPUS_BUILDERS["all_tasks"](Path(td) / "t.db")
            try:
                with mock.patch("time.time", return_value=CORPUS_NOW), \
                     mock.patch("app.services.working_memory._wm_enabled",
                                return_value=False):
                    data = constellation(store, limit=24,
                                         record_impressions=False)
                sel = data["selection"]
                self.assertEqual(sel["path"], "pipeline")
                self.assertFalse(sel.get("wm"))
                focus = [n for n in data["nodes"] if n["layer"] == "focus"]
                self.assertGreaterEqual(
                    sum(1 for n in focus if n["kind"] == "person"), 2)
            finally:
                store.close()


class ConstellationPipelineWiringTests(unittest.TestCase):
    def test_selection_path_is_pipeline(self):
        from app.services.graph import constellation
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Ada")
                store.resolve_person("Bea")
                store.add_task("Something open", confidence=0.9,
                               extracted_at=time.time())
                data = constellation(store, limit=20,
                                     record_impressions=False)
                sel = data.get("selection") or {}
                self.assertEqual(sel.get("path"), "pipeline")
                self.assertIn(sel.get("scorer"), ("gravity", "field_v2"))
            finally:
                store.close()

    def test_selector_failure_still_pipeline(self):
        from app.services.graph import constellation
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Ada")
                store.add_task("Something open", confidence=0.9,
                               extracted_at=time.time())
                with mock.patch(
                        "app.services.working_memory.select_focus",
                        side_effect=RuntimeError("boom")):
                    data = constellation(store, limit=20,
                                         record_impressions=False)
                sel = data.get("selection") or {}
                self.assertEqual(sel.get("path"), "pipeline")
                self.assertTrue(sel.get("fallback"))
                self.assertGreater(
                    sum(1 for n in data["nodes"] if n["layer"] == "focus"), 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
