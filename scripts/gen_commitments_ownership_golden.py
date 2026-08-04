"""Generate tests/fixtures/goldens/commitments_ownership.jsonl (plan 2.2).

~150 turns covering commitments, quoted, negated, hypothetical, two-speaker,
and small-talk. Re-run to regenerate; the eval loads the jsonl as ground truth.

    python scripts/gen_commitments_ownership_golden.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "goldens" / "commitments_ownership.jsonl"
ENROLLED = "Hugh"

# (id_suffix, transcript, keywords) — clear first-person commitments.
_CLEAR = [
    ("send-deck", "I'll send Marc the deck by Friday", ["send", "deck"]),
    ("book-venue", "I need to book the venue next week", ["book", "venue"]),
    ("call-sarah", "I'll call Sarah about the pilot", ["call", "sarah"]),
    ("share-notes", "I'll share the notes after the standup", ["share", "notes"]),
    ("file-expense", "I have to file the expense report today", ["file", "expense"]),
    ("update-roadmap", "I'll update the roadmap before Thursday", ["update", "roadmap"]),
    ("email-quote", "I'll email them the $49 quote", ["email", "quote"]),
    ("schedule-demo", "I promised to schedule the demo with Chris", ["schedule", "demo"]),
    ("review-pr", "I'll review the PR this afternoon", ["review", "pr"]),
    ("send-invoice", "I'll send the invoice by end of day", ["send", "invoice"]),
    ("prep-slides", "I need to prep the slides for Monday", ["prep", "slides"]),
    ("follow-up", "I'll follow up with the customer tomorrow", ["follow", "customer"]),
    ("ship-patch", "I'll ship the patch tonight", ["ship", "patch"]),
    ("confirm-flight", "I have to confirm the flight to Austin", ["confirm", "flight"]),
    ("ping-legal", "I'll ping legal about the MSA", ["ping", "legal"]),
    ("draft-reply", "I'll draft a reply to their offer", ["draft", "reply"]),
    ("close-ticket", "I need to close the open ticket", ["close", "ticket"]),
    ("send-nda", "I'll send over the NDA", ["send", "nda"]),
    ("book-room", "I'll book a room for the offsite", ["book", "room"]),
    ("pay-invoice", "I still need to pay that invoice", ["pay", "invoice"]),
]

_OTHER_SPEAKERS = ["Marc", "Sarah", "Chris", "Alex", "Jordan"]

_QUOTED = [
    ("she-said-send", "She told me she'd send the deck Friday", ["send", "deck"]),
    ("he-said-call", "He said \"I'll call you tomorrow\"", ["call"]),
    ("marc-promised", "Marc promised he'd share the notes", ["share", "notes"]),
    ("they-said-book", "They said they'd book the venue", ["book", "venue"]),
    ("quoted-invoice", "She was like \"I'll send the invoice today\"", ["send", "invoice"]),
    ("relay-demo", "Chris told me he'd schedule the demo", ["schedule", "demo"]),
    ("relay-pr", "Alex said \"I'll review the PR\"", ["review", "pr"]),
    ("relay-patch", "Jordan said they'd ship the patch", ["ship", "patch"]),
    ("relay-legal", "She said \"I'll ping legal\"", ["ping", "legal"]),
    ("relay-nda", "He told me \"I'll send the NDA\"", ["send", "nda"]),
]

_HYP = [
    ("might-send", "I might send the deck Friday", ["send", "deck"]),
    ("if-book", "If we go, I'd book the venue", ["book", "venue"]),
    ("could-call", "I could call Sarah about it", ["call", "sarah"]),
    ("maybe-share", "Maybe I'll share the notes later", ["share", "notes"]),
    ("would-email", "I would email the quote if they ask", ["email", "quote"]),
    ("might-update", "I might update the roadmap", ["update", "roadmap"]),
    ("if-demo", "If they're free, I'd schedule the demo", ["schedule", "demo"]),
    ("could-review", "I could review the PR tonight", ["review", "pr"]),
    ("maybe-ship", "Maybe I'll ship the patch", ["ship", "patch"]),
    ("would-confirm", "I'd confirm the flight if the dates work", ["confirm", "flight"]),
]

_NEGATED = [
    ("dont-send", "Don't send the deck yet", ["send", "deck"]),
    ("wont-call", "I won't call Sarah about this", ["call", "sarah"]),
    ("not-booking", "I'm not booking the venue", ["book", "venue"]),
    ("never-share", "I'm never going to share those notes", ["share", "notes"]),
    ("skip-invoice", "Let's not send the invoice today", ["send", "invoice"]),
    ("hold-demo", "Don't schedule the demo yet", ["schedule", "demo"]),
    ("no-pr", "I am not reviewing that PR", ["review", "pr"]),
    ("cancel-patch", "We should not ship the patch", ["ship", "patch"]),
    ("no-legal", "Don't ping legal about this", ["ping", "legal"]),
    ("hold-nda", "I won't send the NDA", ["send", "nda"]),
]

_SMALL = [
    ("weather", "Nice weather today, huh?"),
    ("thanks", "Thanks, that helps a lot."),
    ("hmm", "Hmm, interesting."),
    ("lol", "Lol yeah exactly."),
    ("coffee", "Want to grab coffee later?"),
    ("how-are-you", "How are you doing?"),
    ("ok", "Okay sounds good."),
    ("bye", "Alright, talk later."),
    ("wow", "Wow, that's wild."),
    ("agree", "Yeah I agree."),
]


def _commitment(text: str, *, from_person: str = "me",
                assertion: str = "stated_by_user",
                span: str | None = None) -> dict:
    return {
        "text": text,
        "from_person": from_person,
        "to_person": "",
        "due": "",
        "confidence": 0.9,
        "source_span": span or text,
        "assertion": assertion,
    }


def _case(cid: str, category: str, *, speaker: str, transcript: str,
          expect_empty: bool, actionables: list, ownership: dict | None,
          assertion: str | None, gate: str | None, oracle: dict) -> dict:
    return {
        "id": cid,
        "category": category,
        "enrolled_user": ENROLLED,
        "speaker": speaker,
        "transcript": transcript,
        "expect_empty": expect_empty,
        "actionables": actionables,
        "claims": [],
        "ownership": ownership,
        "assertion": assertion,
        "gate": gate,
        "oracle": oracle,
    }


def build_cases() -> list[dict]:
    cases: list[dict] = []

    # --- enrolled-user clear commitments (~20) ---
    for suf, text, kw in _CLEAR:
        cases.append(_case(
            f"clear-{suf}", "stated_commitment",
            speaker=ENROLLED, transcript=text, expect_empty=False,
            actionables=[kw],
            ownership={"from_person": "me", "expect": "self"},
            assertion="stated_by_user", gate="insert",
            oracle={"tasks": [], "commitments": [_commitment(text)],
                    "claims": [], "entities": [], "relations": []},
        ))

    # --- me-relative: other speaker says I'll… (~25) ---
    for i, (suf, text, kw) in enumerate(_CLEAR[: len(_OTHER_SPEAKERS) * 5]):
        spk = _OTHER_SPEAKERS[i % len(_OTHER_SPEAKERS)]
        cases.append(_case(
            f"two-spk-{spk.lower()}-{suf}", "me_relative_other",
            speaker=spk, transcript=text, expect_empty=False,
            actionables=[kw],
            ownership={"from_person": "me", "expect": "speaker",
                       "expect_name": spk},
            assertion="stated_by_user", gate="insert",
            oracle={"tasks": [], "commitments": [_commitment(text)],
                    "claims": [], "entities": [], "relations": []},
        ))

    # --- me-relative: enrolled user (~15) ---
    for suf, text, kw in _CLEAR[:15]:
        cases.append(_case(
            f"two-spk-self-{suf}", "me_relative_self",
            speaker=ENROLLED, transcript=text, expect_empty=False,
            actionables=[kw],
            ownership={"from_person": "me", "expect": "self"},
            assertion="stated_by_user", gate="insert",
            oracle={"tasks": [], "commitments": [_commitment(text)],
                    "claims": [], "entities": [], "relations": []},
        ))

    # --- two-speaker ownership: unknown speaker + me → none (~10) ---
    for suf, text, kw in _CLEAR[:10]:
        cases.append(_case(
            f"unk-{suf}", "two_speaker_ownership",
            speaker="", transcript=text, expect_empty=False,
            actionables=[kw],
            ownership={"from_person": "me", "expect": "none"},
            assertion="stated_by_user", gate="insert",
            oracle={"tasks": [], "commitments": [_commitment(text)],
                    "claims": [], "entities": [], "relations": []},
        ))

    # --- quoted → review, not auto-insert (~20 = 10 templates × 2 speakers) ---
    for suf, text, kw in _QUOTED:
        for spk, tag in ((ENROLLED, "self"), ("Marc", "marc")):
            cases.append(_case(
                f"quoted-{tag}-{suf}", "quoted_no_insert",
                speaker=spk, transcript=text, expect_empty=True,
                actionables=[],  # must not auto-insert
                ownership=None,
                assertion="quoted", gate="review",
                oracle={"tasks": [], "commitments": [
                    _commitment(text, assertion="quoted")],
                    "claims": [], "entities": [], "relations": []},
            ))

    # --- hypothetical → review (~20) ---
    for suf, text, kw in _HYP:
        for spk, tag in ((ENROLLED, "self"), ("Sarah", "sarah")):
            cases.append(_case(
                f"hyp-{tag}-{suf}", "hypothetical_no_insert",
                speaker=spk, transcript=text, expect_empty=True,
                actionables=[],
                ownership=None,
                assertion="hypothetical", gate="review",
                oracle={"tasks": [], "commitments": [
                    _commitment(text, assertion="hypothetical")],
                    "claims": [], "entities": [], "relations": []},
            ))

    # --- negated → empty / no insert (~20) ---
    for suf, text, kw in _NEGATED:
        for spk, tag in ((ENROLLED, "self"), ("Chris", "chris")):
            cases.append(_case(
                f"neg-{tag}-{suf}", "negated_no_insert",
                speaker=spk, transcript=text, expect_empty=True,
                actionables=[],
                ownership=None,
                assertion=None, gate=None,
                oracle={"tasks": [], "commitments": [], "claims": [],
                        "entities": [], "relations": []},
            ))

    # --- small talk (~10) ---
    for suf, text in _SMALL:
        cases.append(_case(
            f"small-{suf}", "small_talk",
            speaker=ENROLLED, transcript=text, expect_empty=True,
            actionables=[],
            ownership=None,
            assertion=None, gate=None,
            oracle={"tasks": [], "commitments": [], "claims": [],
                    "entities": [], "relations": []},
        ))

    # Pad to ≥150 with variant phrasings of clear commitments if short.
    n = 0
    while len(cases) < 150:
        suf, text, kw = _CLEAR[n % len(_CLEAR)]
        cases.append(_case(
            f"pad-{n}-{suf}", "stated_commitment",
            speaker=ENROLLED,
            transcript=text + " for sure.",
            expect_empty=False,
            actionables=[kw],
            ownership={"from_person": "me", "expect": "self"},
            assertion="stated_by_user", gate="insert",
            oracle={"tasks": [], "commitments": [
                _commitment(text + " for sure.", span=text)],
                    "claims": [], "entities": [], "relations": []},
        ))
        n += 1

    return cases[: max(150, len(cases))]


def main() -> None:
    cases = build_cases()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for c in cases:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    cats: dict[str, int] = {}
    for c in cases:
        cats[c["category"]] = cats.get(c["category"], 0) + 1
    print(f"wrote {len(cases)} cases -> {OUT}")
    for k in sorted(cats):
        print(f"  {k:16} {cats[k]}")


if __name__ == "__main__":
    main()
