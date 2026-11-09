"""Training rows for Sparrow 1.0 — recorded call + target, in the exact
inference contract of app/services/ollama_text.py.

  system   the system prompt the call sent (already carries the confidence
           trailer _compose_system adds), never the few-shot block
  messages the call's messages, flattened as sent
  target   plain task -> answer + "\\n\\nCONFIDENCE: 0.90"  (training_contract)
           JSON task  -> the object, keys in schema order (Ollama's format
                         grammar emits them in that order), with a top-level
                         "confidence" when the router injected one

A wrong or partial call contributes the teacher's correction. A correct call
contributes the student's own output (on-policy rehearsal: it keeps what the
model already does right from drifting), capped per task relative to the
corrections so the set stays weighted toward what needs fixing.

Rows are the shape scripts/lora_train_wsl.py reads (system/messages/target);
the extra fields are bookkeeping it ignores.
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

TARGET_CONF = 0.90


def _conf_line(conf: float = TARGET_CONF) -> str:
    return f"\n\nCONFIDENCE: {max(0.0, min(1.0, conf)):.2f}"


def order_like_schema(obj, schema: dict | None):
    """Reorder dict keys to the schema's property order, recursively; keys the
    schema does not name keep their order after the named ones."""
    if not isinstance(schema, dict):
        return obj
    if isinstance(obj, dict):
        props = schema.get("properties") or {}
        out = {k: order_like_schema(obj[k], props[k]) for k in props if k in obj}
        out.update({k: v for k, v in obj.items() if k not in out})
        return out
    if isinstance(obj, list):
        return [order_like_schema(x, schema.get("items")) for x in obj]
    return obj


def _emit_schema(call: dict) -> dict | None:
    """The schema Ollama actually constrained this call with."""
    s = call.get("schema")
    if s is None or not call.get("injected"):
        return s
    s = json.loads(json.dumps(s))
    s.setdefault("properties", {})["confidence"] = {"type": "number"}
    return s


def json_target(call: dict, obj: dict) -> str:
    obj = dict(obj)
    schema = call.get("schema") or {}
    if call.get("injected") or "confidence" in (schema.get("properties") or {}):
        c = obj.get("confidence")
        obj["confidence"] = (TARGET_CONF if call.get("injected")
                             or not isinstance(c, (int, float)) else c)
    return json.dumps(order_like_schema(obj, _emit_schema(call)),
                      ensure_ascii=False)


def target_from_correction(call: dict, corrected) -> str | None:
    if corrected is None or corrected == "":
        return None
    if call.get("schema") is None:
        return str(corrected).rstrip() + _conf_line()
    if isinstance(corrected, str):
        try:
            corrected = json.loads(corrected)
        except json.JSONDecodeError:
            return None
    return json_target(call, corrected) if isinstance(corrected, dict) else None


def target_from_student(call: dict) -> str | None:
    out = call.get("output") or {}
    if call.get("error") or out.get("truncated"):
        return None
    if call.get("schema") is None:
        text = (out.get("text") or "").rstrip()
        return text + _conf_line() if text else None
    if not out.get("parse_ok") or not isinstance(out.get("json"), dict):
        return None
    return json_target(call, out["json"])


def row_for(call: dict, grade: dict, *, round_n: int, world_id: str) -> dict | None:
    v = grade.get("verdict")
    if v == "correct":
        target, source = target_from_student(call), "student"
    elif v in ("partial", "wrong"):
        target, source = target_from_correction(call, grade.get("corrected")), "teacher"
    else:
        return None
    if not target or call.get("had_exemplars"):
        return None
    rid = hashlib.sha1(json.dumps([call["system"], call["messages"], target],
                                  ensure_ascii=False).encode()).hexdigest()[:16]
    return {"id": f"s1-{rid}", "task": call["task"], "round": round_n,
            "world": world_id, "source": source, "verdict": v,
            "tags": grade.get("tags") or [],
            "system": call["system"], "messages": call["messages"],
            "target": target}


def rows_for_run(run_dir: Path, *, round_n: int, world_id: str) -> list[dict]:
    run_dir = Path(run_dir)
    calls = {c["id"]: c for c in map(json.loads, (run_dir / "calls.jsonl")
                                     .read_text(encoding="utf-8").splitlines())}
    rows = []
    for ln in (run_dir / "graded.jsonl").read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        g = json.loads(ln)
        c = calls.get(g["call_id"])
        if c is None:
            continue
        r = row_for(c, g, round_n=round_n, world_id=world_id)
        if r:
            rows.append(r)
    return rows


def build_dataset(rows: list[dict], out: Path, *, student_ratio: float = 1.0,
                  student_floor: int = 20, seed: int = 0) -> dict:
    """Dedupe, cap student rehearsal rows per task, shuffle, write JSONL."""
    seen, uniq = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            uniq.append(r)
    rng = random.Random(seed)
    teacher = [r for r in uniq if r["source"] == "teacher"]
    keep = list(teacher)
    per_task: dict[str, dict[str, int]] = {}
    for task in sorted({r["task"] for r in uniq}):
        t_n = sum(1 for r in teacher if r["task"] == task)
        studs = [r for r in uniq if r["source"] == "student" and r["task"] == task]
        rng.shuffle(studs)
        cap = max(student_floor, int(round(t_n * student_ratio)))
        keep += studs[:cap]
        per_task[task] = {"teacher": t_n, "student": min(cap, len(studs))}
    rng.shuffle(keep)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in keep:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"rows": len(keep), "per_task": per_task, "path": str(out)}
