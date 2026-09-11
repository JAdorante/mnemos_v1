"""Typed peer egress (Phase 1) — provenance on the wire.

What this pins:
  * retrieval returns FACTS with provenance, never rendered strings, and the
    egress whitelist is structural (EGRESS_KINDS) rather than a substring
    blocklist that can only filter shapes it already knows;
  * every claim that crosses names the event it came from and when;
  * prose is rendered FROM the claims, so there is exactly one egress path;
  * a peer's fact/event ids stay peer-scoped on the way in — a remote id must
    never be read later as one of ours;
  * usability is structural ("has a claim"), with the legacy heuristic kept
    only for peers that have not upgraded.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.events import Event, Modality  # noqa: E402
from app.services import peer_channel as pch  # noqa: E402
from app.services import peer_retrieval as pr  # noqa: E402
from app.storage import Store  # noqa: E402

NOW = 1_757_000_000.0
DAY = 86400.0


class TypedEgressBase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._td.name) / "egress.db")
        # No embedder in these tests: the literal tier alone exercises the
        # shape change, and a live index would make them depend on the
        # developer's real LanceDB.
        self._search = mock.patch("app.services.memory.memory.search",
                                  return_value=[])
        self._search.start()

    def tearDown(self) -> None:
        self._search.stop()
        self._td.cleanup()

    def _claim(self, text: str, *, speaker: str = "Andy Karos",
               days_ago: float = 1.0, kind: str = "claim") -> int:
        ts = NOW - days_ago * DAY
        ev = Event(time=ts, modality=Modality.TEXT, raw=text,
                   summary=text[:120], source="audio.whisper",
                   people=[speaker] if speaker else [])
        eid = self.store.insert(ev)
        if kind in ("task", "commitment"):
            return self.store.add_task(text, source_event_id=eid,
                                       confidence=0.9, extracted_at=ts)
        return self.store.add_claim(text, source_event_id=eid,
                                    source_span=text, confidence=0.9,
                                    extracted_at=ts)


class RetrievalShapeTests(TypedEgressBase):
    def test_returns_facts_with_provenance_not_strings(self) -> None:
        self._claim("Boost Run is giving us free compute")
        got = pr.facts_for_topic("compute", store=self.store, now=NOW)
        self.assertEqual(len(got), 1)
        row = got[0]
        for key in ("fact_id", "text", "source_event_id", "source_span",
                    "speaker", "ts", "kind"):
            self.assertIn(key, row)
        self.assertIsInstance(row["fact_id"], int)
        self.assertIsNotNone(row["source_event_id"])
        self.assertEqual(row["speaker"], "Andy Karos")
        self.assertAlmostEqual(row["ts"], NOW - DAY, places=0)

    def test_work_items_are_not_egress_candidates(self) -> None:
        self._claim("Boost Run is giving us free compute")
        self._claim("Send Andy an update on our usage by Friday", kind="task")
        got = pr.facts_for_topic("Andy compute usage", store=self.store,
                                 now=NOW)
        texts = " ".join(c["text"] for c in got)
        self.assertIn("free compute", texts)
        self.assertNotIn("Send Andy an update", texts)

    def test_superseded_facts_do_not_cross(self) -> None:
        """A teammate must not be told the plan we already revised."""
        old = self._claim("The launch is planned for September", days_ago=30)
        new = self._claim("The launch slipped to October", days_ago=1)
        self.store.supersede_fact(old, new, NOW)
        got = pr.facts_for_topic("launch", store=self.store, now=NOW)
        texts = " ".join(c["text"] for c in got)
        self.assertIn("slipped to October", texts)
        self.assertNotIn("planned for September", texts)

    def test_screen_mined_facts_are_not_asserted_to_others(self) -> None:
        """Weak attribution is fine for a board a human prunes; it is not fine
        to assert to another person as our knowledge."""
        ts = NOW - DAY
        ev = Event(time=ts, modality=Modality.TEXT,
                   raw="Acme signed the renewal", summary="x",
                   source="desktop.screen")
        eid = self.store.insert(ev)
        self.store.add_claim("Acme signed the renewal", source_event_id=eid,
                             source_span="Acme signed the renewal",
                             confidence=0.9, extracted_at=ts)
        got = pr.facts_for_topic("Acme renewal", store=self.store, now=NOW)
        self.assertEqual(got, [])

    def test_empty_topic_retrieves_nothing(self) -> None:
        self._claim("Boost Run is giving us free compute")
        self.assertEqual(pr.facts_for_topic("", store=self.store), [])

    def test_as_of_is_the_newest_supporting_claim(self) -> None:
        claims = [{"ts": NOW - 10 * DAY}, {"ts": NOW - DAY}, {"ts": None}]
        self.assertEqual(pr.as_of(claims), NOW - DAY)
        self.assertIsNone(pr.as_of([]))


class QueryExpansionTests(TypedEgressBase):
    """Phase 2.1 — ask in the asker's words, retrieve in the answerer's."""

    def _org(self, name: str, *, aliases=(), speaker: str = "Andy Karos",
             fact: str = "", days_ago: float = 1.0) -> tuple[int, int]:
        from app.services.entity_alias import normalize
        ts = NOW - days_ago * DAY
        eid = self.store.resolve_entity(name, "org", ts=ts)
        for a in aliases:
            self.store.upsert_entity_alias(eid, a, normalize(a), ts=ts,
                                           source="test", confirmed=True)
        fid = self._claim(fact, speaker=speaker, days_ago=days_ago)
        self.store.add_relation("fact", fid, "about", "entity", eid,
                                origin="asserted")
        # Store.insert does not mint people from an event's `people` list; the
        # pipeline does that. Seed the person row the way production would.
        self.store.insert_person(speaker, ts=ts, promotion_state="active")
        pid = self.store.find_person_exact(speaker)
        if pid:
            self.store.add_relation("person", int(pid), "works_at",
                                    "entity", eid, origin="asserted")
            self.store.add_relation("person", int(pid), "mentioned_in",
                                    "fact", fid, origin="asserted")
        return eid, fid

    def test_squashed_alias_matches_a_run_together_name(self) -> None:
        """'Boostrun' is how a teammate types it; 'Boost Run' is what we store."""
        self._org("Boost Run", aliases=["Boostrun"],
                  fact="Andy said the Boost Run GPUs are ready for us")
        got = pr.facts_for_topic("any update from Boostrun?",
                                 store=self.store, now=NOW)
        self.assertTrue(any("GPUs are ready" in c["text"] for c in got))

    def test_expansion_reports_its_anchors(self) -> None:
        eid, _ = self._org("Boost Run", aliases=["Boostrun"],
                           fact="The Boost Run contract is signed")
        exp = pr.expand_topic("what about Boostrun?", self.store)
        self.assertIn(eid, exp["entity_ids"])
        self.assertIn("Boost Run", exp["terms"])
        # Naming the org reaches the people affiliated with it.
        self.assertIn("Andy Karos", exp["terms"])

    def test_expansion_adds_terms_without_replacing_the_askers(self) -> None:
        """Their wording must keep working; ours is added alongside."""
        self._org("Boost Run", aliases=["Boostrun"],
                  fact="Andy said the Boost Run GPUs are ready for us")
        got = pr.facts_for_topic("are the GPUs ready?", store=self.store,
                                 now=NOW)
        self.assertTrue(got)

    def test_unknown_names_expand_to_nothing(self) -> None:
        exp = pr.expand_topic("what about Trillium?", self.store)
        self.assertEqual(exp["terms"], [])
        self.assertEqual(exp["entity_ids"], [])

    def test_squash_ignores_spacing_and_punctuation_only(self) -> None:
        self.assertEqual(pr._squash("Boost Run"), pr._squash("BoostRun"))
        self.assertEqual(pr._squash("nexus-v1"), pr._squash("Nexus V1"))
        self.assertNotEqual(pr._squash("Boost Run"), pr._squash("Boost Runs"))


class GraphEdgeTests(QueryExpansionTests):
    """Phase 2.2 — a fact sharing NO words with the question is still reachable."""

    def test_reaches_a_fact_through_the_person_affiliated_with_the_org(self) -> None:
        self._org("Boost Run", fact="Andy agreed to the terms on Tuesday",
                  speaker="Andy Karos", days_ago=4)
        got = pr.facts_for_topic("where did we land with Boost Run?",
                                 store=self.store, now=NOW)
        texts = " ".join(c["text"] for c in got)
        self.assertIn("agreed to the terms", texts)

    def test_graph_candidates_still_obey_the_egress_whitelist(self) -> None:
        """An edge is not a licence: a linked work item still does not cross."""
        eid = self.store.resolve_entity("Boost Run", "org", ts=NOW)
        tid = self._claim("Send Andy the usage report by Friday", kind="task")
        self.store.add_relation("fact", tid, "about", "entity", eid,
                                origin="asserted")
        got = pr.facts_for_topic("what about Boost Run?", store=self.store,
                                 now=NOW)
        self.assertNotIn("Send Andy the usage report",
                         " ".join(c["text"] for c in got))


class RecencyRankingTests(TypedEgressBase):
    """Phase 2.3 — 'what's the latest' is a different question from 'what is'."""

    def test_status_questions_are_recognised(self) -> None:
        for q in ("what's the latest on the launch?",
                  "any update on the launch?",
                  "where are we on the Series A?",
                  "where did we land with Boost Run?",
                  "how is the migration going?",
                  "current status of the pilot"):
            self.assertTrue(pr.is_status_question(q), q)
        for q in ("what do we know about Boost Run?",
                  "who is the CEO of Boost Run?",
                  "can you send me the deck?"):
            self.assertFalse(pr.is_status_question(q), q)

    def test_superseded_version_is_dropped_not_listed_alongside(self) -> None:
        """Citing both versions is worse than vague prose: the asker cannot
        tell which holds, and citations make it look authoritative."""
        self._claim("The launch is planned for the last week of September",
                    speaker="Dave Randel", days_ago=30)
        self._claim("The launch slipped to the second week of October",
                    speaker="Dave Randel", days_ago=1)
        got = pr.facts_for_topic("any update on the launch?",
                                 store=self.store, now=NOW)
        texts = " ".join(c["text"] for c in got)
        self.assertIn("slipped to the second week of October", texts)
        self.assertNotIn("planned for the last week of September", texts)

    def test_two_different_current_facts_both_survive(self) -> None:
        """Recency ranking must not collapse a project's distinct facts into
        one just because they name the same project."""
        self._claim("The launch date is October 12", days_ago=5)
        self._claim("The launch is blocked on legal review", days_ago=1)
        got = pr.facts_for_topic("what's the latest on the launch?",
                                 store=self.store, now=NOW)
        texts = " ".join(c["text"] for c in got)
        self.assertIn("blocked on legal", texts)
        self.assertIn("October 12", texts)

    def test_a_lookup_question_keeps_both_versions(self) -> None:
        """"What do we know about X" is a history question, not a status one."""
        self._claim("The launch is planned for the last week of September",
                    days_ago=30)
        self._claim("The launch slipped to the second week of October",
                    days_ago=1)
        got = pr.facts_for_topic("what do we know about the launch?",
                                 store=self.store, now=NOW)
        self.assertEqual(len(got), 2)


