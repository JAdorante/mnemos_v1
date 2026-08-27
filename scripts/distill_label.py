"""Human-verdict CLI for the distill trail — labeling path for TEXT rows.

Vision rows get labeled through fact review in the console (frame_path join).
Chat answers get one-tap 👍/👎/✏️ in `/ui` — every bubble, kept-local and
escalated alike (POST /chat/outcome → set_user_outcome by row id). This CLI
remains the offline / non-chat path (extract / reflect / batch review).

Labels feed the few-shot learning loop (services/few_shot.py): accepted/edited
rows become worked examples for the local model; rejected rows are excluded.

`review` is the bulk path: one row at a time, local answer next to the
parent's, one keystroke per verdict. Use it to work a backlog down — labeling
row-by-row with `label` is fine for a handful and miserable for a hundred.

Usage:
    python scripts/distill_label.py list              # unlabeled text rows
    python scripts/distill_label.py list --all        # every text row
    python scripts/distill_label.py show <id-prefix>  # full row detail
    python scripts/distill_label.py label <id-prefix> accepted
    python scripts/distill_label.py label <id-prefix> rejected
    python scripts/distill_label.py label <id-prefix> edited --text "corrected answer"
    python scripts/distill_label.py review            # interactive backlog queue
    python scripts/distill_label.py review --task chat --limit 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _rows() -> list[dict]:
    from app.config import settings
    path = Path(settings.escalate_log.path)
    if not path.is_file():
        return []
    out = []
    # utf-8-sig: tolerate a BOM from hand-edited/PowerShell-written files.
    for ln in path.read_text(encoding="utf-8-sig").splitlines():
        if not ln.strip():
            continue
        try:
            row = json.loads(ln)
        except Exception:
            continue
        if row.get("modality") == "text":
            out.append(row)
    return out


def _clip(text: str, n: int) -> str:
    t = (text or "").replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"


def _resolve(rows: list[dict], prefix: str) -> dict:
    hits = [r for r in rows if str(r.get("id") or "").startswith(prefix)]
    if not hits:
        sys.exit(f"no text row with id starting {prefix!r}")
    if len(hits) > 1:
        sys.exit(f"id prefix {prefix!r} is ambiguous ({len(hits)} rows) — use more characters")
    return hits[0]


def cmd_list(args: argparse.Namespace) -> None:
    rows = _rows()
    if not args.all:
        rows = [r for r in rows if r.get("user_outcome") in (None, "unknown")]
    if not rows:
        print("no matching text rows.")
        return
    for r in rows:
        meta = r.get("meta") or {}
        # The answer the user actually saw: parent when one was called,
        # else the kept local answer (local_kept / parent_failed rows).
        shown = ((r.get("parent") or {}).get("text")
                 or (r.get("local") or {}).get("text") or "")
        print(f"{str(r.get('id') or '--------')[:8]}  {r.get('task', '?'):<10}"
              f"{r.get('reason', '?'):<18}{r.get('user_outcome', '?'):<9}"
              f"{_clip(meta.get('prompt_head') or '', 44):<46}"
              f"-> {_clip(shown, 44)}")


def cmd_show(args: argparse.Namespace) -> None:
    row = _resolve(_rows(), args.id)
    print(json.dumps(row, indent=2, ensure_ascii=False))


def cmd_label(args: argparse.Namespace) -> None:
    if args.outcome == "edited" and not args.text:
        sys.exit("edited needs --text \"the corrected answer\" — that text is the "
                 "training target, so don't leave it blank")
    row = _resolve(_rows(), args.id)
    from app.services.escalate_log import escalate_log
    ok = escalate_log.set_user_outcome(args.outcome, row_id=row["id"],
                                       edited_text=args.text)
    if not ok:
        sys.exit("label failed (see [escalate_log] message above)")
    print(f"{row['id'][:8]} ({row.get('task')}) -> {args.outcome}"
          + (f" with corrected text ({len(args.text)} chars)" if args.text else ""))


# --------------------------- review queue ------------------------------------
# Kept-local rows are the SCARCE label class. An escalation that a human
# accepts trains the router as "local failed" (label 0), and escalations are
# most of the trail — so a queue in file order produces a wildly imbalanced
# training set. A 👍 on a kept-local answer is the only cheap source of
# label 1, so those rows go first. `parent_failed` counts too: the user saw
# and lived with the local answer there.
_PRIORITY = ("local_kept", "parent_failed")

# Speculative rows are answers to questions the user never asked — pre-
# generated for a prediction, never displayed. Nobody has the context to judge
# whether one is right, and they are a different input distribution from real
# traffic, so they are OUT of the queue unless asked for. They are also the
# bulk of the backlog, which is exactly why they must not be the default: 65
# rows of unaskable questions ahead of the ones that matter is how a review
# session gets abandoned.
_SPECULATIVE = "speculative_local_only"


def queue_rows(rows: list[dict], *, task: str | None = None,
               reason: str | None = None, limit: int = 0,
               speculative: bool = False) -> list[dict]:
    """The review queue: unlabeled text rows, scarcest label class first.

    Pure so the ordering is testable; the interactive loop below just walks
    whatever this returns."""
    out = [r for r in rows if r.get("user_outcome") in (None, "unknown")]
    if not speculative and reason != _SPECULATIVE:
        out = [r for r in out if r.get("reason") != _SPECULATIVE]
    if task:
        out = [r for r in out if r.get("task") == task]
    if reason:
        out = [r for r in out if r.get("reason") == reason]
    # Stable within each class: oldest first, so a resumed session continues
    # where the last one stopped rather than reshuffling.
    out.sort(key=lambda r: (
        0 if r.get("reason") in _PRIORITY else 1, float(r.get("time") or 0)))
    return out[:limit] if limit else out


def _answer_shown(row: dict) -> str:
    """What the user actually saw: the parent's answer when one was called,
    else the kept local answer."""
    return ((row.get("parent") or {}).get("text")
            or (row.get("local") or {}).get("text") or "")


def _question(row: dict) -> str:
    meta = row.get("meta") or {}
    msgs = meta.get("messages") or []
    for m in reversed(msgs):
        if m.get("role", "user") == "user" and m.get("text"):
            return str(m["text"])
    return str(meta.get("prompt_head") or "")


def _edit_text(seed: str) -> str | None:
    """Open $EDITOR pre-filled with the answer so a correction is a few
    keystrokes, not a retype. Editing is the whole point of this queue —
    `edited` rows are the only ones that carry the model's failure next to
    the fix — so it has to be cheaper than accepting is."""
    import subprocess
    import tempfile
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        print("  (no $EDITOR set — paste the corrected answer, end with a "
              "blank line)")
        lines: list[str] = []
        while True:
            try:
                ln = input("  | ")
            except EOFError:
                break
            if not ln.strip():
                break
            lines.append(ln)
        return "\n".join(lines).strip() or None
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(seed or "")
        tmp = fh.name
    try:
        subprocess.call([editor, tmp])
        text = Path(tmp).read_text(encoding="utf-8").strip()
    finally:
        try:
            Path(tmp).unlink()
        except OSError:
            pass
    return text or None


def cmd_review(args: argparse.Namespace) -> None:
    from app.services.escalate_log import escalate_log

    all_rows = _rows()
    queue = queue_rows(all_rows, task=args.task, reason=args.reason,
                       limit=args.limit, speculative=args.speculative)
    if not queue:
        print("nothing unlabeled matches — the backlog is clear.")
        return
    labeled_before = sum(1 for r in all_rows
                         if r.get("user_outcome") in ("accepted", "edited"))
    print(f"{len(queue)} row(s) to review. "
          f"[a]ccept  [e]dit  [r]eject  [s]kip  [q]uit\n"
          f"Prefer EDIT over accept whenever the answer is imperfect — an "
          f"edited row teaches the model what it got wrong, an accepted one "
          f"only shows a target.\n")
    counts = {"accepted": 0, "edited": 0, "rejected": 0, "skipped": 0}
    for i, row in enumerate(queue, 1):
        shown = _answer_shown(row)
        local = (row.get("local") or {}).get("text") or ""
        print("=" * 72)
        print(f"[{i}/{len(queue)}] {str(row.get('id'))[:8]}  "
              f"task={row.get('task')}  reason={row.get('reason')}")
        print(f"\nQ: {_clip(_question(row), 400)}\n")
        # Only worth showing both sides when they differ — on a kept-local row
        # the "answer" IS the local text and printing it twice is noise.
        if local and shown and local.strip() != shown.strip():
            print(f"LOCAL  : {_clip(local, 400)}\n")
            print(f"PARENT : {_clip(shown, 400)}\n")
        else:
            print(f"ANSWER : {_clip(shown, 400)}\n")
        try:
            choice = (input("  verdict [a/e/r/s/q]: ").strip().lower()
                      or "s")[:1]
        except (EOFError, KeyboardInterrupt):
            print("\nstopped.")
            break
        if choice == "q":
            break
        if choice == "s":
            counts["skipped"] += 1
            continue
        outcome = {"a": "accepted", "e": "edited", "r": "rejected"}.get(choice)
        if outcome is None:
            print("  ? unrecognized — skipping")
            counts["skipped"] += 1
            continue
        text = None
        if outcome == "edited":
            text = _edit_text(shown)
            if not text:
                print("  (empty correction — skipped, nothing was written)")
                counts["skipped"] += 1
                continue
            if text.strip() == (shown or "").strip():
                # Unchanged means the answer was right; recording it as an
                # edit would put an identical pair in the contrastive store.
                print("  (unchanged — recording as accepted instead)")
                outcome, text = "accepted", None
        if escalate_log.set_user_outcome(outcome, row_id=row["id"],
                                         edited_text=text):
            counts[outcome] += 1
            print(f"  -> {outcome}")
        else:
            print("  ! write failed (see [escalate_log] above)")
    done = counts["accepted"] + counts["edited"] + counts["rejected"]
    print("\n" + "=" * 72)
    print(f"reviewed {done}  (accepted {counts['accepted']}, "
          f"edited {counts['edited']}, rejected {counts['rejected']}, "
          f"skipped {counts['skipped']})")
    _progress(labeled_before + counts["accepted"] + counts["edited"])


def _progress(labeled: int) -> None:
    """Where this leaves the two gates that labels feed."""
    try:
        from app.config import settings
        need = int(settings.router.min_labels)
    except Exception:
        need = 50
    left = max(0, need - labeled)
    print(f"labels usable for training: {labeled}")
    print(f"escalation router first fit at {need}: "
          + (f"{left} to go" if left else "READY — it fits on the next "
             "idle tick"))
    print("bench_bakeoff needs ~20 per task before its numbers separate models.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="list text rows (unlabeled by default)")
    p_list.add_argument("--all", action="store_true", help="include labeled rows")
    p_list.set_defaults(fn=cmd_list)
    p_show = sub.add_parser("show", help="print one row in full")
    p_show.add_argument("id", help="row id prefix (from list)")
    p_show.set_defaults(fn=cmd_show)
    p_label = sub.add_parser("label", help="set the human verdict on a row")
    p_label.add_argument("id", help="row id prefix (from list)")
    p_label.add_argument("outcome", choices=["accepted", "rejected", "edited"])
    p_label.add_argument("--text", default=None,
                         help="the corrected answer (required for edited)")
    p_label.set_defaults(fn=cmd_label)
    p_rev = sub.add_parser("review",
                           help="interactive backlog queue (bulk labeling)")
    p_rev.add_argument("--task", default=None, help="only this task")
    p_rev.add_argument("--reason", default=None, help="only this escalate reason")
    p_rev.add_argument("--limit", type=int, default=0, help="stop after N rows")
    p_rev.add_argument("--speculative", action="store_true",
                       help="include pre-generated answers to questions that "
                            "were never actually asked (excluded by default)")
    p_rev.set_defaults(fn=cmd_review)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
