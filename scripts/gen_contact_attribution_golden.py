"""Generate tests/fixtures/goldens/contact_attribution.jsonl (plan 2.4).

5 mandate sentences from people_intelligence_architecture.md §F plus ~50
variants covering possessive / reach-at / co-mention theft / weak local-part
→ review / news+article mint-deny.

    python scripts/gen_contact_attribution_golden.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "goldens" / "contact_attribution.jsonl"

_AUDIO = {"event_source": "audio.whisper", "window": ""}
_NEWS = {"event_source": "desktop.screen", "window": "TMZ - Chrome"}
_CHROME = {"event_source": "desktop.screen", "window": "Chrome"}


def _case(cid: str, category: str, *, text: str, probes: list[dict],
          event_source: str = "audio.whisper", window: str = "",
          mandate: bool = False) -> dict:
    return {
        "id": cid,
        "category": category,
        "mandate": mandate,
        "text": text,
        "event_source": event_source,
        "window": window,
        # probes: each person we try to attribute against this text
        "probes": probes,
    }


def _probe(name: str, expect: str, *, kind: str | None = None,
           value: str | None = None) -> dict:
    """expect: write|skip|review|deny_policy"""
    d = {"person_name": name, "expect": expect}
    if kind:
        d["kind"] = kind
    if value:
        d["value"] = value
    return d


def build_cases() -> list[dict]:
    cases: list[dict] = []

    # --- 5 mandate sentences (architecture §F worked examples) ---
    cases.append(_case(
        "mandate-possessive-email", "mandate",
        text="Marc's email is marc@acme.com.",
        mandate=True, **_AUDIO,
        probes=[
            _probe("Marc", "write", kind="email", value="marc@acme.com"),
            _probe("Justin", "skip", kind="email", value="marc@acme.com"),
        ]))
    cases.append(_case(
        "mandate-reach-at", "mandate",
        text="Reach Marc at marc@acme.com.",
        mandate=True, **_AUDIO,
        probes=[
            _probe("Marc", "write", kind="email", value="marc@acme.com"),
            _probe("Justin", "skip", kind="email", value="marc@acme.com"),
        ]))
    cases.append(_case(
        "mandate-will-email-co-mention", "mandate",
        text="Justin will email Marc at marc@acme.com.",
        mandate=True, **_AUDIO,
        probes=[
            _probe("Marc", "write", kind="email", value="marc@acme.com"),
            _probe("Justin", "skip", kind="email", value="marc@acme.com"),
        ]))
    cases.append(_case(
        "mandate-copy-unassigned", "mandate",
        text="Email Justin and copy marc@acme.com.",
        mandate=True, **_AUDIO,
        probes=[
            _probe("Justin", "skip", kind="email", value="marc@acme.com"),
            _probe("Marc", "skip", kind="email", value="marc@acme.com"),
        ]))
    cases.append(_case(
        "mandate-forwarded-phone", "mandate",
        text="Marc forwarded Justin's number: 555-123-4567.",
        mandate=True, **_AUDIO,
        probes=[
            _probe("Justin", "write", kind="phone", value="555-123-4567"),
            _probe("Marc", "skip", kind="phone", value="555-123-4567"),
        ]))

    # --- possessive / address variants ---
    for name, email in (
        ("Sarah", "sarah@foundry.io"),
        ("Chris", "chris.kim@acme.com"),
        ("Alex", "alex@rivera.dev"),
        ("Jordan", "jordan.lee@corp.com"),
        ("Eve", "eve.torres@studio.io"),
    ):
        cases.append(_case(
            f"poss-{name.lower()}", "possessive",
            text=f"{name}'s email address is {email}",
            **_AUDIO,
            probes=[
                _probe(name, "write", kind="email", value=email),
                _probe("Other", "skip", kind="email", value=email),
            ]))
        cases.append(_case(
            f"reach-{name.lower()}", "reach_at",
            text=f"Reach {name} at {email}",
            **_AUDIO,
            probes=[
                _probe(name, "write", kind="email", value=email),
                _probe("Other", "skip", kind="email", value=email),
            ]))

    # --- co-mention theft (subject ≠ recipient) ---
    for subj, obj, email in (
        ("Justin", "Marc", "marc@acme.com"),
        ("Alex", "Sarah", "sarah@foundry.io"),
        ("Chris", "Eve", "eve.torres@studio.io"),
        ("Jordan", "Alex", "alex@rivera.dev"),
        ("Pat", "Chris", "chris.kim@acme.com"),
    ):
        cases.append(_case(
            f"theft-{subj.lower()}-{obj.lower()}", "co_mention_theft",
            text=f"{subj} will email {obj} at {email}.",
            **_AUDIO,
            probes=[
                _probe(obj, "write", kind="email", value=email),
                _probe(subj, "skip", kind="email", value=email),
            ]))

    # --- weak local-part + name nearby → review (score 1.5 < 2.0) ---
    for name, email in (
        ("Marc", "marc@acme.com"),
        ("Sarah", "sarah@foundry.io"),
        ("Chris", "chris@kim.io"),
    ):
        cases.append(_case(
            f"weak-local-{name.lower()}", "weak_review",
            text=f"Catching up with {name} later — also {email} is on the thread.",
            **_AUDIO,
            probes=[
                _probe(name, "review", kind="email", value=email),
            ]))

    # --- phones ---
    for name, phone in (
        ("Marc", "415-555-0101"),
        ("Sarah", "+1 212 555 0199"),
        ("Chris", "650-555-0144"),
    ):
        cases.append(_case(
            f"phone-poss-{name.lower()}", "possessive_phone",
            text=f"{name}'s phone number is {phone}",
            **_AUDIO,
            probes=[
                _probe(name, "write", kind="phone", value=phone),
                _probe("Other", "skip", kind="phone", value=phone),
            ]))
        cases.append(_case(
            f"phone-reach-{name.lower()}", "reach_phone",
            text=f"Call {name} at {phone}",
            **_AUDIO,
            probes=[
                _probe(name, "write", kind="phone", value=phone),
            ]))

    # --- news / article mint-deny (contacts + person candidates) ---
    for i, text in enumerate((
        "The article mentioned Bill Clinton at clinton@example.com",
        "the article mentioned Sarah Chen",
        "As mentioned in the article, Marc Sullivan leads the round",
        "According to the article Patrick Adorante joined Acme",
        "Breaking news: exclusive report on Eve Torres",
    )):
        cases.append(_case(
            f"article-deny-{i}", "article_mint_deny",
            text=text, **_CHROME,
            probes=[
                # Policy deny on contact extract; also expect news_page class
                _probe("Bill", "deny_policy"),
                _probe("Sarah", "deny_policy"),
                _probe("Marc", "deny_policy"),
            ]))

    for i, text in enumerate((
        "Bill Clinton spotted downtown — exclusive photos",
        "Patrick Adorante mentioned in a sidebar with email pat@tmz.test",
    )):
        cases.append(_case(
            f"news-deny-{i}", "news_contact_deny",
            text=text, **_NEWS,
            probes=[_probe("Patrick", "deny_policy"),
                    _probe("Bill", "deny_policy")]))

    # --- assistant / relational (Patrick must not steal assistant@) ---
    cases.append(_case(
        "assistant-not-patrick", "relational",
        text="Patrick's assistant can be reached at assistant@firm.com.",
        **_AUDIO,
        probes=[
            _probe("Patrick", "skip", kind="email", value="assistant@firm.com"),
        ]))

    # --- pad to ≥55 cases with more clear / theft / deny variants ---
    pad_names = [
        ("Nora", "nora@blake.io"), ("Owen", "owen@carr.dev"),
        ("Penny", "penny@diaz.co"), ("Remy", "remy.fox@mail.com"),
        ("Sasha", "sasha@gill.io"), ("Theo", "theo@hart.com"),
        ("Uma", "uma.iyer@corp.com"), ("Vera", "vera@jain.io"),
        ("Wade", "wade@knox.dev"), ("Xan", "xan.lee@studio.io"),
    ]
    for name, email in pad_names:
        cases.append(_case(
            f"pad-poss-{name.lower()}", "possessive",
            text=f"{name}'s email is {email}",
            **_AUDIO,
            probes=[_probe(name, "write", kind="email", value=email)]))
        cases.append(_case(
            f"pad-theft-{name.lower()}", "co_mention_theft",
            text=f"Pat will email {name} at {email}.",
            **_AUDIO,
            probes=[
                _probe(name, "write", kind="email", value=email),
                _probe("Pat", "skip", kind="email", value=email),
            ]))

    return cases


def main() -> None:
    cases = build_cases()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for c in cases:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    cats: dict[str, int] = {}
    mandates = 0
    probes = 0
    for c in cases:
        cats[c["category"]] = cats.get(c["category"], 0) + 1
        mandates += int(bool(c.get("mandate")))
        probes += len(c.get("probes") or [])
    print(f"wrote {len(cases)} cases ({probes} probes, {mandates} mandate) -> {OUT}")
    for k in sorted(cats):
        print(f"  {k:24} {cats[k]}")


if __name__ == "__main__":
    main()
