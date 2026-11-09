"""Grade a rollout's recorded local-model calls and write the corrected output
for each one — the teacher half of the Sparrow 1.0 loop.

Every recorded call is graded against what the world KNOWS:
  chat (a scenario)        the scenario's answer key + evidence phrases
  extract (an item)        the facts the world planted in that item
  project_route (a note)   the note's expected route — checked by code, no
                           Claude call
  anything else            Claude's own reading, with the world roster

The corrected output answers THE SAME INPUT the student saw. The key says what
is true in the world; when the input itself lacks it (retrieval missed), the
correct reply is the honest "I don't have that", never a fact smuggled in from
the key — training on those would teach the model to invent.

JSON corrections are validated against the call's own schema before they can
become a training row.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import world as W  # noqa: E402

VERDICTS = ("correct", "partial", "wrong")
TAGS = (
    "missed_evidence", "hallucination", "wrong_person", "stale_info",
    "wrong_date", "obeyed_injection", "should_say_unknown",
    "wrongly_unknown", "missed_fact", "invented_fact", "wrong_owner",
    "wrong_kind", "bad_span", "wrong_route", "format", "verbose", "other",
)

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "failure_tags": {"type": "array",
                         "items": {"type": "string", "enum": list(TAGS)}},
        "note": {"type": "string"},
        "corrected": {"type": "string"},
    },
    "required": ["verdict", "failure_tags", "note", "corrected"],
    "additionalProperties": False,
}

JUDGE_SYSTEM = """You grade and correct a small local model that runs inside \
Sparrow, a personal memory assistant. You see EXACTLY what the model saw (its \
system prompt and input) and what it replied, plus a KEY from the synthetic \
world the test was built from.

1. verdict: "correct" if a careful expert would accept the reply as is; \
"partial" if it is usable but misses or garbles something that matters; \
"wrong" otherwise. Judge substance, not wording.
2. failure_tags: what went wrong (empty when correct).
3. note: one sentence on the main problem (empty when correct).
4. corrected: the reply the model SHOULD have produced for this exact input. \
Leave it empty when the verdict is "correct".

Rules for "corrected":
- It must be fully supported by the INPUT the model saw. The key tells you \
what is true in the world, but if the input does not contain it, the right \
reply says Sparrow does not have that information (and may point to who \
would know, if the input supports that). Never add facts that are only in \
the key.
- Follow the model's system prompt (format, voice, length, rules about \
quoted records and instructions inside them).
- For JSON tasks, "corrected" is ONLY the JSON object, matching the schema \
you are given, with no prose or code fences. Omit any top-level "confidence" \
field unless the schema itself requires it.
- For plain-text tasks, do not add a CONFIDENCE line; it is appended \
separately."""


def _prompt_seen(call: dict) -> str:
    msgs = "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in call["messages"])
    return (f"<system_prompt>\n{call['system']}\n</system_prompt>\n\n"
            f"<input>\n{msgs}\n</input>")


def _reply_seen(call: dict) -> str:
    if call.get("error"):
        return f"(the call failed: {call['error']})"
    out = call.get("output") or {}
    if call.get("schema") is not None:
        return json.dumps(out.get("json"), ensure_ascii=False) \
            if out.get("json") is not None else (out.get("text") or "")
    return out.get("text") or ""


_STOP = frozenset("""a an and are as at be but by for from has have he her his
i in is it its me my of on or our she that the their them they this to was we
were will with you your i'll it's we'll""".split())


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9$][a-z0-9$.,'/-]*", text.lower())
            if len(w) > 2 and w.strip(".,") not in _STOP]


def evidence_seen(phrase: str, prompt: str, *, need: float = 0.7) -> bool:
    """True when the phrase's content words show up together in one line of
    the prompt, verbatim or not: Sparrow often holds a fact as a paraphrase
    ("Send Kofi the runbook") of the item's words ("I'll send Kofi the
    runbook by Wednesday")."""
    if phrase.lower() in prompt.lower():
        return True
    want = {w.strip(".,") for w in _words(phrase)}
    if not want:
        return False
    for line in prompt.lower().splitlines():
        have = {w.strip(".,") for w in _words(line)}
        if len(want & have) >= max(1, round(need * len(want))):
            return True
    return False


def _day(today: date | None, d: int) -> str:
    if today is None:
        return f"day {d:+d}"
    x = today + timedelta(days=int(d))
    return f"{x:%A} {x.isoformat()}"