class RelevanceFloorTests(TypedEgressBase):
    """An embedder returns a nearest neighbour even when nothing is close, so
    top-k with no floor turns 'adjacent memory' into 'asserted answer'. Both
    of these were live leaks found by `make eval-peer-live`."""

    def _hits(self, *scored):
        return [{"fact_id": fid, "raw": txt, "score": s, "modality": "fact:claim"}
                for fid, txt, s in scored]

    def test_nothing_close_enough_is_not_an_answer(self) -> None:
        """0.013 similarity is not 'the answer', it is the only thing in the
        index — 'what did legal say about Trillium?' must not return the
        office coffee machine."""
        kept = pr._above_floor(self._hits((1, "coffee machine broken", 0.013)))
        self.assertEqual(kept, [])

    def test_an_adjacent_private_fact_does_not_ride_along(self) -> None:
        """Real scores from the goldens: open roles 0.67, "Dave's salary is
        220k and he asked us to keep it quiet" 0.17 — above the absolute floor
        and a quarter of the real hit. A colleague's pay must not cross."""
        kept = pr._above_floor(self._hits(
            (1, "We are opening two engineering roles in October", 0.671),
            (2, "Dave's salary is 220k", 0.168)))
        self.assertEqual([h["fact_id"] for h in kept], [1])

    def test_a_genuine_weak_match_still_survives(self) -> None:
        """The floor must not cost real answers: the synonymy case scores only
        0.215, and it is the correct answer."""
        kept = pr._above_floor(self._hits(
            (1, "Andy Karos is giving us free compute", 0.215)))
        self.assertEqual([h["fact_id"] for h in kept], [1])

    def test_two_strong_hits_both_survive(self) -> None:
        kept = pr._above_floor(self._hits(
            (1, "Boost Run gives us compute", 0.62),
            (2, "Andy Karos is CEO of Boost Run", 0.54)))
        self.assertEqual(len(kept), 2)

    def test_unscored_hits_pass_through(self) -> None:
        """A stubbed or substring-only tier has no scores to filter on."""
        hits = [{"fact_id": 1, "raw": "x"}]
        self.assertEqual(pr._above_floor(hits), hits)
        self.assertEqual(pr._above_floor([]), [])
        self.assertEqual(pr._above_floor(None), [])


