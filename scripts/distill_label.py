"""Human-verdict CLI for the distill trail — labeling path for TEXT rows.

Vision rows get labeled through fact review in the console (frame_path join).
Chat answers get one-tap 👍/👎/✏️ in `/ui` — every bubble, kept-local and
escalated alike (POST /chat/outcome → set_user_outcome by row id). This CLI
remains the offline / non-chat path (extract / reflect / batch review).

Labels feed the few-shot learning loop (services/few_shot.py): accepted/edited
rows become worked examples for the local model; rejected rows are excluded.

Usage:
    python scripts/distill_label.py list              # unlabeled text rows
    python scripts/distill_label.py list --all        # every text row
    python scripts/distill_label.py show <id-prefix>  # full row detail
    python scripts/distill_label.py label <id-prefix> accepted
    python scripts/distill_label.py label <id-prefix> rejected
    python scripts/distill_label.py label <id-prefix> edited --text "corrected answer"
"""
from __future__ import annotations

import argparse
import json
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
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