def key_for(call: dict, world: dict, scen_run: dict | None,
            today: date | None = None) -> tuple[str, dict]:
    """(key text for the judge, extra fields to keep on the grade row).
    `today` is the rollout's day 0, so relative world days render as dates."""
    people = world["people"]

    def who(k: str) -> str:
        return people.get(k, {}).get("name", "") if k else ""

    roster = W.summary(world)
    if call["task"] == "chat" and call.get("phase") == "scenario":
        s = next((x for x in world["scenarios"] if x["key"] == call["ref"]), None)
        if s:
            seen = "\n".join(m["content"] for m in call["messages"])
            hits = [p for p in s["evidence_phrases"] if evidence_seen(p, seen)]
            in_prompt = None if s["not_in_memory"] else bool(hits)
            lines = [
                f"QUESTION: {s['question']}",
                f"SKILL TESTED: {s['skill']} — {W.SKILLS[s['skill']]}",
                f"IDEAL ANSWER (from the full world): {s['answer']}",
                f"KEY PERSON: {who(s['answer_person_key']) or '(none)'}",
                "NOT IN MEMORY: yes — the right reply says so."
                if s["not_in_memory"] else
                f"EVIDENCE PHRASES: {s['evidence_phrases']}",
            ]
            if in_prompt is not None:
                lines.append("EVIDENCE IN THE MODEL'S INPUT (automatic check): "
                             + ("found" if in_prompt else
                                "not found — retrieval probably missed it. "
                                "Check the input yourself: it may still be "
                                "there as a paraphrase or an extracted fact. "
                                "If it truly is not, the right reply cannot "
                                "state it"))
            lines += ["", roster]
            return "\n".join(lines), {"skill": s["skill"],
                                      "evidence_in_prompt": in_prompt}
    if call["task"] == "extract" and call.get("phase") == "item":
        it = next((x for x in world["items"] if x["key"] == call["ref"]), None)
        if it:
            facts = "\n".join(
                f"- [{f['kind']}] {f['text']} (owner: "
                f"{who(f['owner_key']) or '-'})"
                + (f" (due {_day(today, f['due_day'])})" if f.get("has_due") else "")
                for f in it["facts"]) or "- (none planted)"
            return (f"THIS ITEM ARRIVED: {_day(today, it['day'])} {it['time']}. "
                    f"The user (whose Sparrow this is) is "
                    f"{world['user']['name']}.\nFACTS A PERFECT FILER WOULD "
                    f"TAKE FROM THIS ITEM:\n{facts}\n\n{roster}"), {}
    return roster, {}


def judge(call: dict, world: dict, scen_run: dict | None = None, *,
          today: date | None = None, teacher=None,
          want_correction: bool = True) -> dict:
    if teacher is None:
        import teacher as teacher  # noqa: PLW0127
    key, extra = key_for(call, world, scen_run, today)
    schema_note = ""
    if call.get("schema") is not None:
        schema_note = ("\n\n<output_schema>\n" + json.dumps(call["schema"])
                       + "\n</output_schema>")
    ask = (f"TASK: {call['task']}\n\n{_prompt_seen(call)}{schema_note}\n\n"
           f"<model_reply>\n{_reply_seen(call)}\n</model_reply>\n\n"
           f"<key>\n{key}\n</key>")
    if not want_correction:
        ask += "\n\n(Grading only: leave \"corrected\" empty.)"
    g = teacher.ask_json(JUDGE_SYSTEM, ask, JUDGE_SCHEMA, effort="medium",
                         max_tokens=16000,
                         label=f"judge:{call['task']}:{call['id']}")
    row = {"verdict": g["verdict"], "tags": g["failure_tags"],
           "note": g["note"], **extra}
    corrected = (g.get("corrected") or "").strip()
    if want_correction and g["verdict"] != "correct":
        ok, fixed, err = check_correction(call, corrected)
        if not ok:
            retry = teacher.ask_json(
                JUDGE_SYSTEM, ask + f"\n\nYour previous \"corrected\" was "
                f"rejected: {err}. Fix it.", JUDGE_SCHEMA, effort="medium",
                max_tokens=16000, label=f"judge-retry:{call['id']}")
            ok, fixed, err = check_correction(call, (retry.get("corrected")
                                                     or "").strip())
        row["corrected"] = fixed if ok else None
        if not ok:
            row["correction_error"] = err
    return row