class SemanticTierTests(TypedEgressBase):
    """The tier silently returned nothing for a whole phase because it looked
    for an `id` key that no `memory.search` payload has."""

    def test_fact_payloads_resolve_by_fact_id(self) -> None:
        fid = self._claim("Boost Run gives us free compute")
        with mock.patch("app.services.memory.memory.search",
                        return_value=[{"fact_id": fid, "raw": "x",
                                       "score": 0.8, "is_fact": True}]):
            rows = pr._semantic_fact_rows("compute", 8, self.store)
        self.assertEqual([r["fact_id"] for r in rows], [fid])

    def test_episode_payloads_resolve_by_timestamp(self) -> None:
        """`Event.to_dict()` drops the row id, so the join is on time."""
        ts = NOW - DAY
        fid = self._claim("Boost Run gives us free compute", days_ago=1.0)
        with mock.patch("app.services.memory.memory.search",
                        return_value=[{"time": ts, "raw": "Boost Run gives us "
                                       "free compute", "score": 0.8}]):
            rows = pr._semantic_fact_rows("compute", 8, self.store)
        self.assertEqual([r["fact_id"] for r in rows], [fid])

    def test_an_episode_that_never_became_a_fact_resolves_to_nothing(self) -> None:
        """Raw captured text is not an egress candidate."""
        ts = NOW - 3 * DAY
        self.store.insert(Event(time=ts, modality=Modality.TEXT,
                                raw="unstructured chatter", summary="x",
                                source="audio.whisper"))
        with mock.patch("app.services.memory.memory.search",
                        return_value=[{"time": ts, "raw": "unstructured "
                                       "chatter", "score": 0.9}]):
            rows = pr._semantic_fact_rows("chatter", 8, self.store)
        self.assertEqual(rows, [])


