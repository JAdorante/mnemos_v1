"""End-to-end demo: the learning loop is CLOSED (global definition of done).

On a fresh fixture DB (nothing touches your real data/), walks every stage:

  1. VERDICT     a human edit on an extracted task → canonical LearningPair
  2. EXEMPLAR    the confirmed pair is embedded into the exemplar store
  3. IMPROVED    the next same-type inference retrieves it — the rendered
                 local prompt carries this morning's correction
  4. SHADOW      a kept (non-escalated) local output is re-graded by a mock
                 Claude; the disagreement lands as an unconfirmed pair
  5. ROUTER      the accumulated labels train the escalation router; a
                 thorny input lands in the escalate band, a simple one stays
                 local

    python scripts/demo_learning_loop.py

No network, no Ollama, no Anthropic key: the grader and embedder are stubbed
(deterministic bag-of-words vectors), exactly like the test suites.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np


def fake_vec(text: str) -> np.ndarray:
    v = np.zeros(64, dtype=np.float32)
    for w in str(text).lower().split():
        w = w.strip("?.,!:;")
        if w:
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
    n = float(np.linalg.norm(v)) or 1.0
    return v / n


def fake_embed(texts):
    return [fake_vec(t) for t in texts]


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mnemos_demo_"))
    print(f"fixture dir: {tmp}\n")
    os.environ.update({
        "QUILL_LEARNING": "1",
        "QUILL_EXEMPLARS": "1",
        "QUILL_SHADOW_EVAL": "1",
        "QUILL_ROUTER": "shadow",
        "QUILL_EXEMPLAR_GATES_PATH": str(tmp / "gates.json"),
        "QUILL_EXEMPLAR_USES_PATH": str(tmp / "uses.jsonl"),
        "QUILL_SHADOW_LOCAL_OUTPUTS_PATH": str(tmp / "local_outputs.jsonl"),
        "QUILL_SHADOW_STATE_PATH": str(tmp / "shadow_state.json"),
        "QUILL_SHADOW_REPORT_PATH": str(tmp / "shadow_report.json"),
        "QUILL_SHADOW_GRADES_PATH": str(tmp / "shadow_grades.jsonl"),
        "QUILL_ROUTER_DIR": str(tmp / "router"),
    })

    from app.services import exemplar_store as xs
    from app.services import learning_store as ls
    from app.services import router_train as rt
    from app.services import shadow_eval as se
    from app.storage import Store

    store = Store(db_path=tmp / "demo.db", audio_dir=tmp / "audio")
    xstore = xs.ExemplarStore(path=str(tmp / "lance"))

    with patch.object(xs, "_embed", fake_embed), \
         patch.object(xs, "exemplar_store", xstore):

        # ---- 1. VERDICT ---------------------------------------------------
        print("1. VERDICT — the user edits a mis-extracted task")
        fact = {"id": 1, "kind": "task",
                "text": "send the deck to sarah",
                "source_span": "justin said send the quarterly deck to sarah "
                               "kane by friday"}
        pid = ls.record_fact_verdict(
            fact, "edited", edited_text="Send the Q3 deck to Sarah Kane "
                                        "by Friday", store=store)
        pair = store.get_learning_pair(pid)
        print(f"   learning_pairs += 1  (id {pid[:8]}…, "
              f"task_type={pair['task_type']}, verdict={pair['verdict']}, "
              f"source_refs={pair['source_refs']})")

        # ---- 2. EXEMPLAR --------------------------------------------------
        print("\n2. EXEMPLAR — the edit is retrievable knowledge, instantly")
        rows = xstore.list_rows()
        print(f"   exemplars += 1  (tier={rows[0]['quality_tier']}, "
              f"linked pair {rows[0]['learning_pair_id'][:8]}…)")

        # ---- 3. IMPROVED EXTRACTION --------------------------------------
        print("\n3. IMPROVED — the next similar input retrieves the correction")
        ex = xstore.examples(("extraction.task",),
                             "send the quarterly deck to sarah kane")
        from app.services.few_shot import few_shot
        block = few_shot.render(ex)
        print(f"   retrieved {len(ex)} exemplar (sim={ex[0]['sim']}) — "
              "rendered into the LOCAL prompt:")
        print("   | " + block.strip().splitlines()[-2].strip())
        print("   | " + block.strip().splitlines()[-1].strip())

        # ---- 4. SHADOW DISAGREEMENT --------------------------------------
        print("\n4. SHADOW — a confident-but-wrong kept answer gets caught")
        se.log_local_output(
            "chat",
            messages=[{"role": "user",
                       "content": "when is my meeting with sarah"}],
            text="Your meeting is on Tuesday.", confidence=0.95,
            model_tag="qwen2.5:7b-instruct")

        def mock_grader(system, user, *, model, max_tokens):
            return (json.dumps({
                "verdict": "major_disagree",
                "corrected_output": "Your meeting with Sarah is Wednesday "
                                    "at 2pm.",
                "reason_code": "wrong_content"}), 500, 90)

        out = se.run_nightly(call=mock_grader, store=store)
        sp = store.list_learning_pairs(verdict="shadow_disagree")[0]
        print(f"   graded {out['graded']} kept output → "
              f"{out['verdicts']} ({out['tokens_spent']} tokens)")
        print(f"   learning_pairs += 1  (human_confirmed="
              f"{sp['human_confirmed']} — awaits the Learning-tab confirm)")

        # ---- 5. ROUTER ----------------------------------------------------
        print("\n5. ROUTER — the labels train the escalation router")
        for i in range(30):
            ls.record(task_type="escalation.text",
                      input_text=f"simple lookup question {i} about lunch",
                      final_target=f"the answer to lunch question {i}",
                      verdict="accepted", verdict_source="chat.outcome",
                      source_refs={"task": "chat"}, store=store)
            ls.record(task_type="escalation.text",
                      input_text=f"thorny multi hop reasoning puzzle {i} "
                                 "requiring deep analysis",
                      local_output="a wrong guess",
                      final_target=f"the carefully reasoned answer {i}",
                      verdict="edited", verdict_source="chat.outcome",
                      source_refs={"task": "chat"}, store=store)
        dataset = rt.build_dataset(store=store)
        model, metrics = rt.train(dataset, embed=fake_embed)
        rt.save(model, metrics, n_labels=len(dataset))
        print(f"   trained on {len(dataset)} labels — holdout {metrics}")

        orig_featurize = rt.featurize
        with patch.object(rt, "featurize",
                          lambda r, embed=None: orig_featurize(
                              r, embed=fake_embed)):
            from app.services.escalation_router import EscalationRouter
            router = EscalationRouter()
            for q, conf in (("thorny multi hop reasoning puzzle requiring "
                             "deep analysis", 0.4),
                            ("simple lookup question about lunch", 0.9)):
                p = router.predict("chat", q, conf)
                print(f"   p(fail)={p:.2f} band={router.band(p):<16} ← "
                      f"{q[:48]}")

    print("\nLoop closed: verdict → pair → exemplar → improved prompt → "
          "shadow label → trained router.")
    print(f"(fixture data stayed under {tmp})")


if __name__ == "__main__":
    main()