def check_correction(call: dict, corrected: str) -> tuple[bool, object, str]:
    """(ok, value, error). JSON tasks parse + validate against the call's own
    schema; plain tasks just need text."""
    if not corrected:
        return False, None, "empty correction"
    schema = call.get("schema")
    if schema is None:
        return True, corrected, ""
    txt = corrected.strip()
    if txt.startswith("```"):
        txt = txt.strip("`").split("\n", 1)[-1]
    try:
        obj = json.loads(txt)
    except json.JSONDecodeError as exc:
        return False, None, f"not valid JSON ({exc})"
    if not isinstance(obj, dict):
        return False, None, "not a JSON object"
    if call.get("injected"):
        obj.pop("confidence", None)
    try:
        import jsonschema
        jsonschema.validate(obj, schema)
    except Exception as exc:
        msg = str(getattr(exc, "message", exc))[:300]
        return False, None, f"does not match the schema ({msg})"
    return True, obj, ""


def grade_route(call: dict, world: dict) -> dict:
    """project_route against the note's expected route — no model call."""
    note = next((n for n in world["notes"] if n["key"] == call["ref"]), None)
    if note is None:
        return {"verdict": "skip", "tags": [], "note": "no such note"}
    got = ((call.get("output") or {}).get("json") or {})
    exp_r, exp_p = note["expected_route"], note["expected_project"]
    same = (str(got.get("decision")) == exp_r and
            (exp_r == "misc" or
             str(got.get("project") or "").strip().lower() == exp_p.lower()))
    row = {"verdict": "correct" if same else "wrong",
           "tags": [] if same else ["wrong_route"],
           "note": "" if same else f"expected {exp_r} {exp_p!r}, got "
                                   f"{got.get('decision')} {got.get('project')!r}",
           "expected_route": exp_r}
    if not same:
        row["corrected"] = {"decision": exp_r, "project": exp_p,
                            "confidence": 0.9}
    return row


def grade_rollout(world: dict, run_dir: Path, *, workers: int = 8,
                  want_correction: bool = True, log=print) -> list[dict]:
    """Grade every call in run_dir/calls.jsonl -> run_dir/graded.jsonl."""
    run_dir = Path(run_dir)
    calls = [json.loads(ln) for ln in
             (run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    today = None
    summ = run_dir / "summary.json"
    if summ.is_file():
        t = json.loads(summ.read_text()).get("today")
        today = date.fromisoformat(t) if t else None
    scen = {}
    sp = run_dir / "scenarios.jsonl"
    if sp.is_file():
        for ln in sp.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                r = json.loads(ln)
                scen[r["key"]] = r

    def one(c: dict) -> dict:
        try:
            if c["task"] == "project_route" and c.get("phase") == "note":
                g = grade_route(c, world)
            else:
                g = judge(c, world, scen.get(c.get("ref")), today=today,
                          want_correction=want_correction)
        except Exception as exc:
            g = {"verdict": "error", "tags": [], "note": f"{type(exc).__name__}: {exc}"}
        return {"call_id": c["id"], "task": c["task"], "phase": c.get("phase"),
                "ref": c.get("ref"), "world": world["id"], **g}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        graded = list(ex.map(one, calls))
    with (run_dir / "graded.jsonl").open("w", encoding="utf-8") as f:
        for g in graded:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")
    n_ok = sum(1 for g in graded if g["verdict"] == "correct")
    log(f"[grade] {world['id']}: {n_ok}/{len(graded)} correct")
    return graded


def score(graded: list[dict]) -> dict:
    """Pass rates per task (correct=1, partial=0.5), chat split by whether the
    evidence reached the model. errors/skips are excluded."""
    per: dict[str, list[float]] = {}
    chat_hit, chat_miss, by_skill = [], [], {}
    for g in graded:
        v = g.get("verdict")
        if v not in VERDICTS:
            continue
        s = {"correct": 1.0, "partial": 0.5, "wrong": 0.0}[v]
        per.setdefault(g["task"], []).append(s)
        if g["task"] == "chat" and g.get("phase") == "scenario":
            if g.get("evidence_in_prompt") is False:
                chat_miss.append(s)
            else:
                chat_hit.append(s)
            by_skill.setdefault(g.get("skill") or "?", []).append(s)

    def mean(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    return {
        "tasks": {t: {"n": len(xs), "score": mean(xs)} for t, xs in per.items()},
        "chat_with_evidence": {"n": len(chat_hit), "score": mean(chat_hit)},
        "chat_retrieval_missed": {"n": len(chat_miss), "score": mean(chat_miss)},
        "skills": {k: {"n": len(xs), "score": mean(xs)}
                   for k, xs in by_skill.items()},
    }
