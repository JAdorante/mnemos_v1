"""Graph hygiene — purge junk person/entity nodes the extractor created before
the write-time name gate (services/name_quality.py) existed.

It flags exactly what the gate would now REJECT — pronouns, role words, sentence
fragments, system tokens, file paths — so the cleanup and the prevention agree.
Ambiguous-but-plausible nodes (e.g. a bare "Dell") are kept, not guessed at.

Usage (from the repo root):
    python scripts/graph_cleanup.py            # dry run — list what would go
    python scripts/graph_cleanup.py --apply    # back up to JSON, then delete

Reversible: --apply first writes every deleted row to
data/graph_cleanup_backup_<ts>.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Junk labels can contain non-cp1252 chars (em dashes, arrows); don't let a print
# crash the run on a Windows console.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.services.name_quality import is_plausible_entity, is_plausible_person  # noqa: E402
from app.storage import get_store  # noqa: E402


def find_junk(store) -> tuple[list[dict], list[dict]]:
    people = [p for p in store.all_people()
              if not is_plausible_person(p.get("name") or p.get("canonical_name") or "")]
    entities = [e for e in store.all_entities()
                if not is_plausible_entity(e.get("name") or e.get("canonical_name") or "")]
    return people, entities


def _entity_event_sources(store, entity_id: int) -> tuple[set[str], bool]:
    """The distinct event `source`s that support an entity (via its graph edges),
    and whether it has any supporting edges at all."""
    rel = store.relations_of("entity", entity_id)
    srcs: set[str] = set()
    has_edges = False
    for edge in rel.get("out", []) + rel.get("in", []):
        sev = edge.get("source_event_id")
        if not sev:
            continue
        has_edges = True
        ev = store.by_ids_map([sev]).get(sev)
        if ev is not None:
            srcs.add(ev.source or "")
    return srcs, has_edges


_CODE_JUNK_TOKEN = __import__("re").compile(r"[a-z][A-Z]")   # camelCase compound


def _looks_like_code_junk(name: str) -> bool:
    """Within the doc-only set, is this UNAMBIGUOUS code/architecture junk (safe
    to auto-delete) rather than a real single-word brand (Anthropic, Word)?

    A real brand mentioned in a doc is a single, capitalized, plain word. Junk is
    lowercase tokens (esbuild), snake/hyphen tech tokens (escalate_distill.jsonl),
    camelCase (EventBus), version strings (llama3.2), or multi-word architecture
    phrases ('FastAPI server', 'Memory Engine', 'Anthropic API key')."""
    n = (name or "").strip()
    words = n.split()
    if len(words) >= 2:                 # arch phrase / "<Thing> API key" / fragment
        return True
    w = words[0] if words else n
    if not w[:1].isupper():             # lowercase -> dep token / common noun
        return True
    if "_" in w or "-" in w:            # snake / hyphen tech token
        return True
    if any(c.isdigit() for c in w):     # version string
        return True
    if _CODE_JUNK_TOKEN.search(w):      # camelCase compound
        return True
    return False


def find_doc_only_entities(store, source: str = "documents.scan"
                           ) -> tuple[list[dict], list[dict]]:
    """Entities whose ONLY support is the document ingestion, SPLIT into
    (code_junk, ambiguous). Scan-created tools (GitHub, Cursor) have NO edges so
    they're never flagged; an entity also mentioned in speech/vision keeps a real
    edge and survives. `code_junk` is safe to auto-delete; `ambiguous` (single
    capitalized brands like Anthropic/Word that happened to be named in a doc) is
    surfaced for manual review, not deleted."""
    junk, ambiguous = [], []
    for e in store.all_entities():
        srcs, has_edges = _entity_event_sources(store, int(e["id"]))
        if has_edges and srcs and srcs <= {source}:
            (junk if _looks_like_code_junk(_name(e)) else ambiguous).append(e)
    return junk, ambiguous


def _name(row: dict) -> str:
    return row.get("name") or row.get("canonical_name") or "?"


def _rollback_docs(store, *, apply: bool, source: str = "documents.scan") -> int:
    """Undo the document ingestion: remove its events + facts (+ their vectors),
    and clear the scan ledger so a future clean pass re-ingests. Reversible via a
    JSON backup of the deleted rows."""
    import time as _t

    ev_ids = [i for i, ev in store.all_with_ids() if ev.source == source]
    print(f"\nDocument ingestion to roll back: {len(ev_ids)} events "
          f"(source={source!r}) and the facts extracted from them.")
    if not apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply --rollback-docs.\n")
        return 0

    removed = store.purge_source(source)
    # Drop the vectors for the deleted events + facts (best-effort).
    try:
        from app.services.memory import FACT_ID_OFFSET
        from app.vectorstore import get_vectorstore
        vids = list(removed["events"]) + [FACT_ID_OFFSET + f for f in removed["facts"]]
        get_vectorstore().delete_ids(vids)
    except Exception as exc:
        print(f"[rollback] vector cleanup skipped ({exc}).")
    # Clear the ledger so 'read my documents' re-ingests cleanly next time.
    try:
        from app.config import settings
        Path(settings.documents.state_path).unlink(missing_ok=True)
    except Exception:
        pass

    stamp = int(_t.time())
    backup = Path("data") / f"docs_rollback_backup_{stamp}.json"
    backup.write_text(json.dumps(removed, indent=2), encoding="utf-8")
    print(f"\nRolled back {len(removed['events'])} events + "
          f"{len(removed['facts'])} facts. Backup: {backup}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Purge junk graph nodes.")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is a dry run)")
    ap.add_argument("--docs", action="store_true",
                    help="also flag entities supported ONLY by document ingestion")
    ap.add_argument("--rollback-docs", action="store_true",
                    help="roll back the whole document ingestion: delete "
                         "documents.scan events + their facts (tasks/claims). "
                         "Re-run 'read my documents' (now code-safe) for a clean pass.")
    args = ap.parse_args()

    store = get_store()

    if args.rollback_docs:
        return _rollback_docs(store, apply=args.apply)

    people, entities = find_junk(store)
    ambiguous: list[dict] = []
    if args.docs:
        have = {e["id"] for e in entities}
        doc_junk, ambiguous = find_doc_only_entities(store)
        entities += [e for e in doc_junk if e["id"] not in have]

    print(f"\nJunk PEOPLE flagged ({len(people)}):")
    for p in people:
        print(f"  - {_name(p)!r}")
    print(f"\nJunk ENTITIES flagged ({len(entities)}):")
    for e in entities:
        print(f"  - {_name(e)!r}")

    if ambiguous:
        print(f"\nDoc-only but KEPT for manual review ({len(ambiguous)}) — "
              f"single capitalized brands named only in a document; delete by "
              f"hand if unwanted:")
        for e in ambiguous:
            print(f"  ? {_name(e)!r}")

    if not args.apply:
        print(f"\nDRY RUN — nothing deleted. Re-run with --apply to remove "
              f"{len(people)} people + {len(entities)} entities.\n")
        return 0

    # Back up every row we're about to delete, so the purge is reversible.
    stamp = int(time.time())
    backup = Path("data") / f"graph_cleanup_backup_{stamp}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    deleted = {"people": [], "entities": []}
    for p in people:
        row = store.delete_person(int(p["id"]))
        if row:
            deleted["people"].append(row)
    for e in entities:
        row = store.delete_entity(int(e["id"]))
        if row:
            deleted["entities"].append(row)
    backup.write_text(json.dumps(deleted, indent=2, ensure_ascii=False),
                      encoding="utf-8")
    print(f"\nDeleted {len(deleted['people'])} people + "
          f"{len(deleted['entities'])} entities. Backup: {backup}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