class NearMissTests(TypedEgressBase):
    def test_related_but_stale_comes_back_dated(self) -> None:
        """The asker's wording misses the exact search, but we clearly hold
        something on the subject — say so, with its date, instead of refusing.
        "migrations" never substring-matches "migration"; the near-miss's
        looser prefix match is what recovers it."""
        self._claim("We scoped the Helio migration at three weeks",
                    days_ago=95)
        out = pch.compose_peer_claims("how are the migrations going?",
                                      store=self.store, now=NOW)
        self.assertTrue(out["near_miss"])
        self.assertTrue(out["claims"])
        self.assertIn("Helio migration", out["prose"])
        self.assertIn("don't have anything on that", out["prose"])
        self.assertIsNotNone(out["as_of"])

    def test_unrelated_memory_is_not_offered_as_a_near_miss(self) -> None:
        """Noise dressed as context is worse than admitting we have nothing."""
        self._claim("The office coffee machine is broken again", days_ago=1)
        out = pch.compose_peer_claims(
            "what did legal say about the Trillium contract?",
            store=self.store, now=NOW)
        self.assertEqual(out["claims"], [])
        self.assertFalse(out["near_miss"])
        self.assertEqual(out["prose"], "")

    def test_a_near_miss_never_mints_facts(self) -> None:
        """It is an honest 'nothing on that', not knowledge about the topic."""
        with mock.patch.object(pch, "_ingest_answer") as ingest, \
             mock.patch.object(pch, "ingest_enabled", return_value=True), \
             mock.patch.object(pch, "_update_sent"), \
             mock.patch.object(pch, "_touch"), \
             mock.patch.object(pch, "_publish_event") as pub:
            pch._record_answer({"name": "Sarah"}, "p1", "a1",
                               "Nothing on that. Closest I have: X",
                               claims=[{"text": "X"}], near_miss=True)
        ingest.assert_not_called()
        self.assertFalse(pub.call_args.args[2]["ingested"])


