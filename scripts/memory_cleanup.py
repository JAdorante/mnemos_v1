"""One-time (safely re-runnable) hygiene sweep over the EXISTING facts store.

The write-time gates (services/fact_gate.py) keep the store clean going
FORWARD; this script applies the same standards RETROACTIVELY to facts stored
before hygiene existed:

  pass 1  empty text                  -> archive
  pass 2  confidence below the floor  -> archive (human-approved/edited exempt)
  pass 3  exact duplicates (normalized text, same kind)
                                      -> keep one (reviewed > newest), the
                                         rest superseded by the keeper
  pass 4  semantic near-dups + stale-vs-correction pairs (vector probe; the
          local model adjudicates the ambiguous band)
                                      -> older row superseded by the newer

Nothing is deleted: archived/superseded rows keep full provenance and can be
inspected (or un-marked) in SQLite; they simply stop surfacing in retrieval,
reflection, and the person graph. A timestamped .bak of the DB is written
before any change.

Dry-run by default — prints the full plan. Apply with --write.
Run while the app is STOPPED (the sweep needs the DB write lock).

    python scripts/memory_cleanup.py            # plan only
    python scripts/memory_cleanup.py --write    # do it

Generic code: thresholds come from the same FactHygieneConfig as the live gate.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_WS = re.compile(r"[\W_]+")


def _norm(text: str) -> str:
    return _WS.sub(" ", (text or "").lower()).strip()


def _review_rank(f: dict) -> int:
    """Keeper preference: human-reviewed beats unreviewed."""
    return 0 if f.get("review") in ("approved", "edited") else 1


def _load_active(store) -> list[dict]:
    rows = store.facts_since(0.0, limit=1_000_000, exclude_dismissed=True,
                             exclude_superseded=True)
    rows.sort(key=lambda f: -(f.get("updated_at") or f.get("extracted_at") or 0))
    return rows


def _snapshot(db_path: Path) -> Path:
    """Consistent SQLite backup (not a raw file copy — WAL-safe)."""
    bak = db_path.with_suffix(f".bak-{time.strftime('%Y%m%d-%H%M%S')}.db")
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(bak))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return bak


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true",
                    help="apply the plan (default: dry-run report)")
    ap.add_argument("--min-conf", type=float, default=None,
                    help="override QUILL_FACT_MIN_CONF for pass 2")
    ap.add_argument("--no-adjudicate", action="store_true",
                    help="skip the local-model band in pass 4 (pure cosine only)")
    ap.add_argument("--max-adjudications", type=int, default=300,
                    help="cap on local-model calls in pass 4")
    args = ap.parse_args()

    from app.config import settings
    from app.storage import get_store

    cfg = settings.facts
    min_conf = cfg.min_conf if args.min_conf is None else args.min_conf
    store = get_store()
    facts = _load_active(store)
    print(f"[cleanup] {len(facts)} active facts loaded "
          f"(floor={min_conf}, dup>={cfg.auto_dup_sim}, "
          f"band>={cfg.adjudicate_sim})")

    now = time.time()
    archive: list[tuple[int, str]] = []          # (fact_id, reason)
    supersede: list[tuple[int, int, str]] = []   # (old_id, keeper_id, reason)
    gone: set[int] = set()

    # --- pass 1+2: empties and below-floor confidence ---------------------
    survivors: list[dict] = []
    for f in facts:
        fid, text = f["fact_id"], (f.get("text") or "").strip()
        conf = f.get("confidence")
        if not text:
            archive.append((fid, "empty text"))
            gone.add(fid)
        elif (min_conf > 0 and conf is not None and conf < min_conf
              and _review_rank(f) == 1):
            archive.append((fid, f"confidence {conf:.2f} < {min_conf}"))
            gone.add(fid)
        else:
            survivors.append(f)

    # --- pass 3: exact duplicates (normalized text, same kind) ------------
    by_key: dict[tuple[str, str], list[dict]] = {}
    for f in survivors:
        by_key.setdefault((f["kind"], _norm(f.get("text") or "")), []).append(f)
    for group in by_key.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda f: (_review_rank(f),
                                  -(f.get("updated_at") or 0)))
        keeper = group[0]
        for dup in group[1:]:
            supersede.append((dup["fact_id"], keeper["fact_id"],
                              "exact duplicate"))
            gone.add(dup["fact_id"])

    # --- pass 4: semantic near-dups + stale-vs-correction pairs -----------
    # Probe the shared vector index per fact (newest first, so the freshest
    # statement always wins its cluster). The ambiguous band goes to the same
    # local-model adjudicator the live gate uses.
    from app.services.fact_gate import _adjudicate
    from app.services.memory import memory

    probes = adjs = 0
    sem_available = True
    for f in survivors:
        fid = f["fact_id"]
        if fid in gone:
            continue
        kind, text = f["kind"], f.get("text") or ""
        hits = memory.similar_facts(kind, text, k=6)
        if probes == 0 and not hits:
            # first probe empty could just mean no neighbours; check the index
            if memory._ensure_vectors() is None:
                sem_available = False
                print("[cleanup] vector index unavailable — pass 4 skipped.")
                break
        probes += 1
        if probes % 100 == 0:
            print(f"[cleanup]   probed {probes} facts "
                  f"({len(supersede)} merges so far) ...")
        for hid, score, old_text in hits:
            # only ever fold OLDER rows into this newer one (list is
            # newest-first, so anything still probe-able later is older)
            if hid == fid or hid in gone:
                continue
            if score >= cfg.auto_dup_sim:
                supersede.append((hid, fid, f"near-dup cos {score:.2f}"))
                gone.add(hid)
            elif (score >= cfg.adjudicate_sim and not args.no_adjudicate
                  and adjs < args.max_adjudications):
                adjs += 1
                rel = _adjudicate(kind, old_text, text)
                if rel in ("duplicate", "update"):
                    supersede.append((hid, fid, f"adjudicated {rel} "
                                                f"(cos {score:.2f})"))
                    gone.add(hid)
    if sem_available:
        print(f"[cleanup] pass 4: {probes} probed, {adjs} adjudicated "
              f"by the local model.")

    # --- report ------------------------------------------------------------
    fmap = {f["fact_id"]: f for f in facts}

    def _line(fid: int) -> str:
        f = fmap.get(fid, {})
        return f"#{fid} [{f.get('kind', '?')}] {(f.get('text') or '')[:70]}"

    print(f"\n[cleanup] PLAN — archive {len(archive)}, "
          f"supersede {len(supersede)}, "
          f"keep {len(facts) - len(gone)} of {len(facts)}")
    for fid, reason in archive[:15]:
        print(f"  archive   {_line(fid)}   <- {reason}")
    if len(archive) > 15:
        print(f"  ... and {len(archive) - 15} more")
    for old, new, reason in supersede[:15]:
        print(f"  supersede {_line(old)}   -> kept #{new}   <- {reason}")
    if len(supersede) > 15:
        print(f"  ... and {len(supersede) - 15} more")

    if not args.write:
        print("\n[cleanup] dry-run — nothing changed. "
              "Re-run with --write to apply.")
        return 0

    bak = _snapshot(Path(store.db_path))
    print(f"\n[cleanup] backup -> {bak}")
    n_a = sum(store.archive_fact(fid, now) for fid, _ in archive)
    n_s = sum(store.supersede_fact(old, new, now)
              for old, new, _ in supersede)
    print(f"[cleanup] applied: {n_a} archived, {n_s} superseded. "
          f"{len(facts) - len(gone)} facts remain active.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
