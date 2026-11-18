"""Entity typing golden set: replay labelled sightings through the real
pipeline and score what Sparrow makes of each name.

The golden set (`tests/fixtures/goldens/entity_typing.jsonl`) is a list of
seats and their sightings. Most seats are synthetic companies from unrelated
industries (`origin` world-*); a minority are the source plan's own examples
of the failure (`origin` plan). Each seat is replayed on its own fresh Store
in a temp directory: the projects its user files notes under are filed the
way an accepted note files them (`projects.ensure_filed`), then every
sighting is an event read by the extractor's own entity path
(`Extractor._persist_entities`: the mint gate, the candidate ledger and
promotion, the kind vote), and the scope pass (`entity_scope.run`) runs at
the end. Nothing in that path is stubbed; only the name embedder (alias
proposals, person matching) is off, so the replay needs no model, and the
seat belongs to no org (no shared project tier).

A case is one name in one seat. Its outcome is the kind of the visible row
that carries the name as its own (`none` when there is none: still a
candidate, never promoted, or refused) and that row's parent; a name the
resolver folded into another row as an alias is `alias` (and the row it
went into is reported). Scored:

- project precision: of the cases that came out as a `project` row of
  their own, the share the label says are projects (a name folded into a
  project row as an alias made no project row; it is an accuracy miss).
  Overall, and per seat: a model "project" must never become a project,
  so a false project anywhere is a defect. A seat is gated on its own when
  it has at least MIN_SUPPORT project cases (labelled or predicted); seats
  under that are reported together, and that pool admits no false project
  at all (gated at the floor, thirty one-project seats would let one false
  project through). A gated seat with 19 or more project cases would admit
  one at the floor; the golden set's seats hold two to four.
- accuracy: kind and parent both right, over every case.
- rule cases: a case in one of RULE_CATEGORIES is decided by one
  deterministic rule of the entity model, so none may be wrong (the
  accuracy floor alone lets a whole rule break: the code-shape, lowercase
  description and bind-only rules each cost three or four cases).

The rules were tuned on the same synthetic worlds these seats come from
(the thresholds and the repair rules were measured on them), so this is a
regression gate for the shipped rules, not evidence that they generalise to
seats it has not seen.

    python3 scripts/eval_entity_typing.py            # report
    python3 scripts/eval_entity_typing.py --gate     # exit 1 under a floor
    python3 scripts/eval_entity_typing.py --json

The thresholds the pipeline reads (`QUILL_ENTITY_CANDIDATE_*`,
`QUILL_ENTITY_CODE_KIND_MIN_CONF`, `QUILL_ENTITY_KIND_*`,
`QUILL_ENTITY_SCOPE*`) are cleared for the replay: the golden set measures
the shipped defaults.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

GOLDEN = ROOT / "tests" / "fixtures" / "goldens" / "entity_typing.jsonl"
PROJECT_FLOOR = 0.95
ACCURACY_FLOOR = 0.95
# A seat is gated on its own from this many project cases (labelled or
# predicted): below it one case is the whole number.
MIN_SUPPORT = 2
# Categories one deterministic rule decides: the filing prior, the legal
# form (filed or not), the email-domain prior, the code-shape rule (and its
# G1 exception), the lowercase-description rule, the value rule, bind-only
# sources, the known-person guard, the person-shape rule (two Title Case
# words are no person without person evidence), the typed signal and
# recurrence. A miss there is that rule broken, not noise, so the gate
# allows none. The rest (recurring orgs, places and tools, mixed votes,
# topics and names inside a project) also rest on the vote's margins, the
# scope pass and the shared corrector, and are held by the accuracy floor
# and project precision.
RULE_CATEGORIES = frozenset({
    "filed_project", "filed_project_other_kind", "filed_company",
    "org_legal_form", "org_email_domain", "code_id_in_project",
    "code_id_once", "code_named_tool", "description", "value", "bind_only",
    "person_name", "person_shaped_topic", "typed_once", "org_once",
    "place_once", "model_project_one_session"})

BASE = 1_790_000_000.0          # sightings are timed from here
DAY = 86400.0
# Source label -> (event source, speech?). Speech reaches the extractor with
# no event source; the anchor event says what it was.
SOURCES = {
    "typed": ("chat.user", False),          # the user's own chat turn
    "mail": ("google.mail", False),
    "team_chat": ("slack.message", False),
    "meeting": ("notion.transcript", False),
    "document": ("notion.document", False),
    "upload": ("chat.attach", False),
    "calendar": ("google.calendar", False),
    "speech": ("audio.whisper", True),
    "note": ("assistant.claude", False),    # an assistant note: binds only
    "screen": ("desktop.screen", False),    # screen text: binds only
}
KINDS = ("org", "project", "tool", "place", "idea", "label", "thing", "none")
_ENV_CLEARED = (
    "QUILL_ENTITY_CANDIDATE_SESSIONS", "QUILL_ENTITY_CANDIDATE_TTL_DAYS",
    "QUILL_ENTITY_CANDIDATE_SESSION_GAP_S",
    "QUILL_ENTITY_CANDIDATE_SUMMARY_TTL_DAYS",
    "QUILL_ENTITY_CODE_KIND_MIN_CONF", "QUILL_ENTITY_KIND_INCUMBENT_PRIOR",
    "QUILL_ENTITY_KIND_FLIP_MARGIN", "QUILL_ENTITY_KIND_MIN_DISSENT",
    "QUILL_ENTITY_SCOPE", "QUILL_ENTITY_SCOPE_MIN_EVENTS",
    "QUILL_ENTITY_SCOPE_MIN_EVENTS_LABEL", "QUILL_ENTITY_CANDIDATES",
    "QUILL_EXTRACT_ENTITY_V2")


# ---------------------------------------------------------------- load --
def load(path: Path | str = GOLDEN) -> dict:
    """{"seats": {seat: setup}, "mentions": [...]} from the JSONL file,
    checked: every mention names a known seat and source, every kind is a
    stored kind or `none`, and every mention of one name in one seat
    expects the same outcome."""
    seats: dict[str, dict] = {}
    mentions: list[dict] = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8")
                             .splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("type") == "about":
            continue                # what the file is, for a reader
        if row.get("type") == "seat":
            seats[row["seat"]] = row
        elif row.get("type") == "mention":
            row["_line"] = n
            mentions.append(row)
        else:
            raise ValueError(f"line {n}: unknown row type {row.get('type')!r}")
    outcome: dict[tuple, tuple] = {}
    for m in mentions:
        if m["seat"] not in seats:
            raise ValueError(f"line {m['_line']}: unknown seat {m['seat']!r}")
        if m["source"] not in SOURCES:
            raise ValueError(f"line {m['_line']}: unknown source "
                             f"{m['source']!r}")
        exp = m.get("expect") or {}
        if exp.get("kind") not in KINDS:
            raise ValueError(f"line {m['_line']}: expected kind "
                             f"{exp.get('kind')!r}")
        key = (m["seat"], _key(m["name"]))
        got = (exp.get("kind"), _key(exp.get("parent") or ""))
        if outcome.setdefault(key, got) != got:
            raise ValueError(f"line {m['_line']}: {m['name']!r} expects "
                             f"{got}, an earlier line {outcome[key]}")
    return {"seats": seats, "mentions": mentions}


def _key(name: str) -> str:
    from app.services.kg_keys import entity_name_key
    return entity_name_key(name or "")


# -------------------------------------------------------------- replay --
@contextlib.contextmanager
def _seat_env(store, data_dir: str):
    """The process-wide seams the pipeline reads, pointed at this seat's
    temp store and data dir, for the length of one replay."""
    from app import storage as storage_mod
    from app.services import embeddings, projects, resolution
    env = {"QUILL_DATA_DIR": data_dir, "QUILL_HEADS": "off",
           "QUILL_USAGE_LEDGER": "0"}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        for var in _ENV_CLEARED:
            stack.enter_context(patch.dict(os.environ, {var: ""}))
            os.environ.pop(var, None)
        stack.enter_context(patch.object(
            resolution, "resolver", resolution.Resolver(store=store)))
        stack.enter_context(patch("app.storage.get_store",
                                  return_value=store))
        stack.enter_context(patch.object(storage_mod, "_store", None))
        stack.enter_context(patch.object(projects, "_scopes", lambda: []))

        def no_model(*a, **k):
            raise RuntimeError("no name embedder in the golden replay")
        stack.enter_context(patch.object(embeddings.embedder, "encode",
                                         no_model))
        projects.invalidate_filed_keys()
        try:
            yield
        finally:
            projects.invalidate_filed_keys()


def _file(store, project: str) -> None:
    """What accepting a note filed under `project` writes: the accepted
    capture (the registry's filed tier) and `ensure_filed`."""
    from app.services import projects
    from app.services.remote_mcp import connect
    with connect() as c:
        c.execute("INSERT INTO captures (created_at, project, kind, text, "
                  "status, decided_at) VALUES (?, ?, 'note', '', "
                  "'accepted', ?)", (BASE, project, BASE))
        c.commit()
    projects.invalidate_filed_keys()
    projects.ensure_filed(project, store=store, now=BASE, user_accept=True)


def _people(store, setup: dict) -> None:
    for p in setup.get("people") or ():
        pid = store.resolve_person(p["name"], ts=BASE)
        if p.get("email") and pid:
            store.upsert_contact_point(
                person_id=int(pid), type_="email", value_display=p["email"],
                value_normalized=p["email"].lower(), confidence=0.9,
                attribution_method="email_header",
                verification_status="attributed", source_event_id=None,
                evidence_quote=None, discourse_role=None, ts=BASE,
                created_by="system", pipeline_version="golden")


def replay_seat(setup: dict, mentions: list[dict]) -> dict:
    """Replay one seat on a fresh Store; {(name key): outcome} for every
    name its mentions carry."""
    from app.events import Event, Modality
    from app.storage import Store
    with tempfile.TemporaryDirectory(prefix="golden_entity_") as tmp:
        store = Store(db_path=Path(tmp) / "quill.db",
                      audio_dir=Path(tmp) / "audio")
        try:
            with _seat_env(store, tmp):
                from app.services import entity_scope
                from app.services.extractor import Extractor
                _people(store, setup)
                for project in setup.get("filed") or ():
                    _file(store, project)
                ex = Extractor(store=store)
                order = sorted(enumerate(mentions),
                               key=lambda im: (float(im[1]["session"]), im[0]))
                slot: dict[float, int] = defaultdict(int)
                for _, m in order:
                    s = float(m["session"])
                    # One minute apart inside a session, a day between
                    # sessions.
                    ts = BASE + s * DAY + 60.0 * slot[s]
                    slot[s] += 1
                    source, speech = SOURCES[m["source"]]
                    meta = ({"project": m["filing_project"]}
                            if m.get("filing_project") else {})
                    ev = int(store.insert(Event(
                        time=ts,
                        modality=Modality.AUDIO if speech else Modality.TEXT,
                        raw=m["context"], source=source, meta=meta)))
                    ent = {"name": m["name"], "kind": m.get("proposed_kind"),
                           "confidence": m.get("confidence", 0.9),
                           "source_span": m["context"]}
                    if m.get("parent"):
                        ent["parent"] = m["parent"]
                    facts = {"entities": [ent], "relations": []}
                    if speech:
                        ex._persist_entities(facts, ev, ts,
                                             turn_text=m["context"])
                    else:
                        ex._persist_entities(facts, ev, ts,
                                             event_source=source,
                                             text=m["context"])
                entity_scope.run(store)
                return _outcomes(store, mentions)
        finally:
            store.close()


def _outcomes(store, mentions: list[dict]) -> dict:
    rows = store.all_entities()
    by_id = {int(e["id"]): e for e in rows}
    own: dict[str, dict] = {}
    folded: dict[str, dict] = {}
    for e in rows:
        own.setdefault(_key(e["name"]), e)
        for n in e.get("aliases") or []:
            folded.setdefault(_key(n), e)
    out = {}
    for m in mentions:
        k = _key(m["name"])
        e = own.get(k)
        if e is None and k in folded:
            out[k] = {"kind": "alias", "parent": None,
                      "folded_into": folded[k]["name"]}
            continue
        if e is None:
            out[k] = {"kind": "none", "parent": None}
            continue
        pid = e.get("parent_id")
        parent = by_id.get(int(pid)) if pid is not None else None
        out[k] = {"kind": e.get("kind") or "idea",
                  "parent": parent["name"] if parent else None,
                  "kind_source": e.get("kind_source")}
    return out


# --------------------------------------------------------------- score --
def evaluate(golden: dict | None = None) -> dict:
    """Replay every seat and score it. Returns the report `gate` reads."""
    golden = golden if golden is not None else load()
    by_seat: dict[str, list[dict]] = defaultdict(list)
    for m in golden["mentions"]:
        by_seat[m["seat"]].append(m)
    cases: list[dict] = []
    for seat, ms in sorted(by_seat.items()):
        setup = golden["seats"][seat]
        got = replay_seat(setup, ms)
        seen: set[str] = set()
        for m in ms:
            k = _key(m["name"])
            if k in seen:
                continue
            seen.add(k)
            exp = m["expect"]
            pred = got[k]
            ok_kind = pred["kind"] == exp["kind"]
            ok_parent = _key(pred.get("parent") or "") == \
                _key(exp.get("parent") or "")
            cases.append({
                "seat": seat, "origin": setup.get("origin"),
                "industry": setup.get("industry"), "name": m["name"],
                "category": m.get("category"),
                "mentions": sum(1 for x in ms if _key(x["name"]) == k),
                "expected": exp["kind"], "expected_parent": exp.get("parent"),
                "predicted": pred["kind"], "predicted_parent": pred.get("parent"),
                "folded_into": pred.get("folded_into"),
                "ok": ok_kind and ok_parent})
    return score(cases, golden)


def _precision(cases: list[dict]) -> dict:
    tp = sum(1 for c in cases if c["predicted"] == "project"
             and c["expected"] == "project")
    fp = sum(1 for c in cases if c["predicted"] == "project"
             and c["expected"] != "project")
    fn = sum(1 for c in cases if c["expected"] == "project"
             and c["predicted"] != "project")
    support = sum(1 for c in cases if c["predicted"] == "project"
                  or c["expected"] == "project")
    return {"tp": tp, "fp": fp, "fn": fn, "support": support,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None}


def score(cases: list[dict], golden: dict | None = None) -> dict:
    seats: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        seats[c["seat"]].append(c)
    per_seat = {}
    small: list[dict] = []
    failures: list[str] = []
    for seat, cs in sorted(seats.items()):
        p = _precision(cs)
        gated = p["support"] >= MIN_SUPPORT
        row = {"origin": cs[0]["origin"], "industry": cs[0]["industry"],
               "cases": len(cs), "accuracy": round(
                   sum(c["ok"] for c in cs) / len(cs), 4),
               **p, "gated": "own" if gated else "pooled"}
        per_seat[seat] = row
        if not gated:
            small.extend(cs)
        elif p["precision"] is not None and p["precision"] < PROJECT_FLOOR:
            failures.append(f"{seat}: project precision {p['precision']} "
                            f"< {PROJECT_FLOOR} ({p['fp']} false)")
    pooled = _precision(small) if small else None
    # Not the floor: small seats pooled at 0.95 would hide one false project
    # behind enough true ones, which is what pooling must never do.
    if pooled and pooled["fp"]:
        failures.append(f"seats under {MIN_SUPPORT} project cases, pooled: "
                        f"{pooled['fp']} false project(s), precision "
                        f"{pooled['precision']}")
    overall = _precision(cases)
    accuracy = round(sum(c["ok"] for c in cases) / len(cases), 4) \
        if cases else None
    if overall["precision"] is not None \
            and overall["precision"] < PROJECT_FLOOR:
        failures.append(f"overall project precision {overall['precision']} "
                        f"< {PROJECT_FLOOR}")
    if accuracy is None or accuracy < ACCURACY_FLOOR:
        failures.append(f"overall accuracy {accuracy} < {ACCURACY_FLOOR}")
    rule_cases = [c for c in cases if c.get("category") in RULE_CATEGORIES]
    rule_misses = [c for c in rule_cases if not c["ok"]]
    if rule_misses:
        failures.append(
            f"rule cases wrong ({len(rule_misses)} of {len(rule_cases)}): "
            + ", ".join(f"{c['seat']} {c['category']}" for c in rule_misses))
    by_origin: dict[str, dict] = {}
    for origin in sorted({c["origin"] or "" for c in cases}):
        cs = [c for c in cases if (c["origin"] or "") == origin]
        by_origin[origin] = {"cases": len(cs), **_precision(cs),
                             "accuracy": round(sum(c["ok"] for c in cs)
                                               / len(cs), 4)}
    by_cat: dict[str, dict] = {}
    for cat in sorted({c["category"] or "" for c in cases}):
        cs = [c for c in cases if (c["category"] or "") == cat]
        by_cat[cat] = {"cases": len(cs),
                       "ok": sum(c["ok"] for c in cs)}
    mentions = golden["mentions"] if golden else []
    seat_origin = {s: (golden["seats"][s].get("origin") or "")
                   for s in golden["seats"]} if golden else {}
    return {
        "floors": {"project_precision": PROJECT_FLOOR,
                   "accuracy": ACCURACY_FLOOR, "min_support": MIN_SUPPORT},
        "mentions": len(mentions),
        "mentions_by_origin": _count(seat_origin.get(m["seat"], "")
                                     for m in mentions),
        "cases": len(cases), "accuracy": accuracy, "project": overall,
        "rule_cases": {"cases": len(rule_cases),
                       "ok": len(rule_cases) - len(rule_misses)},
        "pooled_small_seats": pooled, "per_seat": per_seat,
        "by_origin": by_origin, "by_category": by_cat,
        "misses": [c for c in cases if not c["ok"]],
        "failures": failures, "passed": not failures}


def _count(items) -> dict:
    out: dict = defaultdict(int)
    for i in items:
        out[i] += 1
    return dict(sorted(out.items()))


# ---------------------------------------------------------------- main --
def _pct(v) -> str:
    return "-" if v is None else f"{v:.3f}"


def print_report(rep: dict) -> None:
    print(f"golden: {rep['mentions']} mentions, {rep['cases']} cases; "
          f"mentions by origin {rep['mentions_by_origin']}")
    p = rep["project"]
    print(f"overall: accuracy {_pct(rep['accuracy'])} (floor "
          f"{ACCURACY_FLOOR}); project precision {_pct(p['precision'])} "
          f"(floor {PROJECT_FLOOR}) tp {p['tp']} fp {p['fp']} fn {p['fn']}")
    r = rep["rule_cases"]
    print(f"rule cases: {r['ok']} of {r['cases']} right (all must be)")
    print(f"{'seat':16} {'origin':12} {'cases':>5} {'acc':>6} "
          f"{'proj P':>7} {'tp':>3} {'fp':>3} {'fn':>3} gate  industry")
    for seat, r in rep["per_seat"].items():
        print(f"{seat:16} {r['origin'] or '':12} {r['cases']:>5} "
              f"{_pct(r['accuracy']):>6} {_pct(r['precision']):>7} "
              f"{r['tp']:>3} {r['fp']:>3} {r['fn']:>3} {r['gated']:5} "
              f"{r['industry'] or ''}")
    if rep["pooled_small_seats"]:
        q = rep["pooled_small_seats"]
        print(f"pooled seats under {MIN_SUPPORT} project cases: precision "
              f"{_pct(q['precision'])} tp {q['tp']} fp {q['fp']}")
    for origin, r in rep["by_origin"].items():
        print(f"origin {origin}: {r['cases']} cases, accuracy "
              f"{_pct(r['accuracy'])}, project precision "
              f"{_pct(r['precision'])}")
    for c in rep["misses"]:
        print(f"  miss {c['seat']}: {c['name']!r} expected {c['expected']}"
              f"{' in ' + c['expected_parent'] if c['expected_parent'] else ''}"
              f", got {c['predicted']}"
              f"{' in ' + c['predicted_parent'] if c['predicted_parent'] else ''}"
              f"{' of ' + c['folded_into'] if c.get('folded_into') else ''}"
              f" ({c['category']})")
    print("PASS" if rep["passed"] else "FAIL: " + "; ".join(rep["failures"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--golden", type=Path, default=GOLDEN)
    ap.add_argument("--gate", action="store_true",
                    help="exit 1 when a floor is not met")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rep = evaluate(load(args.golden))
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True, default=str))
    else:
        print_report(rep)
    return 1 if (args.gate and not rep["passed"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