class ComposerTests(TypedEgressBase):
    def test_prose_is_rendered_from_the_claims(self) -> None:
        self._claim("Boost Run is giving us free compute", speaker="Andy Karos")
        out = pch.compose_peer_claims("compute", store=self.store, now=NOW)
        self.assertTrue(out["claims"])
        # Every claim's text appears in the prose: there is no second path by
        # which prose could contain something the claims do not.
        for c in out["claims"]:
            self.assertIn(c["text"], out["prose"])
        self.assertIn("Andy Karos", out["prose"])

    def test_each_line_carries_its_own_date(self) -> None:
        self._claim("Compute is free through November", days_ago=1)
        out = pch.compose_peer_claims("compute", store=self.store, now=NOW)
        self.assertIn("yesterday", out["prose"])

    def test_askers_context_brief_is_not_echoed_back(self) -> None:
        """Their brief is background; answering with it would invent agreement."""
        self._claim("Boost Run is giving us free compute")
        q = ("what about compute?\n\n(Context from my side — background only):\n"
             "- We think Vertex is sponsoring the GPUs")
        out = pch.compose_peer_claims(q, store=self.store, now=NOW)
        self.assertNotIn("Vertex", out["prose"])

    def test_compose_answer_reports_no_memory_without_inventing(self) -> None:
        out = pch.compose_answer("what do you know about Trillium?")
        self.assertEqual(out["claims"], [])
        self.assertIsNone(out["as_of"])
        self.assertIn("don't have anything", out["text"].lower())


