"""Synthetic LoRA pairs — parent-distilled, grounded in THIS install's memory.

Bootstraps a thin training set (the 100-pair green light) without waiting
months for organic verdicts: generate questions a user like this one would
ask, ground each through the SAME retrieval path real chat uses
(grounding.compose), and take the target from the PARENT model — exactly the
semantics of a real escalation pair, minus the human verdict. The output is
therefore quarantined:

  * written to its own file (default data/lora/synthetic.jsonl), NEVER into
    the learning-pairs store — the human-confirmed trail stays honest;
  * merged into TRAIN only by distill_curate --synthetic; never into holdout,
    so the promotion gate still judges on real human-verified rows;
  * capped relative to real pairs at curate time (default 3x) so synthetic
    volume can't drown organic signal as it accumulates.

Style/topic seeds come from the install's own confirmed pairs and memory
graph; a fresh profile with no organic pair yet templates from the live chat
surface's own contract (browser_agent.llm.ANSWER_SYSTEM) instead. Nothing
user-specific lives in code. Outbound prompts pass through redact_text —
same egress hygiene as the live escalation path.

Runs automatically: the idle trainer calls generate_pairs() once a profile
has enough memory to ground questions in (see idle_trainer.synth_bootstrap).

Usage (manual):
    python scripts/synthetic_pairs.py --n 45              # generate
    python scripts/synthetic_pairs.py --n 45 --dry-run    # questions only
    python scripts/distill_curate.py --synthetic data/lora/synthetic.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

OUT_DEFAULT = ROOT / "data" / "lora" / "synthetic.jsonl"
_CURRENT_TASK_RE = re.compile(r"\n\nCurrent task: (.+)\s*$", re.S)

# Must match agent_bridge._make_memory_provider's grounding header.
_FALLBACK_HEADER = (
    "RELEVANT MEMORIES FROM Sparrow (things you have already seen or "
    "heard — use them to complete the task without asking the user to "
    "repeat context; ignore any that aren't relevant):")

_QGEN_SYSTEM = (
    "You write realistic test questions for a personal AI memory assistant. "
    "You are given a summary of the people, projects, and facts in the "
    "user's memory, and (when available) real questions its user actually "
    "asked. Write NEW questions in the same voice and register as the real "
    "ones — some short and casual, some multi-part. Cover a mix: people and "
    "relationships, project/tool details, open tasks and commitments, "
    "cross-entity reasoning ('who should I ask about X'), identity checks "
    "('who are you', 'what do you know about me'), and 2-3 questions about "
    "things NOT in the summary (to exercise the honest 'I don't have a "
    "memory of that' behavior). Refer ONLY to people/projects named in the "
    "summary (except those deliberate misses). Return a JSON array of "
    "strings, nothing else."
)


def _pick_template(chat_rows: list[dict]) -> tuple[str, str]:
    """(system, message_header): from the newest full-fidelity organic chat
    pair when one exists — byte-faithful to the surface that produced it —
    else from the live surface's own contract (fresh profile, zero pairs)."""
    for r in sorted(chat_rows, key=lambda x: x.get("time") or 0, reverse=True):
        meta = r.get("meta") or {}
        msgs = meta.get("messages") or []
        if not (meta.get("system") and msgs):
            continue
        text = str(msgs[0].get("text") or "")
        header = text.split("\n", 1)[0]
        if header.startswith("RELEVANT MEMORIES FROM"):
            return str(meta["system"]), header
    from browser_agent.llm import ANSWER_SYSTEM
    system = ANSWER_SYSTEM
    try:
        from app.services.clock import clock_instruction
        system = system + "\n\n" + clock_instruction()
    except Exception:
        pass
    return system, _FALLBACK_HEADER


def _real_questions(chat_rows: list[dict], limit: int = 20) -> list[str]:
    out = []
    for r in chat_rows:
        msgs = (r.get("meta") or {}).get("messages") or []
        if not msgs:
            continue
        m = _CURRENT_TASK_RE.search(str(msgs[0].get("text") or ""))
        if m:
            out.append(m.group(1).strip()[:200])
    return out[:limit]


def world_summary(store, *, max_facts: int = 40) -> str:
    """Compact people/entities/facts digest for the question generator."""
    lines = ["PEOPLE:"]
    for p in store.all_people():
        if p.get("hide_from_people") or p.get("canonical_person_id"):
            continue
        lines.append(f"- {p['name']} ({p.get('promotion_state')})")
    lines.append("PROJECTS / ORGS / TOOLS:")
    for e in store.all_entities():
        lines.append(f"- {e['name']} ({e.get('kind') or '?'})")
    lines.append("SAMPLE FACTS:")
    n = 0
    for kind in ("task", "commitment", "claim"):
        for f in store.list_facts(kind=kind, limit=max_facts):
            if n >= max_facts:
                break
            t = str(f.get("text") or "").strip()
            if t:
                lines.append(f"- [{kind}] {t[:160]}")
                n += 1
    return "\n".join(lines)


