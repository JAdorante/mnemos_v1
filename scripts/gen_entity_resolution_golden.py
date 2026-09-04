"""Generate tests/fixtures/goldens/entity_resolution.jsonl (plan 2.3).

Offline entity-resolution fixtures for People v2: exact/alias bind, ambiguous
short names (must leave_open), create_new, news knowledge-only, rejects, and
adversarial near-misses (must never false-merge).

    python scripts/gen_entity_resolution_golden.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "goldens" / "entity_resolution.jsonl"

# Shared roster fragments
_ROSTER_CORE = [
    {"name": "Marc Sullivan", "aliases": ["Marc"], "promotion_state": "active"},
    {"name": "Chris Falloon", "aliases": [], "promotion_state": "active"},
    {"name": "Chris Kim", "aliases": [], "promotion_state": "active"},
    {"name": "Sarah Chen", "aliases": ["Sarah"], "promotion_state": "active"},
    {"name": "Alex Rivera", "aliases": [], "promotion_state": "active"},
    {"name": "Alex One", "aliases": [], "promotion_state": "active"},
    {"name": "Alex Two", "aliases": [], "promotion_state": "active"},
    {"name": "Jordan Lee", "aliases": [], "promotion_state": "active"},
    {"name": "Patrick Adorante", "aliases": [], "promotion_state": "active"},
    {"name": "Eve Torres", "aliases": [], "promotion_state": "active"},
]

_AUDIO = {"event_source": "audio.whisper", "window": ""}
_NEWS = {"event_source": "desktop.screen", "window": "TMZ - Chrome"}
_NYT = {"event_source": "desktop.screen", "window": "The New York Times"}


def _case(cid: str, category: str, *, mention: str, roster: list,
          expect_decision: str, expect_person: str | None = None,
          text: str | None = None, relationship_boost: float = 0.7,
          event_source: str = "audio.whisper", window: str = "",
          merge_sensitive: bool = False,
          enrolled_user: str = "Hugh") -> dict:
    return {
        "id": cid,
        "category": category,
        "enrolled_user": enrolled_user,
        "roster": roster,
        "mention": mention,
        "text": text or f"{mention} will follow up",
        "event_source": event_source,
        "window": window,
        "relationship_boost": relationship_boost,
        "expect": {
            "decision": expect_decision,
            "person": expect_person,
        },
        # When True, an auto_resolve to the wrong person (or any auto_resolve
        # when expect is leave_open/reject) counts as a merge error.
        "merge_sensitive": merge_sensitive,
    }


def build_cases() -> list[dict]:
    cases: list[dict] = []
    core = list(_ROSTER_CORE)

    # --- exact full-name auto_resolve ---
    for p in ("Marc Sullivan", "Sarah Chen", "Patrick Adorante",
              "Jordan Lee", "Eve Torres", "Chris Falloon", "Chris Kim",
              "Alex Rivera"):
        cases.append(_case(
            f"exact-{p.lower().replace(' ', '-')}", "exact_match",
            mention=p, roster=core, expect_decision="auto_resolve",
            expect_person=p, relationship_boost=0.9, **_AUDIO))

    # --- alias exact (alias string on roster) ---
    alias_roster = [
        {"name": "Christopher Falloon", "aliases": ["Chris Falloon", "Chris F"],
         "promotion_state": "active"},
        {"name": "Marcus Sullivan", "aliases": ["Marc Sullivan"],
         "promotion_state": "active"},
        {"name": "Sarah Chen", "aliases": ["Sar Chen"],
         "promotion_state": "active"},
    ]
    for mention, person in (
        ("Chris Falloon", "Christopher Falloon"),
        ("Chris F", "Christopher Falloon"),
        ("Marc Sullivan", "Marcus Sullivan"),
        ("Sar Chen", "Sarah Chen"),
    ):
        cases.append(_case(
            f"alias-{mention.lower().replace(' ', '-')}", "alias_match",
            mention=mention, roster=alias_roster,
            expect_decision="auto_resolve", expect_person=person,
            relationship_boost=0.9, **_AUDIO))

    # --- ambiguous short names → leave_open (merge-sensitive) ---
    for mention, tag in (("Chris", "chris"), ("Alex", "alex")):
        for i, boost in enumerate((0.5, 0.6, 0.75, 0.9, 0.95)):
            cases.append(_case(
                f"ambig-{tag}-{i}", "ambiguous_short",
                mention=mention, roster=core,
                expect_decision="leave_open", expect_person=None,
                text=f"I spoke with {mention} about the pilot",
                relationship_boost=boost, merge_sensitive=True, **_AUDIO))

    # --- unique short nickname: prefix score < auto threshold → leave_open ---
    unique_roster = [
        {"name": "Marc Sullivan", "aliases": [], "promotion_state": "active"},
        {"name": "Sarah Chen", "aliases": [], "promotion_state": "active"},
    ]
    for mention, i in (("Marc", 0), ("Marc", 1), ("Sarah", 0), ("Sarah", 1)):
        cases.append(_case(
            f"short-unique-{mention.lower()}-{i}", "unique_short_leave_open",
            mention=mention, roster=unique_roster,
            expect_decision="leave_open", expect_person=None,
            relationship_boost=0.9 if i else 0.6,
            merge_sensitive=True, **_AUDIO))

    # --- create_new: unknown full name, high relevance ---
    newcomers = [
        "Avery Quinn", "Blake Horton", "Casey Nguyen", "Drew Patel",
        "Ellis Morgan", "Finley Brooks", "Gray Whitman", "Harper Cole",
        "Indie Walsh", "Jules Navarro", "Kai Brennan", "Logan Pierce",
    ]
    for name in newcomers:
        cases.append(_case(
            f"create-{name.lower().replace(' ', '-')}", "create_new",
            mention=name, roster=core, expect_decision="create_new",
            expect_person=None,
            text=f"{name} owns the launch checklist",
            relationship_boost=0.85, **_AUDIO))

    # --- low relevance + no match → leave_open (not mint) ---
    for name in ("Riley Stone", "Morgan Vale", "Quinn Asher"):
        cases.append(_case(
            f"weak-{name.lower().replace(' ', '-')}", "leave_open_weak",
            mention=name, roster=core, expect_decision="leave_open",
            expect_person=None,
            text=f"someone mentioned {name} in passing",
            relationship_boost=0.3, **_AUDIO))

    # --- reject junk / non-persons ---
    for junk in ("My Contacts", "My Files", "QA and CTO", "set it to",
                 "Sparrow", "C:/Users"):
        slug = junk.lower().replace(" ", "-").replace("/", "-")[:24]
        cases.append(_case(
            f"reject-{slug}",
            "reject_junk",
            mention=junk, roster=core, expect_decision="reject",
            expect_person=None, text=junk, relationship_boost=0.9,
            merge_sensitive=True, **_AUDIO))

    # --- OS account reject ---
    cases.append(_case(
        "reject-os-account", "reject_os",
        mention="Dell AI User", roster=core, expect_decision="reject",
        expect_person=None, text=r"C:\Users\Dell AI User\Documents",
        relationship_boost=0.9, merge_sensitive=True,
        event_source="desktop.screen", window="File Explorer"))

    # --- news: unknown public figure must not mint ---
    for name in ("Bill Clinton", "Ben Shapiro", "Elon Musk",
                 "Taylor Swift", "Oprah Winfrey"):
        for i, surf in enumerate((_NEWS, _NYT)):
            cases.append(_case(
                f"news-reject-{name.lower().replace(' ', '-')}-{i}",
                "news_no_mint",
                mention=name, roster=core, expect_decision="reject",
                expect_person=None,
                text=f"{name} spotted downtown — exclusive photos",
                relationship_boost=0.9, merge_sensitive=True, **surf))

    # --- news: exact bind to existing contact ok ---
    for name in ("Patrick Adorante", "Marc Sullivan", "Eve Torres"):
        cases.append(_case(
            f"news-bind-{name.lower().replace(' ', '-')}", "news_bind_existing",
            mention=name, roster=core, expect_decision="auto_resolve",
            expect_person=name,
            text=f"{name} mentioned in a sidebar",
            relationship_boost=0.5, **_NEWS))

    # --- adversarial near-miss: must not wrong-merge ---
    near = [
        ("Christina", "Chris Falloon"),   # not prefix-equal first token pair wrong way
        ("Christopher", "Chris Kim"),
        ("Alexandra", "Alex Rivera"),
        ("Marcus", "Marc Sullivan"),
        ("Alex Rivera-Smith", "Alex Rivera"),
    ]
    near_roster = list(core) + [
        {"name": "Alex Rivera-Smith", "aliases": [], "promotion_state": "active"},
        {"name": "Christina Park", "aliases": [], "promotion_state": "active"},
        {"name": "Christopher Nolan", "aliases": [], "promotion_state": "active"},
        {"name": "Marcus Aurelius", "aliases": [], "promotion_state": "active"},
        {"name": "Alexandra Bell", "aliases": [], "promotion_state": "active"},
    ]
    for mention, wrong in near:
        # Expect leave_open or create_new or auto_resolve to the *right* person —
        # never to `wrong`. encode as leave_open/create when no exact match.
        # Christina → Christina Park exact if we use full; short adversarial:
        cases.append(_case(
            f"near-{mention.lower()}-not-{wrong.lower().replace(' ', '-')}",
            "adversarial_near_miss",
            mention=mention, roster=near_roster,
            expect_decision="leave_open",  # conservative default; scorer also
            expect_person=None,           # checks auto_resolve ≠ wrong via forbid
            text=f"catch up with {mention}",
            relationship_boost=0.7, merge_sensitive=True, **_AUDIO))

    # Annotate forbidden person on adversarial cases
    for c in cases:
        if c["category"] == "adversarial_near_miss":
            # recover forbidden from id
            # near-christina-not-chris-falloon → Chris Falloon
            parts = c["id"].split("-not-", 1)
            if len(parts) == 2:
                forbid = parts[1].replace("-", " ").title()
                # title-case fix for multiword
                forbid = " ".join(w.capitalize() for w in parts[1].split("-"))
                c["expect"]["forbid_person"] = forbid

    # Fix forbid names properly from the near list
    forbid_by_mention = {m: w for m, w in near}
    for c in cases:
        if c["category"] == "adversarial_near_miss":
            c["expect"]["forbid_person"] = forbid_by_mention.get(c["mention"])
            # If exact person exists on roster, auto_resolve to them is OK
            names = {p["name"] for p in c["roster"]}
            if c["mention"] in names:
                c["expect"]["decision"] = "auto_resolve"
                c["expect"]["person"] = c["mention"]

    # --- self tokens ---
    for tok in ("me", "I", "myself"):
        cases.append(_case(
            f"self-{tok.lower()}", "self",
            mention=tok, roster=core, expect_decision="self",
            expect_person=None, relationship_boost=1.0, **_AUDIO))

    # --- absorbed person must not be chosen ---
    absorbed_roster = [
        {"name": "Sam Active", "aliases": [], "promotion_state": "active"},
        {"name": "Sam Absorbed", "aliases": ["Sam"], "promotion_state": "active",
         "canonical_person_id": "REF:Sam Active", "hide_from_people": True},
    ]
    cases.append(_case(
        "absorbed-skipped", "absorbed_hidden",
        mention="Sam Active", roster=absorbed_roster,
        expect_decision="auto_resolve", expect_person="Sam Active",
        relationship_boost=0.9, merge_sensitive=True, **_AUDIO))

    # Pad with more exact / create variants to clear ~100
    extras = [
        "Nora Blake", "Owen Carr", "Penny Diaz", "Remy Fox",
        "Sasha Gill", "Theo Hart", "Uma Iyer", "Vera Jain",
    ]
    for name in extras:
        cases.append(_case(
            f"exact-pad-{name.lower().replace(' ', '-')}", "exact_match",
            mention=name,
            roster=core + [{"name": name, "aliases": [], "promotion_state": "active"}],
            expect_decision="auto_resolve", expect_person=name,
            relationship_boost=0.9, **_AUDIO))
        cases.append(_case(
            f"create-pad-{name.lower().replace(' ', '-')}-x", "create_new",
            mention=name + " Jr", roster=core, expect_decision="create_new",
            expect_person=None,
            text=f"{name} Jr owns the Q3 plan",
            relationship_boost=0.9, **_AUDIO))

    return cases


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
        print(f"  {k:28} {cats[k]}")


if __name__ == "__main__":
    main()
