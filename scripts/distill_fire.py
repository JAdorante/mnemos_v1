"""Rapid-fire memory questions against a running /ui server for distill labeling.

Requires uvicorn (or run_all) already up. Sends each question to POST /chat,
polls until a result, then prompts for a/r/e (accepted / rejected / edited).
Only escalated answers get a distill_id — those are the ones worth labeling.

Usage:
    python scripts/distill_fire.py              # built-in question pack
    python scripts/distill_fire.py --limit 5
    python scripts/distill_fire.py --file questions.txt
    python scripts/distill_fire.py --base http://127.0.0.1:8000 --delay 0.5

Keys at the prompt:  a=accept  r=reject  e=edit  s=skip  q=quit
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# General-purpose question pack (hard rule: no personal names in code, and
# onboarding must never require a training session — this script is a DEV
# labeling accelerator, so even it stays generic). Entity questions are
# templates whose slots fill at runtime from THIS install's own knowledge
# graph; a slot the graph can't fill drops its template, so a fresh data dir
# still yields the generic pack and never renders a placeholder name.
GENERIC_QUESTIONS = [
    "What open items or follow-ups show up in my recent memories?",
    "List people mentioned in my recent memories. Names only, no guessing.",
    "List topics I've talked about recently that aren't people's names. Memories only.",
    "Do I have a clear next meeting time stored? If not, say you don't have it.",
    "Is there anything about a status update or a number I promised someone in memory?",
    "What do my memories say about catching up or meeting later today?",
    "What did I say about money or stocks recently? Memories only — no guessing.",
    "Summarize my most recent day in two sentences from memory.",
    "What notes or reminders am I most likely forgetting about? Memories only.",
    "What should I remember about how I like things drafted, if anything is stored?",
]

ENTITY_TEMPLATES = [
    "What do I know about {person} from memory? Bullet points only; if thin, say so.",
    "Who is {person2} in my memories, and was I going to send them anything?",
    "What did I say about {project}? Memories only — don't invent a description.",
    "Any notes about {person} and a deal, quote, or follow-up? Memories only.",
    "What's the most reliable fact you have about {person2} from memory?",
    "Tell me about {person} — memories only; if you don't have it, say you don't.",
    "What do my memories say about {org}?",
]


def build_questions() -> list[str]:
    """Generic pack + entity templates filled from the user's own vocabulary."""
    qs = list(GENERIC_QUESTIONS)
    slots: dict[str, str] = {}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.services.vocabulary import vocabulary
        t = vocabulary.get_bias_terms()
        people = [p for p in (t.get("people") or []) if p]
        orgs = [o for o in (t.get("orgs") or []) if o]
        projects = [p for p in (t.get("projects") or []) if p]
        if people:
            slots["person"] = people[0]
            slots["person2"] = people[1] if len(people) > 1 else people[0]
        if orgs:
            slots["org"] = orgs[0]
        if projects:
            slots["project"] = projects[0]
    except Exception as exc:
        print(f"[fire] vocabulary unavailable ({exc}); generic pack only.",
              file=sys.stderr)
    for tpl in ENTITY_TEMPLATES:
        try:
            qs.append(tpl.format(**slots))
        except (KeyError, IndexError):
            continue    # graph lacks this slot kind — drop the template
    return qs


def _http(method: str, url: str, body: dict | None = None, timeout: float = 60) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


def load_questions(path: Path | None) -> list[str]:
    if path is None:
        return build_questions()
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def ask_one(base: str, question: str, *, timeout_s: float) -> tuple[str, str | None]:
    """Send one question; return (answer_text, distill_id_or_None)."""
    start = _http("POST", f"{base}/chat", {"message": question})
    since = int(start.get("since") or 0)
    deadline = time.time() + timeout_s
    answer = ""
    distill_id = None
    while time.time() < deadline:
        poll = _http("GET", f"{base}/chat/poll?since={since}")
        for ev in poll.get("events") or []:
            since = max(since, int(ev.get("id") or 0) + 1)
            kind = ev.get("kind")
            if kind == "result":
                answer = str(ev.get("text") or "")
                distill_id = ev.get("distill_id")
                return answer, distill_id
            if kind == "error":
                return f"[error] {ev.get('text')}", None
            if kind == "ask":
                # Approval / clarification — skip for this fire loop.
                return f"[ask] {ev.get('text')}", None
        time.sleep(0.4)
    return "[timeout waiting for result]", None


def label(base: str, distill_id: str, outcome: str, edited: str | None = None) -> bool:
    body = {"distill_id": distill_id, "outcome": outcome}
    if edited is not None:
        body["edited_text"] = edited
    try:
        _http("POST", f"{base}/chat/outcome", body)
        return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"  label failed: {exc.code} {detail}", file=sys.stderr)
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--file", type=Path, default=None,
                    help="text file, one question per line (# comments ok)")
    ap.add_argument("--limit", type=int, default=0, help="max questions (0=all)")
    ap.add_argument("--delay", type=float, default=0.3,
                    help="pause between questions (seconds)")
    ap.add_argument("--timeout", type=float, default=120,
                    help="seconds to wait for each answer")
    ap.add_argument("--auto-reject-no-distill", action="store_true",
                    help="skip prompt when no distill_id (local-only answer)")
    args = ap.parse_args()

    questions = load_questions(args.file)
    if args.limit > 0:
        questions = questions[: args.limit]
    if not questions:
        sys.exit("no questions")

    print(f"firing {len(questions)} questions at {args.base}")
    print("keys: a=accept  r=reject  e=edit  s=skip  q=quit\n")

    n_ok = n_skip = n_nodistill = 0
    for i, q in enumerate(questions, 1):
        print(f"[{i}/{len(questions)}] {q}")
        try:
            answer, did = ask_one(args.base, q, timeout_s=args.timeout)
        except urllib.error.URLError as exc:
            sys.exit(f"server not reachable at {args.base}: {exc}\n"
                     "start uvicorn first: uvicorn app.main:app --reload")
        print(f"  -> {answer[:300]}{'…' if len(answer) > 300 else ''}")
        if not did:
            n_nodistill += 1
            print("  (no distill_id — local-only or non-escalated; nothing to label)")
            if args.auto_reject_no_distill:
                time.sleep(args.delay)
                continue
            # still allow skip/quit
            key = input("  [s]kip / [q]uit: ").strip().lower() or "s"
            if key.startswith("q"):
                break
            time.sleep(args.delay)
            continue

        print(f"  distill={did[:8]}…")
        while True:
            key = input("  verdict [a/r/e/s/q]: ").strip().lower()
            if not key:
                continue
            if key.startswith("q"):
                print(f"\ndone early. labeled={n_ok} skipped={n_skip} "
                      f"no-distill={n_nodistill}")
                return
            if key.startswith("s"):
                n_skip += 1
                break
            if key.startswith("a"):
                if label(args.base, did, "accepted"):
                    print("  -> accepted")
                    n_ok += 1
                break
            if key.startswith("r"):
                if label(args.base, did, "rejected"):
                    print("  -> rejected")
                    n_ok += 1
                break
            if key.startswith("e"):
                edited = input("  corrected answer: ").strip()
                if not edited:
                    print("  edit needs text")
                    continue
                if label(args.base, did, "edited", edited):
                    print("  -> edited")
                    n_ok += 1
                break
            print("  use a, r, e, s, or q")
        time.sleep(args.delay)

    print(f"\ndone. labeled={n_ok} skipped={n_skip} no-distill={n_nodistill}")
    print("progress: python scripts/distill_curate.py --no-dedupe-embed")


if __name__ == "__main__":
    main()