def _claude(model: str, system: str, user: str, max_tokens: int) -> str:
    import anthropic
    resp = anthropic.Anthropic().messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def generate_questions(model: str, summary: str, examples: list[str],
                       n: int) -> list[str]:
    parts = []
    if examples:
        parts.append("Real questions the user asked:\n"
                     + "\n".join(f"- {q}" for q in examples))
    parts.append(f"Memory summary:\n{summary}")
    parts.append(f"Write {n} new questions (JSON array of strings).")
    from app.services.redact import redact_text
    raw = _claude(model, _QGEN_SYSTEM, redact_text("\n\n".join(parts)), 4000)
    start, end = raw.find("["), raw.rfind("]")
    qs = json.loads(raw[start:end + 1])
    return [str(q).strip() for q in qs if str(q).strip()][:n]


def build_row(question: str, *, system: str, header: str,
              target: str, block: str) -> dict:
    message = (f"{header}\n{block or '(no relevant memories found)'}"
               f"\n\nCurrent task: {question}")
    rid = "synth-" + hashlib.sha1(question.encode("utf-8")).hexdigest()[:10]
    return {
        "id": rid, "time": time.time(), "task": "chat",
        "reason": "synthetic_distill", "modality": "text",
        "user_outcome": "synthetic", "synthetic": True,
        "local": {"text": ""}, "parent": {"text": target},
        "meta": {"system": system,
                 "messages": [{"role": "user", "text": message}]},
    }


def generate_pairs(*, n: int = 45, out: Path = OUT_DEFAULT,
                   model: str | None = None, append: bool = True,
                   log=print) -> dict:
    """Generate up to `n` parent-distilled pairs into `out`. Returns
    {"generated": int, "total": int}. Raises on a hard failure (no API key,
    question generation failed) so the caller can back off and retry."""
    model = model or os.environ.get("QUILL_SYNTH_MODEL", "claude-sonnet-4-6")
    import distill_curate as dc
    from app.config import settings
    from app.services.redact import redact_text
    from app.storage import get_store

    rows, source = dc.load_training_rows(settings)
    chats = [r for r in rows if r.get("task") == "chat"]
    system, header = _pick_template(chats)
    examples = _real_questions(chats)
    log(f"[synth] templating from {len(chats)} organic chat pairs "
        f"({source}); {len(examples)} style examples")

    store = get_store()
    summary = world_summary(store)
    questions = generate_questions(model, summary, examples, n)
    log(f"[synth] {len(questions)} questions generated")

    seen: set[str] = set()
    existing: list[dict] = []
    if append and out.is_file():
        for ln in out.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
                existing.append(r)
                seen.add(r.get("id", ""))
            except Exception:
                continue

    from app.services.grounding import compose
    made: list[dict] = []
    for i, q in enumerate(questions, 1):
        rid = "synth-" + hashlib.sha1(q.encode("utf-8")).hexdigest()[:10]
        if rid in seen:
            continue
        try:
            block = compose(q, semantic_limit=8)["block"] or ""
        except Exception as exc:
            log(f"[synth] grounding failed for q{i} ({exc}); skipped.")
            continue
        message = (f"{header}\n{block or '(no relevant memories found)'}"
                   f"\n\nCurrent task: {q}")
        try:
            target = _claude(model, redact_text(system),
                             redact_text(message), 500)
        except Exception as exc:
            log(f"[synth] parent call failed for q{i} ({exc}); skipped.")
            continue
        if not target:
            continue
        made.append(build_row(q, system=system, header=header,
                              target=target, block=block))
        seen.add(rid)
        log(f"[synth] {i}/{len(questions)}: {q[:70]}")

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in existing + made:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    total = len(existing) + len(made)
    log(f"[synth] wrote {total} rows -> {out}")
    return {"generated": len(made), "total": total}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=45, help="questions to generate")
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--model",
                    default=os.environ.get("QUILL_SYNTH_MODEL",
                                           "claude-sonnet-4-6"),
                    help="parent model for questions AND targets")
    ap.add_argument("--dry-run", action="store_true",
                    help="print generated questions, write nothing")
    ap.add_argument("--append", action="store_true",
                    help="add to an existing file (dedupes by question hash)")
    args = ap.parse_args()

    if args.dry_run:
        import distill_curate as dc
        from app.config import settings
        from app.storage import get_store
        rows, source = dc.load_training_rows(settings)
        chats = [r for r in rows if r.get("task") == "chat"]
        examples = _real_questions(chats)
        print(f"[synth] templating from {len(chats)} organic chat pairs "
              f"({source}); {len(examples)} style examples")
        qs = generate_questions(args.model, world_summary(get_store()),
                                examples, args.n)
        print(f"[synth] {len(qs)} questions generated")
        for q in qs:
            print("  -", q)
        return

    generate_pairs(n=args.n, out=args.out, model=args.model,
                   append=args.append)
    print("[synth] next: python scripts/distill_curate.py --synthetic "
          f"{args.out}  (or set QUILL_LORA_SYNTHETIC for train_lora)")


if __name__ == "__main__":
    main()
