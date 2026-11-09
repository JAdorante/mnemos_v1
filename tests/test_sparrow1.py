"""Sparrow 1.0 base-model loop (scripts/sparrow1): the parts that must not
drift — the training-row contract, world validation, grading checks, rollout
isolation and the promotion gate. No network, no Claude, no GPU."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "sparrow1"))

import grade as G  # noqa: E402
import loop as L  # noqa: E402
import pairs as P  # noqa: E402
import rollout as R  # noqa: E402
import world as W  # noqa: E402


def _raw_world() -> dict:
    blank = {"subject": "", "from_key": "", "to_keys": [], "cc_keys": [],
             "thread": "", "channel": "", "text": "", "title": "", "body": "",
             "minutes": 0, "attendee_keys": [], "turns": [], "start_day": 0,
             "start_time": "", "end_time": "", "rescheduled_from": "",
             "project": "", "facts": []}
    return {
        "org": {"name": "Harbor Freightworks", "industry": "logistics", "about": ""},
        "user": {"key": "me", "name": "Rina Okoye", "role": "ops manager"},
        "people": [
            {"key": "dana", "name": "Dana Whitcomb", "role": "dispatch",
             "company": "Harbor Freightworks", "relationship": "teammate"},
            {"key": "ines", "name": "Ines Carvalho", "role": "procurement",
             "company": "Pellwood Foods", "relationship": "external"}],
        "projects": [{"name": "Route Atlas", "summary": "routing", "owner_key": "dana"}],
        "items": [
            {**blank, "key": "e1", "type": "email", "day": -3, "time": "09:10",
             "project": "Route Atlas", "subject": "Atlas pilot",
             "from_key": "dana", "to_keys": ["me"], "thread": "atlas",
             "body": "I will send the driver training deck by Friday.",
             "facts": [{"kind": "commitment", "text": "Dana sends the deck",
                        "owner_key": "dana", "has_due": True, "due_day": 0}]},
            {**blank, "key": "k1", "type": "calendar", "day": -1, "time": "10:00",
             "title": "Atlas review", "attendee_keys": ["dana", "me"],
             "start_day": 2, "start_time": "15:00", "end_time": "15:30"},
            {**blank, "key": "bad", "type": "email", "day": -2, "time": "08:00",
             "from_key": "nobody", "body": "dropped: unknown sender"}],
        "notes": [
            {"key": "n1", "title": "", "text": "Atlas tablets crash.",
             "expected_route": "existing", "expected_project": "Route Atlas"},
            {"key": "n2", "title": "", "text": "Kickoff for Project Lark.",
             "expected_route": "new", "expected_project": "Project Lark"},
            {"key": "n3", "title": "", "text": "No name here.",
             "expected_route": "new", "expected_project": "Ghost"}],
        "scenarios": [
            {"key": "s1", "skill": "commitments", "question": "what will dana send?",
             "answer": "The deck.", "evidence_item_keys": ["e1"],
             "evidence_phrases": ["driver training deck", "not in the item"],
             "answer_person_key": "dana", "not_in_memory": False},
            {"key": "s2", "skill": "find_owner", "question": "who owns X?",
             "answer": "?", "evidence_item_keys": ["e1"],
             "evidence_phrases": ["made up phrase"], "answer_person_key": "",
             "not_in_memory": False},
            {"key": "s3", "skill": "not_in_memory", "question": "fuel budget?",
             "answer": "Not in memory.", "evidence_item_keys": ["e1"],
             "evidence_phrases": ["x"], "answer_person_key": "",
             "not_in_memory": True}],
    }


class WorldTests(unittest.TestCase):
    def test_spec_quota_covers_every_skill_and_sums(self):
        for focus in (None, {"latest_status": 5.0, "ambiguous_name": 0.0}):
            spec = W.world_spec(7, focus=focus, n_scenarios=16)
            self.assertEqual(sum(spec["skill_quota"].values()), 16)
            self.assertTrue(all(n >= 1 for n in spec["skill_quota"].values()))
        heavy = W.world_spec(7, focus={"latest_status": 5.0}, n_scenarios=20)
        self.assertGreater(heavy["skill_quota"]["latest_status"],
                           heavy["skill_quota"]["find_owner"])

    def test_normalise_pins_references_and_drops_ungradeable(self):
        w = W.normalise(_raw_world(), {"seed": 1})
        self.assertEqual([i["key"] for i in w["items"]], ["e1", "k1"])
        # evidence phrases must be verbatim; a scenario with none is dropped
        self.assertEqual([s["key"] for s in w["scenarios"]], ["s1", "s3"])
        self.assertEqual(w["scenarios"][0]["evidence_phrases"],
                         ["driver training deck"])
        # not_in_memory scenarios carry no evidence
        self.assertEqual(w["scenarios"][1]["evidence_item_keys"], [])
        # a "new" route must quote its name in the note
        self.assertEqual([n["key"] for n in w["notes"]], ["n1", "n2"])
        self.assertTrue(w["user"]["email"].endswith("@harborfreightworks.example"))

    def test_expand_parses_transcript_lines_and_calendar(self):
        raw = {"people": [{"key": "dana"}], "items": [
            {"key": "t1", "type": "transcript", "day": -1, "time": "10:00",
             "project": "", "from_key": "", "to_keys": ["dana", "me"],
             "title": "Standup", "thread": "", "minutes": 15,
             "body": "dana: shipping Friday\nme: ok\n  and the docs too\nNot a turn",
             "facts": [{"kind": "task", "text": "ship", "owner_key": "dana",
                        "due": "+2"}]},
            {"key": "k1", "type": "calendar", "day": 3, "time": "15:00",
             "project": "", "from_key": "", "to_keys": ["dana"],
             "title": "Review", "thread": "", "minutes": 45, "body": "",
             "facts": []}]}
        items = W._expand(raw)["items"]
        self.assertEqual([t["speaker_key"] for t in items[0]["turns"]], ["dana", "me"])
        self.assertIn("docs too", items[0]["turns"][1]["text"])
        self.assertEqual(items[0]["facts"][0]["due_day"], 2)
        self.assertTrue(items[0]["facts"][0]["has_due"])
        cal = items[1]
        self.assertEqual((cal["start_day"], cal["start_time"], cal["end_time"]),
                         (3, "15:00", "15:45"))
        self.assertLessEqual(cal["day"], 0)       # captured before it happens

    def test_connector_items_use_the_two_sparrow_mapping(self):
        w = W.normalise(_raw_world(), {"seed": 1})
        out = W.connector_items(w)
        kinds = {k: (cid, it["kind"]) for k, cid, it in out}
        self.assertEqual(kinds["e1"], ("google", "mail"))
        self.assertEqual(kinds["k1"], ("google", "calendar"))
        mail = next(it for k, _, it in out if k == "e1")
        self.assertIn("Dana Whitcomb", mail["from"])
        self.assertFalse(mail["from_self"])


class PairsTests(unittest.TestCase):
    def _plain_call(self):
        return {"id": "c1", "task": "chat", "system": "SYS", "schema": None,
                "injected": False, "messages": [{"role": "user", "content": "q"}],
                "output": {"text": "It is Friday.", "json": None,
                           "confidence": 0.4, "parse_ok": True, "truncated": False}}

    def test_plain_target_matches_the_inference_contract(self):
        from app.services.ollama_text import split_confidence, training_contract
        call = self._plain_call()
        t = P.target_from_correction(call, "Dana sends it Friday.")
        self.assertEqual(t, training_contract("x", "Dana sends it Friday.")[1])
        text, conf = split_confidence(t)
        self.assertEqual((text, conf), ("Dana sends it Friday.", 0.9))
        # student rehearsal keeps the trailer at the target confidence
        self.assertTrue(P.target_from_student(call).endswith("CONFIDENCE: 0.90"))

    def test_json_target_orders_keys_and_appends_injected_confidence(self):
        schema = {"type": "object", "properties": {
            "tasks": {"type": "array", "items": {"type": "object", "properties": {
                "text": {"type": "string"}, "owner": {"type": "string"}}}},
            "claims": {"type": "array"}}, "required": ["tasks", "claims"]}
        call = {"schema": schema, "injected": True}
        t = P.target_from_correction(
            call, '{"claims": [], "tasks": [{"owner": "me", "text": "x"}]}')
        self.assertEqual(list(json.loads(t)), ["tasks", "claims", "confidence"])
        self.assertEqual(list(json.loads(t)["tasks"][0]), ["text", "owner"])
        self.assertEqual(json.loads(t)["confidence"], 0.9)
        # a schema that owns its confidence keeps the model's value
        own = {"type": "object", "properties": {"decision": {}, "confidence": {}}}
        t2 = P.json_target({"schema": own, "injected": False},
                           {"confidence": 0.7, "decision": "misc"})
        self.assertEqual(json.loads(t2), {"decision": "misc", "confidence": 0.7})

    def test_unusable_student_output_gives_no_row(self):
        call = {**self._plain_call(), "schema": {"type": "object"},
                "output": {"parse_ok": False, "json": None, "truncated": False}}
        self.assertIsNone(P.target_from_student(call))
        call2 = {**self._plain_call(), "error": "URLError"}
        self.assertIsNone(P.target_from_student(call2))

    def test_row_source_follows_verdict(self):
        call = self._plain_call()
        ok = P.row_for(call, {"verdict": "correct"}, round_n=1, world_id="w")
        fix = P.row_for(call, {"verdict": "wrong", "corrected": "Fixed."},
                        round_n=1, world_id="w")
        self.assertEqual((ok["source"], fix["source"]), ("student", "teacher"))
        self.assertTrue(fix["target"].startswith("Fixed."))
        self.assertIsNone(P.row_for(call, {"verdict": "error"}, round_n=1,
                                    world_id="w"))
        ex = {**call, "had_exemplars": True}
        self.assertIsNone(P.row_for(ex, {"verdict": "correct"}, round_n=1,
                                    world_id="w"))

    def test_dataset_caps_student_rows_per_task(self):
        rows = ([{"id": f"t{i}", "task": "chat", "source": "teacher"} for i in range(3)]
                + [{"id": f"s{i}", "task": "chat", "source": "student"} for i in range(50)]
                + [{"id": "t0", "task": "chat", "source": "teacher"}])
        with tempfile.TemporaryDirectory() as d:
            st = P.build_dataset(rows, Path(d) / "x.jsonl", student_ratio=1.0,
                                 student_floor=5)
            self.assertEqual(st["per_task"]["chat"], {"teacher": 3, "student": 5})
            self.assertEqual(st["rows"], 8)


class GradeTests(unittest.TestCase):
    def setUp(self):
        self.w = W.normalise(_raw_world(), {"seed": 1})

    def test_route_graded_by_code(self):
        call = {"id": "c", "task": "project_route", "phase": "note", "ref": "n1",
                "output": {"json": {"decision": "misc", "project": ""}}}
        g = G.grade_route(call, self.w)
        self.assertEqual(g["verdict"], "wrong")
        self.assertEqual(g["corrected"]["project"], "Route Atlas")
        call["output"]["json"] = {"decision": "existing", "project": "route atlas"}
        self.assertEqual(G.grade_route(call, self.w)["verdict"], "correct")

    def test_check_correction_validates_against_the_call_schema(self):
        call = {"schema": {"type": "object", "properties": {
            "decision": {"type": "string", "enum": ["a", "b"]}},
            "required": ["decision"]}, "injected": True}
        self.assertTrue(G.check_correction(call, '{"decision": "a", "confidence": 1}')[0])
        self.assertFalse(G.check_correction(call, '{"decision": "c"}')[0])
        self.assertFalse(G.check_correction(call, "not json")[0])
        self.assertTrue(G.check_correction({"schema": None}, "plain")[0])

    def test_key_marks_retrieval_misses(self):
        call = {"task": "chat", "phase": "scenario", "ref": "s1",
                "messages": [{"role": "user", "content": "nothing relevant"}]}
        key, extra = G.key_for(call, self.w, None, date(2026, 11, 8))
        self.assertFalse(extra["evidence_in_prompt"])
        self.assertIn("retrieval probably missed", key)
        call["messages"][0]["content"] = "...the Driver Training Deck..."
        self.assertTrue(G.key_for(call, self.w, None)[1]["evidence_in_prompt"])

    def test_evidence_matches_paraphrased_facts_on_one_line(self):
        prompt = ("OPEN TASKS:\n- Send Kofi the runbook (from: Marcus)\n"
                  "- unrelated: Kofi lunch")
        self.assertTrue(G.evidence_seen("I'll send Kofi the runbook by Wednesday",
                                        prompt))
        self.assertFalse(G.evidence_seen("texture memory budget cut",
                                         prompt))
        # words scattered across lines do not count
        self.assertFalse(G.evidence_seen("Kofi runbook lunch unrelated budget",
                                         "Kofi\nrunbook\nlunch"))

    def test_extract_key_renders_absolute_dates(self):
        call = {"task": "extract", "phase": "item", "ref": "e1", "messages": []}
        key, _ = G.key_for(call, self.w, None, date(2026, 11, 8))
        self.assertIn("2026-11-05", key)          # arrived day -3
        self.assertIn("due Sunday 2026-11-08", key)

    def test_judge_uses_the_teacher_and_retries_a_bad_json_fix(self):
        answers = iter([
            {"verdict": "wrong", "failure_tags": ["wrong_route"], "note": "n",
             "corrected": "{oops"},
            {"verdict": "wrong", "failure_tags": [], "note": "",
             "corrected": '{"decision": "a"}'}])

        class Fake:
            @staticmethod
            def ask_json(*a, **k):
                return next(answers)
        call = {"id": "c", "task": "x", "system": "S", "messages":
                [{"role": "user", "content": "u"}], "schema":
                {"type": "object", "properties": {"decision": {"type": "string"}}},
                "injected": True, "output": {"json": {}}}
        g = G.judge(call, self.w, teacher=Fake)
        self.assertEqual(g["corrected"], {"decision": "a"})

    def test_score_splits_chat_by_evidence(self):
        graded = [
            {"task": "chat", "phase": "scenario", "verdict": "correct",
             "evidence_in_prompt": True, "skill": "a"},
            {"task": "chat", "phase": "scenario", "verdict": "wrong",
             "evidence_in_prompt": False, "skill": "a"},
            {"task": "extract", "verdict": "partial"},
            {"task": "extract", "verdict": "error"}]
        sc = G.score(graded)
        self.assertEqual(sc["chat_with_evidence"]["score"], 1.0)
        self.assertEqual(sc["chat_retrieval_missed"]["n"], 1)
        self.assertEqual(sc["tasks"]["extract"], {"n": 1, "score": 0.5})


class RolloutTests(unittest.TestCase):
    def test_child_env_drops_dev_flags_and_blocks_cloud(self):
        os.environ["QUILL_SHADOW_EVAL_TEST_LEAK"] = "1"
        try:
            env = R.child_env(Path("/tmp/x"), "student:tag")
        finally:
            os.environ.pop("QUILL_SHADOW_EVAL_TEST_LEAK", None)
        self.assertNotIn("QUILL_SHADOW_EVAL_TEST_LEAK", env)
        self.assertEqual(env["QUILL_TEXT_LOCAL_MODEL"], "student:tag")
        self.assertEqual(env["QUILL_TEXT_LOCAL"], "1")
        self.assertEqual(env["QUILL_EXEMPLARS"], "0")
        self.assertTrue(env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1"))
        self.assertTrue(env["QUILL_DATA_DIR"].startswith("/"))

    def test_recorder_captures_the_prompt_as_sent(self):
        from app.services import ollama_text as ot
        before = ot.OllamaText.complete
        rec = R.Recorder()
        rec.install()
        canned = {"message": {"content": '{"decision": "misc", "project": ""}'},
                  "done_reason": "stop"}
        orig_chat = ot.OllamaText._chat
        ot.OllamaText._chat = lambda self, task, msgs, **kw: canned
        try:
            rec.context = {"phase": "note", "ref": "n1"}
            schema = {"type": "object", "properties": {
                "decision": {"type": "string"}, "project": {"type": "string"}}}
            out = ot.OllamaText("m").complete(
                "project_route", system="SYS",
                messages=[{"role": "user", "content": "body"}], schema=schema,
                exemplars="\n\n---\nVERIFIED EXAMPLES: x")
        finally:
            ot.OllamaText._chat = orig_chat
            rec.uninstall()
        self.assertEqual(out["json"]["decision"], "misc")
        c = rec.calls[0]
        self.assertEqual((c["task"], c["phase"], c["ref"]),
                         ("project_route", "note", "n1"))
        self.assertTrue(c["injected"])
        self.assertEqual(c["system"], ot._compose_system(
            "SYS", "", schema=schema, injected=True))
        self.assertNotIn("VERIFIED EXAMPLES", c["system"])
        self.assertTrue(c["had_exemplars"])
        self.assertIs(ot.OllamaText.complete, before)


class LiveLayerBaseTests(unittest.TestCase):
    def test_seat_can_name_the_1_0_weights_by_env(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import train_lora as tl
        prev = os.environ.get("QUILL_LORA_BASE_HF")
        os.environ["QUILL_LORA_BASE_HF"] = "/srv/sparrow-1.0-r2-qwen"
        try:
            self.assertEqual(tl.hf_base_for("sparrow-1.0-r2"),
                             "/srv/sparrow-1.0-r2-qwen")
        finally:
            if prev is None:
                os.environ.pop("QUILL_LORA_BASE_HF", None)
            else:
                os.environ["QUILL_LORA_BASE_HF"] = prev
        self.assertEqual(tl.hf_base_for("qwen2.5:14b-instruct"),
                         "unsloth/Qwen2.5-14B-Instruct")


class GateTests(unittest.TestCase):
    def test_promotion_needs_margin_and_no_task_collapse(self):
        champ = {"overall": 0.60, "tasks": {"chat": {"score": 0.6, "n": 9},
                                            "extract": {"score": 0.6, "n": 9}}}
        up = {"overall": 0.66, "tasks": {"chat": {"score": 0.7, "n": 9},
                                         "extract": {"score": 0.6, "n": 9}}}
        tiny = {**up, "overall": 0.61}
        drop = {"overall": 0.70, "tasks": {"chat": {"score": 0.9, "n": 9},
                                           "extract": {"score": 0.5, "n": 9}}}
        self.assertTrue(L.better(up, champ)[0])
        self.assertFalse(L.better(tiny, champ)[0])
        self.assertFalse(L.better(drop, champ)[0])

    def test_focus_upweights_failed_skills(self):
        w = W.normalise(_raw_world(), {"seed": 1})
        graded = [
            {"task": "chat", "phase": "scenario", "ref": "s1", "world": w["id"],
             "verdict": "wrong", "skill": "commitments", "note": "missed deck"},
            {"task": "chat", "phase": "scenario", "ref": "s3", "world": w["id"],
             "verdict": "correct", "skill": "not_in_memory", "note": ""}]
        focus, notes = L.focus_from(graded, {w["id"]: w})
        self.assertGreater(focus["commitments"], focus["not_in_memory"])
        self.assertTrue(any("what will dana send?" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