class WirePayloadTests(unittest.TestCase):
    """Inbound claims are a peer's data, bounded and re-scoped at the border."""

    def test_peer_ids_are_renamed_so_they_cannot_pass_as_ours(self) -> None:
        got = pch._sanitize_claims([
            {"text": "Compute is free", "fact_id": 42,
             "source_event_id": 7, "speaker": "Andy", "ts": NOW}])
        self.assertEqual(got[0]["peer_fact_id"], 42)
        self.assertEqual(got[0]["peer_event_id"], 7)
        self.assertNotIn("fact_id", got[0])
        self.assertNotIn("source_event_id", got[0])

    def test_absent_claims_and_empty_claims_are_different(self) -> None:
        """None = an older peer that sent no structure. [] = an upgraded peer
        saying it has nothing typed. Only the first may fall back."""
        self.assertIsNone(pch._sanitize_claims(None))
        self.assertEqual(pch._sanitize_claims([]), [])

    def test_payload_is_bounded(self) -> None:
        many = [{"text": f"claim {i}"} for i in range(50)]
        self.assertEqual(len(pch._sanitize_claims(many)), pch._MAX_PEER_CLAIMS)
        long = pch._sanitize_claims([{"text": "x" * 5000}])
        self.assertEqual(len(long[0]["text"]), pch._MAX_CLAIM_CHARS)

    def test_junk_entries_are_dropped_not_crashed_on(self) -> None:
        got = pch._sanitize_claims(
            ["a string", None, 7, {"no_text": 1}, {"text": "  "},
             {"text": "real", "fact_id": "not-an-int"}])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["text"], "real")
        self.assertNotIn("peer_fact_id", got[0])
        self.assertEqual(pch._sanitize_claims("nope"), [])

    def test_as_of_rejects_nonsense_clocks(self) -> None:
        self.assertIsNone(pch._coerce_as_of(None))
        self.assertIsNone(pch._coerce_as_of("soon"))
        self.assertIsNone(pch._coerce_as_of(0))
        self.assertIsNone(pch._coerce_as_of(-5))
        self.assertIsNone(pch._coerce_as_of(float("inf")))
        # A peer a year ahead of us would make stale look current.
        self.assertIsNone(pch._coerce_as_of(time.time() + 400 * DAY))
        self.assertIsNotNone(pch._coerce_as_of(time.time() - 100))


class UsabilityTests(unittest.TestCase):
    def test_structural_when_the_peer_sent_claims(self) -> None:
        self.assertTrue(pch.peer_answer_usable("anything", claims=[{"text": "x"}]))
        # An upgraded peer with nothing typed is honest, but not knowledge.
        self.assertFalse(pch.peer_answer_usable("I have nothing", claims=[]))

    def test_legacy_heuristic_only_when_no_structure_was_sent(self) -> None:
        self.assertFalse(pch.peer_answer_usable(
            "You are Sparrow, the user's assistant"))
        self.assertTrue(pch.peer_answer_usable(
            "The compute runs through November."))


