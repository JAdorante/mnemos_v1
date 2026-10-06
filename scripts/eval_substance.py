"""Substance eval — does the local chat model USE what grounding retrieved?

bench_text/bench_bakeoff score a local reply by embedding similarity to the
parent's past answer. That rewards matching the parent's length and phrasing,
which is the wrong target when the question is "are local answers thin?": the
parent answers in the trail were written under the same 'concisely' contract.
This harness instead grades each reply against the memory block it was given:

  1. FACT LIST (once per prompt, cached): a judge enumerates the distinct facts
     in the retrieved context that are relevant to the question. Cached to
     data/bench/substance/facts.json so every (model, prompt) arm is graded
     against the SAME list — arms differ only in the answer.
  2. GRADE (per arm): the judge marks which of those facts the answer used,
     lists personal claims the context does not support, and rates filler.

Replays production chat rows from the escalate trail (task=chat, full-fidelity
prompt, a retrieval block present), deduped by question. Rows are replayed RAW
(no few-shot) so arms compare the model + system prompt and nothing else.

    python scripts/eval_substance.py --label base7b
    python scripts/eval_substance.py --model qwen2.5:14b-instruct --label base14b
    python scripts/eval_substance.py --answer-system /tmp/new.txt --label new7b
    python scripts/eval_substance.py --compare base7b new7b base14b

--answer-system swaps the live ANSWER_SYSTEM for the file's text inside each
stored system prompt (rows whose stored prompt predates the live constant are
skipped for every arm, so all arms share one row set). Outbound judge prompts
pass through redact_text — the same egress hygiene as the escalation path,
and every row replayed here already reached the parent once.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

OUT_DIR = Path("data/bench/substance")
FACTS_CACHE = OUT_DIR / "facts.json"
JUDGE_MODEL = "claude-opus-5-5"

_CONTEXT_MARKERS = ("RELEVANT MEMORIES", "RELEVANT CONTEXT", "Retrieved memories")
_TASK_RE = re.compile(r"Current task:\s*(.+?)\s*$", re.S)

_FACTS_SYSTEM = (
    "You audit a personal memory assistant. You are given the context block it "
    "retrieved and the user's question. List every DISTINCT fact in the context "
    "that a complete, helpful answer to this question should use — people, "
    "relationships, projects, open tasks, commitments, dates, status. One short "
    "line per fact, no duplicates, nothing that is not in the context. If the "
    "message is a statement to remember rather than a question, or nothing in "
    "the context is relevant, return an empty list.")

_GRADE_SYSTEM = (
    "You grade one answer from a personal memory assistant. You get the context "
    "it was given, the user's question, a numbered list of the relevant facts in "
    "that context, and the answer.\n"
    "- used: the numbers of the listed facts the answer conveys (paraphrase "
    "counts; a fact merely implied does not).\n"
    "- unsupported: each PERSONAL claim in the answer (about the user, people, "
    "their work, events) that the context does not support. General world "
    "knowledge is not unsupported. Quote it briefly.\n"
    "- filler: 0 = none; 1 = some padding, restating, generic advice or "
    "unnecessary hedging; 2 = mostly padding.\n"
    "- answers: true if the answer addresses what was actually asked (for a "
    "statement to remember, a brief accurate acknowledgement counts).\n"
    "- substance: 1-5 overall — how much correct, relevant, connected "
    "information the user gets. 5 = uses everything relevant and connects it; "
    "1 = empty, wrong or invented.")

_FACTS_SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {"type": "string"}}},
    "required": ["facts"], "additionalProperties": False}

_GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "used": {"type": "array", "items": {"type": "integer"}},
        "unsupported": {"type": "array", "items": {"type": "string"}},
        "filler": {"type": "integer"},
        "answers": {"type": "boolean"},
        "substance": {"type": "integer"},
    },
    "required": ["used", "unsupported", "filler", "answers", "substance"],
    "additionalProperties": False}


# ----------------------------- dataset --------------------------------------
def user_text(row: dict) -> str:
    msgs = (row.get("meta") or {}).get("messages") or []
    return "\n\n".join(str(m.get("text") or "") for m in msgs
                       if m.get("role", "user") == "user")


def question_of(row: dict) -> str:
    m = _TASK_RE.search(user_text(row))
    return (m.group(1) if m else user_text(row)[-300:]).strip()


def row_key(row: dict) -> str:
    """Stable per-prompt key for the facts cache (the full user turn, so the
    same question asked over different memories is a different row)."""
    return hashlib.sha1(user_text(row).encode("utf-8")).hexdigest()[:16]


def eligible(row: dict, answer_system: str) -> bool:
    meta = row.get("meta") or {}
    return (row.get("task") == "chat"
            and bool(meta.get("system")) and bool(meta.get("messages"))
            and answer_system in str(meta["system"])
            and any(k in user_text(row) for k in _CONTEXT_MARKERS))


def load_rows(path: Path, answer_system: str) -> list[dict]:
    rows, seen = [], set()
    if not path.is_file():
        return rows
    for ln in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            row = json.loads(ln)
        except Exception:
            continue
        if not eligible(row, answer_system):
            continue
        q = question_of(row).lower()
        if q in seen:
            continue
        seen.add(q)
        rows.append(row)
    return rows


# ----------------------------- judge ----------------------------------------
def _judge(system: str, user: str, schema: dict) -> dict:
    import anthropic
    from app.services.redact import redact_text
    resp = anthropic.Anthropic().messages.create(
        model=JUDGE_MODEL, max_tokens=4000, system=system,
        messages=[{"role": "user", "content": redact_text(user)}],
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": schema}})
    if resp.stop_reason == "refusal":
        raise RuntimeError("judge refused")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def facts_for(row: dict, cache: dict) -> list[str]:
    key = row_key(row)
    if key not in cache:
        cache[key] = _judge(_FACTS_SYSTEM, user_text(row), _FACTS_SCHEMA)["facts"]
    return cache[key]


def grade(row: dict, facts: list[str], answer: str) -> dict:
    listed = "\n".join(f"{i}. {f}" for i, f in enumerate(facts, 1)) or "(none)"
    user = (f"CONTEXT AND QUESTION:\n{user_text(row)}\n\n"
            f"RELEVANT FACTS:\n{listed}\n\nANSWER:\n{answer or '(empty)'}")
    g = _judge(_GRADE_SYSTEM, user, _GRADE_SCHEMA)
    g["used"] = sorted({i for i in g["used"] if 1 <= i <= len(facts)})
    return g


# ----------------------------- replay ---------------------------------------
def replay(row: dict, local, answer_system: str, override: str | None) -> dict:
    from bench_text import replay_messages
    system = str(row["meta"]["system"])
    if override is not None:
        system = system.replace(answer_system, override, 1)
    t0 = time.time()
    res = local.complete("chat", system=system, messages=replay_messages(row))
    return {"text": res.get("text") or "", "confidence": res.get("confidence"),
            "latency_s": round(time.time() - t0, 2)}


def summarize(rows: list[dict]) -> dict:
    graded = [r for r in rows if "grade" in r]
    with_facts = [r for r in graded if r["n_facts"]]
    lat = sorted(r["latency_s"] for r in graded) or [0.0]
    return {
        "n": len(graded),
        "fact_coverage": round(statistics.mean(
            len(r["grade"]["used"]) / r["n_facts"] for r in with_facts), 3)
        if with_facts else None,
        "rows_with_unsupported": round(sum(
            1 for r in graded if r["grade"]["unsupported"]) / len(graded), 3)
        if graded else None,
        "filler_mean": round(statistics.mean(
            r["grade"]["filler"] for r in graded), 2) if graded else None,
        "answers_rate": round(sum(
            1 for r in graded if r["grade"]["answers"]) / len(graded), 3)
        if graded else None,
        "substance_mean": round(statistics.mean(
            r["grade"]["substance"] for r in graded), 2) if graded else None,
        "chars_median": statistics.median(len(r["text"]) for r in graded)
        if graded else None,
        "no_confidence_rate": round(sum(
            1 for r in graded if r["confidence"] is None) / len(graded), 3)
        if graded else None,
        "latency_p50_s": lat[len(lat) // 2],
        "latency_p90_s": lat[min(len(lat) - 1, int(len(lat) * 0.9))],
    }


def compare(labels: list[str]) -> None:
    cols = ["n", "fact_coverage", "rows_with_unsupported", "filler_mean",
            "answers_rate", "substance_mean", "chars_median",
            "no_confidence_rate", "latency_p50_s", "latency_p90_s"]
    print("label".ljust(14) + "".join(c[:13].rjust(14) for c in cols))
    for label in labels:
        p = OUT_DIR / f"{label}.jsonl"
        if not p.is_file():
            print(f"{label:<14}(missing)")
            continue
        rows = [json.loads(ln) for ln in p.read_text().splitlines() if ln]
        s = summarize(rows)
        print(label.ljust(14) + "".join(str(s[c]).rjust(14) for c in cols))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default=None, help="Ollama tag (default: live)")
    ap.add_argument("--answer-system", type=Path, default=None,
                    help="file whose text replaces ANSWER_SYSTEM in each prompt")
    ap.add_argument("--label", default=None, help="results name (required to run)")
    ap.add_argument("--rows", type=int, default=0, help="cap rows (smoke runs)")
    ap.add_argument("--compare", nargs="+", default=None,
                    help="print a side-by-side of saved labels and exit")
    args = ap.parse_args()

    if args.compare:
        compare(args.compare)
        return
    if not args.label:
        sys.exit("--label is required to run an arm")

    from app.config import settings
    from app.services.ollama_text import OllamaText
    from browser_agent.llm import ANSWER_SYSTEM

    override = (args.answer_system.read_text(encoding="utf-8").strip()
                if args.answer_system else None)
    rows = load_rows(Path(settings.escalate_log.path), ANSWER_SYSTEM)
    if args.rows:
        rows = rows[:args.rows]
    if not rows:
        sys.exit("no replayable chat rows with a retrieval block in the trail.")

    local = OllamaText(model=args.model)
    if not local.available():
        sys.exit(f"{local.model} is not present at {local.url}")
    print(f"[substance] {args.label}: {len(rows)} rows on {local.model}"
          f"{' (prompt override)' if override else ''}", file=sys.stderr)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cache = (json.loads(FACTS_CACHE.read_text())
             if FACTS_CACHE.is_file() else {})
    out: list[dict] = []
    for i, row in enumerate(rows, 1):
        rec = {"id": row.get("id"), "key": row_key(row),
               "question": question_of(row)[:200]}
        try:
            facts = facts_for(row, cache)
            rec.update(replay(row, local, ANSWER_SYSTEM, override))
            rec["n_facts"] = len(facts)
            rec["grade"] = grade(row, facts, rec["text"])
        except Exception as exc:
            rec["error"] = str(exc)[:200]
            print(f"  [{i}/{len(rows)}] error: {exc}", file=sys.stderr)
        else:
            g = rec["grade"]
            print(f"  [{i}/{len(rows)}] used {len(g['used'])}/{rec['n_facts']} "
                  f"unsupported={len(g['unsupported'])} filler={g['filler']} "
                  f"substance={g['substance']} {rec['latency_s']}s",
                  file=sys.stderr)
        out.append(rec)
        FACTS_CACHE.write_text(json.dumps(cache, indent=1))

    (OUT_DIR / f"{args.label}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out))
    print(json.dumps({"label": args.label, "model": local.model,
                      "prompt_override": bool(override), **summarize(out)},
                     indent=1))


if __name__ == "__main__":
    main()
