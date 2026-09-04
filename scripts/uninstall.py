#!/usr/bin/env python
"""Erase Sparrow from this machine, offline (pilot blocker: clean uninstall).

The in-app "Delete everything" button is the path most testers will take. This
is the one for the tester who has already stopped trusting the app enough to
open it — and for the case the button half-succeeds because a database was
still in use. Same deletion code (:mod:`app.services.wipe`), no server needed.

    python scripts/uninstall.py                 # asks, then deletes captured data
    python scripts/uninstall.py --yes           # no prompt (support calls)
    python scripts/uninstall.py --yes --all     # also the key, config and venv

What it removes, in order: every capture directory Sparrow writes
(``data/``, ``sessions/``, ``desktop_agent/sessions/``), then optionally the
credentials and the virtualenv. What it never removes is the folder itself —
the tester drags that to the trash, and seeing it go is the point.

It refuses while the server is up rather than racing it: a live capture thread
recreates ``quill.db`` moments after the delete, which looks exactly like the
uninstall not working. ``--force`` overrides.

A receipt lands beside the install either way, so the operator can answer
"did it really all go?" from a file rather than from memory.

Exit codes: 0 deleted, 1 refused or incomplete, 2 usage.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def server_is_up(host: str, port: int, timeout: float = 1.5) -> bool:
    """True when something answers /health — i.e. Sparrow is probably running."""
    from urllib.error import URLError
    from urllib.request import urlopen
    try:
        with urlopen(f"http://{host}:{port}/health", timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 200)) < 500
    except URLError:
        return False
    except Exception:
        return False


def _remove_venv(root: Path) -> str | None:
    """Delete the virtualenv. Last, and never while running out of it."""
    venv = root / ".venv"
    if not venv.is_dir():
        return None
    running_from = Path(sys.prefix).resolve()
    if running_from == venv.resolve():
        return (f"skipped {venv} — this interpreter lives inside it; "
                f"delete the folder manually")
    try:
        shutil.rmtree(venv)
        return f"removed {venv}"
    except Exception as exc:
        return f"could not remove {venv}: {exc}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Delete everything Sparrow captured on this machine.")
    ap.add_argument("--yes", action="store_true",
                    help="skip the typed confirmation")
    ap.add_argument("--all", action="store_true",
                    help="also remove the API key, shipped config and .venv")
    ap.add_argument("--credentials", action="store_true",
                    help="also remove .env / .credentials.env")
    ap.add_argument("--force", action="store_true",
                    help="skip the running-server probe")
    ap.add_argument("--host", default=os.environ.get("QUILL_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("QUILL_PORT", "8000")))
    args = ap.parse_args(argv[1:])

    from app.services import wipe

    if not args.force and server_is_up(args.host, args.port):
        print(f"Sparrow is still running on {args.host}:{args.port}.\n"
              "Close it first (the Sparrow window / start.bat), then re-run.\n"
              "Deleting while capture is writing leaves a fresh database behind.",
              file=sys.stderr)
        return 1

    before = wipe.preview()
    print("This permanently deletes everything Sparrow captured here:\n")
    for row in before["targets"]:
        mark = "  " if row["exists"] else "  (nothing) "
        print(f"{mark}{row['label']}\n      {row['path']}"
              f"  —  {row['human']} in {row['files']} file(s)")
    creds = bool(args.credentials or args.all)
    if creds and before["credentials_present"]:
        print("\n  Your API key / .env:")
        for c in before["credentials_present"]:
            print(f"      {c}")
    print(f"\nTotal: {before['total_human']} in {before['total_files']} file(s).")
    print("There is no server-side copy — this is all of it.")
    print(f"A receipt will be written to {before['receipt_dir']}.\n")

    if not args.yes:
        try:
            typed = input(f"Type {wipe.CONFIRM_PHRASE} to confirm (anything else "
                          f"cancels): ")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled. Nothing was deleted.")
            return 1
    else:
        typed = wipe.CONFIRM_PHRASE

    try:
        receipt = wipe.wipe(typed, full=bool(args.all), credentials=creds)
    except wipe.WipeRefused as exc:
        print(f"Cancelled: {exc}", file=sys.stderr)
        return 1

    for row in receipt["targets"]:
        print(f"  cleared {row['path']} ({row['removed']} item(s))")
    for note in receipt.get("credentials_removed", []):
        print(f"  removed {note}")
    if args.all:
        msg = _remove_venv(wipe.install_root())
        if msg:
            print(f"  {msg}")
    if receipt.get("receipt_path"):
        print(f"\nReceipt: {receipt['receipt_path']}")
    if not receipt["complete"]:
        print("\nSome paths could not be removed:", file=sys.stderr)
        for f in receipt["failures"]:
            print(f"  {f}", file=sys.stderr)
        print("Close anything still using them and re-run.", file=sys.stderr)
        return 1
    print("\nDone. Delete this folder to finish removing Sparrow.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