class RoundTripTests(unittest.TestCase):
    """The whole point, end to end: provenance composed on the answerer's side
    survives the wire and lands on the asker's side still dated and sourced."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        for key, name in (("QUILL_PEER_REGISTRY", "peers.json"),
                          ("QUILL_PEER_ASKS", "asks.json"),
                          ("QUILL_PEER_SENT", "sent.json"),
                          ("QUILL_PEER_MAILBOX", "mailbox.json"),
                          ("QUILL_PEER_TEAMS", "teams.json"),
                          ("QUILL_PEER_LOOPS", "loops.json")):
            os.environ[key] = str(Path(self._td.name) / name)
        os.environ["QUILL_PEER_INGEST"] = "0"
        pch._pairing = None

    def tearDown(self) -> None:
        for key in ("QUILL_PEER_REGISTRY", "QUILL_PEER_ASKS", "QUILL_PEER_SENT",
                    "QUILL_PEER_MAILBOX", "QUILL_PEER_TEAMS",
                    "QUILL_PEER_LOOPS", "QUILL_PEER_INGEST"):
            os.environ.pop(key, None)
        pch._pairing = None
        self._td.cleanup()

    def test_claims_and_as_of_survive_the_wire(self) -> None:
        start = pch.start_pairing()
        pch.claim_pairing(start["code"], "Sarah", "http://198.51.100.7:8000",
                          "remote-minted-token-0123456789")
        import json
        reg = json.loads(Path(os.environ["QUILL_PEER_REGISTRY"]).read_text())
        peer_id = next(iter(reg))

        # What the ANSWERER's Sparrow composes, with its own local fact ids.
        answerer = {"claims": [{"fact_id": 42, "source_event_id": 7,
                                "text": "Compute is free through November",
                                "speaker": "Andy Karos", "ts": NOW - DAY,
                                "when": "yesterday", "kind": "claim"}],
                    "as_of": NOW - DAY,
                    "prose": "- Compute is free through November",
                    "near_miss": False, "redacted": []}

        def fake_post(rec, path, payload):
            self.assertEqual(path, "/peer/ask")
            out = dict(answerer)
            return {"ok": True, "status": "answered",
                    "answer": out["prose"], "claims": out["claims"],
                    "as_of": out["as_of"]}

        with mock.patch.object(pch, "_post_peer", side_effect=fake_post), \
             mock.patch.object(pch, "enrich_peer_question", side_effect=lambda q: q):
            res = pch.ask(peer_id, "what's the compute situation?")

        self.assertEqual(res["status"], "answered")
        self.assertEqual(res["as_of"], NOW - DAY)
        # Their ids arrived peer-scoped, so nothing downstream can read 42 as
        # one of OUR fact ids.
        self.assertEqual(res["claims"][0]["peer_fact_id"], 42)
        self.assertEqual(res["claims"][0]["peer_event_id"], 7)

        # And it persisted, so the asker can date the answer later.
        sent = json.loads(Path(os.environ["QUILL_PEER_SENT"]).read_text())
        row = next(s for s in sent if s["ask_id"] == res["ask_id"])
        self.assertEqual(row["as_of"], NOW - DAY)
        self.assertEqual(row["claims"][0]["speaker"], "Andy Karos")

        # find_peer_answers inherits the dates with no further work (1.4).
        found = pch.find_peer_answers(peer_id, "compute")
        self.assertTrue(found)
        self.assertEqual(found[0]["as_of"], NOW - DAY)
        self.assertTrue(found[0]["usable"])

    def test_an_older_peer_without_claims_still_works(self) -> None:
        start = pch.start_pairing()
        pch.claim_pairing(start["code"], "Sarah", "http://198.51.100.7:8000",
                          "remote-minted-token-0123456789")
        import json
        reg = json.loads(Path(os.environ["QUILL_PEER_REGISTRY"]).read_text())
        peer_id = next(iter(reg))
        with mock.patch.object(
                pch, "_post_peer",
                return_value={"ok": True, "status": "answered",
                              "answer": "The compute runs through November."}), \
             mock.patch.object(pch, "enrich_peer_question", side_effect=lambda q: q):
            res = pch.ask(peer_id, "what's the compute situation?")
        self.assertEqual(res["status"], "answered")
        self.assertEqual(res["claims"], [])
        self.assertIsNone(res["as_of"])
        # No structure sent => the legacy heuristic decides, and this is fine.
        self.assertTrue(pch.find_peer_answers(peer_id, "compute")[0]["usable"])


class DeadCodeTests(unittest.TestCase):
    """Phase 1 acceptance: no substring filtering left on the egress path."""

    def test_the_old_blocklist_composer_is_gone(self) -> None:
        for name in ("_peer_memory_lines", "_compose_peer_memory_text",
                     "compose_peer_answer", "_PEER_SKIP_SOURCE_LABELS",
                     "_WORK_ITEM_RE"):
            self.assertFalse(hasattr(pch, name),
                             f"{name} survived the typed-egress cutover")

    def test_the_surviving_stripper_is_ingress_only(self) -> None:
        """_strip_peer_update_leaks stays for answers from peers on an older
        build. Nothing on the way OUT may call it."""
        import inspect
        src = inspect.getsource(pch)
        egress = src.split("def compose_peer_claims", 1)[1]
        egress = egress.split("def handle_ask", 1)[0]
        self.assertNotIn("_strip_peer_update_leaks", egress)


if __name__ == "__main__":
    unittest.main()
