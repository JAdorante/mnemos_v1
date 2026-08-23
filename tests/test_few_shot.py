"""Tests for retrieval-based few-shot correction (Phase 1 of the learning loop).

Three layers:
  * FewShotRecall — index/filter policy: only text-modality rows with a trusted
    human outcome (accepted/edited) and the same task are candidates; the
    similarity floor gates noise; edited text beats the parent's raw output;
    the index rebuilds when the trail file changes.
  * render — the injected prompt block's shape and size cap.
  * ModelRouter integration — examples augment ONLY the local system prompt
    (Claude parent prompt and the stored distill prompt stay clean), the row
    records fewshot_n, and full-fidelity rows store untruncated
    system/messages/output while the legacy flag restores the old caps.

Embeddings are faked (keyword one-hots); no models, no network.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from app.services import few_shot as fs
from app.services import model_router as mr
from app.services.escalate_log import escalate_log


def _fake_embed(texts):
    """Deterministic stand-in: 'alpha'/'beta' keyword one-hots + a tiny shared
    component so unrelated texts land near-zero (but nonzero) similarity."""
    def vec(t):
        v = np.array([1.0 if "alpha" in t else 0.0,
                      1.0 if "beta" in t else 0.0, 0.1], dtype=np.float32)
        return v / np.linalg.norm(v)
    return np.stack([vec(t or "") for t in texts])


def _row(task="chat", outcome="accepted", prompt="alpha question",
         answer="the alpha answer", modality="text", edited=None, full=True):
    row = {
        "id": uuid.uuid4().hex, "time": 1.0, "task": task, "reason": "low_confidence",
        "modality": modality, "user_outcome": outcome,
        "parent": {"text": answer}, "meta": {"prompt_head": prompt[:500]},
    }
    if full:
        row["meta"]["messages"] = [{"role": "user", "text": prompt}]
    if edited:
        row["edited"] = edited
    return row


class _TrailMixin:
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_fewshot_"))
        self.trail = self.tmp / "escalate_distill.jsonl"
        self._embed = mock.patch.object(fs, "_embed_many", side_effect=_fake_embed)
        self._embed.start()
        self._settings = mock.patch.object(
            fs, "settings",
            SimpleNamespace(escalate_log=SimpleNamespace(path=str(self.trail))))
        self._settings.start()
        self.recall = fs.FewShotRecall()

    def tearDown(self) -> None:
        self._embed.stop()
        self._settings.stop()

    def _write(self, rows) -> None:
        self.trail.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8")

    def _examples(self, prompt="alpha question", task="chat", k=3, min_sim=0.4):
        return self.recall.examples(
            task, [{"role": "user", "content": prompt}], k=k, min_sim=min_sim)


class RecallTests(_TrailMixin, unittest.TestCase):
    def test_trusted_same_task_text_rows_only(self) -> None:
        self._write([
            _row(outcome="accepted"),
            _row(outcome="rejected"),
            _row(outcome="unknown"),
            _row(task="extract"),                    # other task
            _row(modality="vision"),                 # other modality
        ])
        ex = self._examples()
        self.assertEqual(len(ex), 1)
        self.assertEqual(ex[0]["answer"], "the alpha answer")
        self.assertEqual(ex[0]["outcome"], "accepted")

    def test_similarity_floor_gates_unrelated(self) -> None:
        self._write([_row(prompt="beta thing", answer="beta answer")])
        self.assertEqual(self._examples(), [])

    def test_ranked_desc_and_capped_at_k(self) -> None:
        self._write([
            _row(prompt="alpha one"),
            _row(prompt="alpha two"),
            _row(prompt="alpha and beta both"),      # mixed → lower sim to pure alpha
        ])
        ex = self._examples(k=2)
        self.assertEqual(len(ex), 2)
        self.assertGreaterEqual(ex[0]["sim"], ex[1]["sim"])

    def test_edited_text_beats_parent_output(self) -> None:
        self._write([_row(outcome="edited", answer="raw parent",
                          edited="human corrected")])
        self.assertEqual(self._examples()[0]["answer"], "human corrected")

    def test_accepted_local_kept_row_teaches_local_text(self) -> None:
        # Buttons-on-everything: a kept local answer has no parent side; a 👍
        # verifies the LOCAL text, which then becomes the taught example.
        row = _row()
        row["reason"] = "local_kept"
        row["parent"] = None
        row["local"] = {"text": "the alpha answer (local)", "confidence": 0.9}
        self._write([row])
        ex = self._examples()
        self.assertEqual(len(ex), 1)
        self.assertEqual(ex[0]["answer"], "the alpha answer (local)")

    def test_unlabeled_local_kept_row_not_in_pool(self) -> None:
        row = _row(outcome="unknown")
        row["reason"] = "local_kept"
        row["parent"] = None
        row["local"] = {"text": "unvouched", "confidence": 0.9}
        self._write([row])
        self.assertEqual(self._examples(), [])

    def test_prompt_head_fallback_for_old_rows(self) -> None:
        self._write([_row(full=False)])
        self.assertEqual(len(self._examples()), 1)

    def test_reindex_when_trail_changes(self) -> None:
        self._write([_row(outcome="unknown")])
        self.assertEqual(self._examples(), [])
        self._write([_row(outcome="unknown"), _row(outcome="accepted")])
        self.assertEqual(len(self._examples()), 1)

    def test_missing_trail_and_zero_k_are_empty(self) -> None:
        self.assertEqual(self._examples(), [])                 # no file
        self._write([_row()])
        self.assertEqual(self._examples(k=0), [])

    def test_query_focus_extracts_the_ask(self) -> None:
        qf = fs.query_focus
        self.assertEqual(qf("Retrieved memories:\n- beta\n\nQuestion: alpha q"),
                         "alpha q")
        self.assertEqual(qf("MEMORIES...\nstuff\n\nUser: alpha q"), "alpha q")
        self.assertEqual(qf("User: alpha q"), "alpha q")       # marker at start
        self.assertEqual(qf("ctx\nCurrent task: alpha q\nline two"),
                         "alpha q\nline two")                  # multiline ask
        self.assertEqual(qf("no markers at all"), "no markers at all")
        self.assertEqual(qf(""), "")

    def test_similarity_follows_question_not_context(self) -> None:
        # Finding #4: same stored row, question about alpha buried under
        # beta-flavored context. A beta-context/beta-question query must NOT
        # match it; an alpha question under different context must.
        self._write([_row(
            prompt="Retrieved memories:\n- beta beta beta\n\nQuestion: alpha q")])
        beta_q = "Retrieved memories:\n- beta beta\n\nQuestion: beta thing"
        self.assertEqual(self._examples(prompt=beta_q), [])
        alpha_q = "Retrieved memories:\n- unrelated\n\nQuestion: alpha thing"
        hits = self._examples(prompt=alpha_q)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["prompt"], "alpha q")   # focused, not full

    def test_refusal_answers_never_become_examples(self) -> None:
        # Correct answer, poisonous example — teaches refusal regardless of
        # context (live failure 2026-07-17).
        self._write([
            _row(answer="I don't have a memory of that."),
            _row(answer="No information about alpha available."),
            _row(prompt="alpha two", answer="the real alpha answer"),
        ])
        ex = self._examples()
        self.assertEqual(len(ex), 1)
        self.assertEqual(ex[0]["answer"], "the real alpha answer")

    def test_exclude_ids_bars_rows_from_retrieval(self) -> None:
        self._write([_row()])
        full = self._examples()
        self.assertEqual(len(full), 1)
        barred = self.recall.examples(
            "chat", [{"role": "user", "content": "alpha question"}],
            k=3, min_sim=0.4, exclude_ids={full[0]["id"]})
        self.assertEqual(barred, [])

    def test_embed_failure_degrades_to_empty(self) -> None:
        self._write([_row()])
        with mock.patch.object(fs, "_embed_many",
                               side_effect=RuntimeError("no torch")):
            self.assertEqual(self._examples(), [])

    def test_render_shape_and_empty(self) -> None:
        block = self.recall.render([{"prompt": "p1", "answer": "a1",
                                     "outcome": "accepted", "sim": 0.9}])
        self.assertIn("VERIFIED EXAMPLES", block)
        self.assertIn("REQUEST: p1", block)
        self.assertIn("VERIFIED ANSWER: a1", block)
        self.assertNotIn("CONFIDENCE:", block)          # schema mode: pure JSON
        self.assertEqual(self.recall.render([]), "")

    def test_render_confidence_line_for_plain_text(self) -> None:
        # Bare examples teach the model to drop its CONFIDENCE trailer (a
        # verified-live regression: right answer, missing conf, escalated).
        ex = [{"prompt": "p1", "answer": "a1", "outcome": "accepted", "sim": 0.9},
              {"prompt": "p2", "answer": "a2", "outcome": "edited", "sim": 0.8}]
        block = self.recall.render(ex, confidence_line=True)
        self.assertEqual(block.count("CONFIDENCE: 0.9"), 2)
        self.assertIn("VERIFIED ANSWER: a1\nCONFIDENCE: 0.9", block)

    def test_render_clips_long_sides(self) -> None:
        block = self.recall.render([{"prompt": "p" * 5000, "answer": "a" * 5000,
                                     "outcome": "accepted", "sim": 0.9}])
        self.assertLess(len(block), 4000)


def _text_cfg(fewshot_k=3, min_conf=0.6, conf_weight=0.85):
    return SimpleNamespace(enabled=True, local_model="fake-local",
                           ollama_url="http://127.0.0.1:1", local_timeout_s=1.0,
                           escalate_min_conf=min_conf, high_stakes_tasks=("plan",),
                           fewshot_k=fewshot_k, fewshot_min_sim=0.4,
                           fewshot_conf_weight=conf_weight)


class _CaptureLocal:
    """Fake OllamaText that records the system prompt it was handed."""

    def __init__(self, res):
        self.model, self.url = "fake-local", "http://127.0.0.1:1"
        self._res = res
        self.system = None
        self.exemplars = ""

    def complete(self, task, *, system, messages, max_tokens=1024,
                 schema=None, exemplars=""):
        self.system = system
        self.exemplars = exemplars
        return dict(self._res)

    @property
    def prompt(self) -> str:
        """What the model actually receives. Phase 1.2 moved the exemplars out
        of `system` into their own argument so the static prefix could come
        first; the guarantee under test — local sees the examples, the parent
        does not — is unchanged, but it now has to be checked on the composed
        prompt rather than on `system` alone."""
        from app.services.ollama_text import _compose_system
        return _compose_system(self.system or "", self.exemplars or "")


class _RouterTrailMixin:
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_fewshot_router_"))
        self.trail = self.tmp / "escalate_distill.jsonl"
        self._orig = (escalate_log._path, escalate_log._counts, escalate_log._total)
        from collections import Counter
        escalate_log._path = self.trail
        escalate_log._counts = Counter()
        escalate_log._total = 0

    def tearDown(self) -> None:
        escalate_log._path, escalate_log._counts, escalate_log._total = self._orig

    def _rows(self):
        if not self.trail.is_file():
            return []
        return [json.loads(ln) for ln in
                self.trail.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _run(self, *, local_res, examples, full_fidelity=True, prompt="q" * 900,
             claude_text="parent answer", consistent=True):
        """`consistent` fakes the answer-agreement check: True embeds the local
        answer and the example answer as identical vectors, False as orthogonal."""
        local = _CaptureLocal(local_res)
        r = mr.ModelRouter()
        r._local, r._local_ok = local, True
        r._complete_claude = mock.Mock(return_value=claude_text)
        vecs = (np.asarray([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)
                if consistent else
                np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32))
        with mock.patch.object(mr, "_text_cfg", return_value=_text_cfg()), \
             mock.patch.object(
                 mr, "settings",
                 SimpleNamespace(escalate_log=SimpleNamespace(
                     full_fidelity=full_fidelity))), \
             mock.patch("app.services.embeddings.embedder") as emb, \
             mock.patch.object(fs.few_shot, "examples", return_value=examples):
            emb.encode_many.return_value = vecs
            out = r.complete("chat", system="s",
                             messages=[{"role": "user", "content": prompt}])
        return out, local, r


class RouterIntegrationTests(_RouterTrailMixin, unittest.TestCase):
    def test_examples_augment_local_prompt_only(self) -> None:
        # sim 0.5: weak enough that the calibrated floor (0.5*0.85=0.425)
        # stays under the 0.6 gate, so this still exercises the escalate path.
        ex = [{"prompt": "alpha", "answer": "verified", "outcome": "accepted",
               "sim": 0.5}]
        local_res = {"text": "meh", "json": None, "confidence": 0.2, "parse_ok": True}
        out, local, r = self._run(local_res=local_res, examples=ex)
        self.assertEqual(out, "parent answer")
        self.assertIn("VERIFIED EXAMPLES", local.prompt)       # local sees them
        self.assertIn("CONFIDENCE: 0.9", local.prompt)         # trailer survives
        self.assertTrue(local.prompt.startswith("s"))
        self.assertEqual(local.system, "s")   # the task prompt stays clean
        # Phase 1.2: the static trailer precedes the per-call exemplars, or the
        # prefix cache can never hit.
        from app.services.ollama_text import _CONF_TRAILER
        self.assertLess(local.prompt.index(_CONF_TRAILER),
                        local.prompt.index("VERIFIED EXAMPLES"))
        _, kwargs = r._complete_claude.call_args
        self.assertEqual(kwargs["system"], "s")                # parent stays clean
        row = self._rows()[0]
        self.assertEqual(row["meta"]["fewshot_n"], 1)
        self.assertEqual(row["meta"]["system"], "s")           # stored input clean
        self.assertEqual(row["meta"]["messages"][0]["text"], "q" * 900)

    def test_strong_match_floors_confidence_and_stays_local(self) -> None:
        # Calibration (#6): a sim-0.9 verified match floors effective
        # confidence at 0.765 — the self-reported 0.2 no longer forces a
        # needless escalation of a good in-distribution answer.
        ex = [{"prompt": "alpha", "answer": "verified", "outcome": "accepted",
               "sim": 0.9}]
        local_res = {"text": "good answer", "json": None, "confidence": 0.2,
                     "parse_ok": True}
        out, local, r = self._run(local_res=local_res, examples=ex)
        self.assertEqual(out, "good answer")
        r._complete_claude.assert_not_called()
        # Kept chat answers now write a labelable local_kept row (buttons on
        # every bubble) — with the calibration evidence stamped for analysis.
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "local_kept")
        self.assertEqual(rows[0]["meta"]["fewshot_top_sim"], 0.9)
        self.assertEqual(rows[0]["meta"]["conf_effective"], 0.765)

    def test_missing_conf_with_strong_match_stays_local(self) -> None:
        # The dropped-trailer case: retrieval evidence alone clears the gate.
        ex = [{"prompt": "alpha", "answer": "verified", "outcome": "accepted",
               "sim": 0.8}]
        local_res = {"text": "good answer", "json": None, "confidence": None,
                     "parse_ok": True}
        out, _, r = self._run(local_res=local_res, examples=ex)
        self.assertEqual(out, "good answer")
        r._complete_claude.assert_not_called()

    def test_inconsistent_answer_gets_no_floor_and_escalates(self) -> None:
        # The failure the first calibration attempt shipped: strong prompt
        # match, but the local answer CONTRADICTS the verified answer — the
        # floor must not apply, and the row escalates as before.
        ex = [{"prompt": "alpha", "answer": "verified", "outcome": "accepted",
               "sim": 0.9}]
        local_res = {"text": "something else entirely", "json": None,
                     "confidence": 0.2, "parse_ok": True}
        out, _, r = self._run(local_res=local_res, examples=ex, consistent=False)
        self.assertEqual(out, "parent answer")
        self.assertEqual(r._complete_claude.call_count, 1)

    def test_refusal_despite_context_escalates_at_any_confidence(self) -> None:
        ctx_prompt = ("KNOWN PERSON: X\n- committed: send link [open]\n"
                      "- mentioned_in: stocks chat\n\nQuestion: what's open?")
        local_res = {"text": "I don't have any relevant memories about that.",
                     "json": None, "confidence": 0.9, "parse_ok": True}
        out, _, r = self._run(local_res=local_res, examples=[],
                              prompt=ctx_prompt)
        self.assertEqual(out, "parent answer")
        self.assertEqual(self._rows()[0]["reason"], "refusal_despite_context")

    def test_refusal_without_context_stays_local_when_confident(self) -> None:
        local_res = {"text": "I don't have any information about that.",
                     "json": None, "confidence": 0.9, "parse_ok": True}
        out, _, r = self._run(local_res=local_res, examples=[],
                              prompt="what is my landlord's name?")
        self.assertEqual(out, "I don't have any information about that.")
        r._complete_claude.assert_not_called()

    def test_echo_answer_escalates(self) -> None:
        local_res = {"text": "You want me to summarize your day in two sentences.",
                     "json": None, "confidence": 0.9, "parse_ok": True}
        out, _, r = self._run(local_res=local_res, examples=[],
                              prompt="Summarize my day in two sentences")
        self.assertEqual(out, "parent answer")
        self.assertEqual(self._rows()[0]["reason"], "echo_answer")

    def test_effective_confidence_policy(self) -> None:
        eff = mr.effective_confidence
        self.assertIsNone(eff(None, [], weight=0.85))              # unchanged gate
        self.assertEqual(eff(0.3, [], weight=0.85), 0.3)           # no evidence
        ex = [{"sim": 0.8}, {"sim": 0.5}]
        self.assertAlmostEqual(eff(0.2, ex, weight=0.85), 0.68)    # floor wins
        self.assertAlmostEqual(eff(0.9, ex, weight=0.85), 0.9)     # self can raise
        self.assertAlmostEqual(eff(None, ex, weight=0.85), 0.68)   # evidence only
        self.assertEqual(eff(0.2, [{"sim": 2.0}], weight=0.85), 0.95)  # capped
        self.assertEqual(eff(0.2, ex, weight=0), 0.2)              # disabled

    def test_no_examples_leaves_system_untouched(self) -> None:
        local_res = {"text": "fine", "json": None, "confidence": 0.9, "parse_ok": True}
        out, local, _ = self._run(local_res=local_res, examples=[])
        self.assertEqual(out, "fine")
        self.assertEqual(local.system, "s")
        rows = self._rows()                    # the kept answer's verdict row
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "local_kept")
        self.assertNotIn("fewshot_n", rows[0]["meta"])

    def test_full_fidelity_stores_untruncated_output(self) -> None:
        long_answer = "x" * 5000
        local_res = {"text": "y" * 5000, "json": None, "confidence": 0.2,
                     "parse_ok": True}
        _, _, _ = self._run(local_res=local_res, examples=[],
                            claude_text=long_answer)
        row = self._rows()[0]
        self.assertEqual(row["parent"]["text"], long_answer)
        self.assertEqual(row["local"]["text"], "y" * 5000)

    def test_legacy_flag_restores_truncation(self) -> None:
        local_res = {"text": "y" * 5000, "json": None, "confidence": 0.2,
                     "parse_ok": True}
        _, _, _ = self._run(local_res=local_res, examples=[], full_fidelity=False,
                            claude_text="x" * 5000)
        row = self._rows()[0]
        self.assertEqual(len(row["parent"]["text"]), 2001)     # 2000 + ellipsis
        self.assertEqual(len(row["local"]["text"]), 2001)
        self.assertNotIn("system", row.get("meta") or {})
        self.assertNotIn("messages", row.get("meta") or {})


class OutcomeEditedTextTests(_RouterTrailMixin, unittest.TestCase):
    """set_user_outcome(edited_text=...) stamps the correction on the row."""

    def test_edited_text_lands_on_row(self) -> None:
        local_res = {"text": "meh", "json": None, "confidence": 0.2, "parse_ok": True}
        self._run(local_res=local_res, examples=[])
        rid = self._rows()[0]["id"]
        ok = escalate_log.set_user_outcome("edited", row_id=rid,
                                           edited_text="the human fix")
        self.assertTrue(ok)
        row = self._rows()[0]
        self.assertEqual(row["user_outcome"], "edited")
        self.assertEqual(row["edited"], "the human fix")

    def test_edited_text_ignored_for_accept(self) -> None:
        local_res = {"text": "meh", "json": None, "confidence": 0.2, "parse_ok": True}
        self._run(local_res=local_res, examples=[])
        rid = self._rows()[0]["id"]
        escalate_log.set_user_outcome("accepted", row_id=rid,
                                      edited_text="should not stick")
        self.assertNotIn("edited", self._rows()[0])


if __name__ == "__main__":
    unittest.main()
