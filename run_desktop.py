"""Try the guarded desktop driver by hand.

    python run_desktop.py                       # interactive REPL
    python run_desktop.py --new-project foo     # flagship: new project + open Cursor

Everything is confined to the jail (default ~/quill_desktop) and every mutating
action pauses for your approval. This is NOT wired into the browser agent yet —
it's a standalone harness to exercise the guardrails.
"""
from __future__ import annotations

import argparse

from desktop_agent import config as cfg
from desktop_agent.driver import DesktopDriver


def new_project(d: DesktopDriver, name: str, app: str = "cursor") -> None:
    """The motivating example: 'open Cursor and start a new project' — no pixel
    automation, just a jailed mkdir + an allowlisted app launch, each gated."""
    r = d.make_dir(name)
    if not r["ok"]:
        print(f"stopped: {r['detail']}")
        return
    d.launch_app(app, [r["path"]])


def repl(d: DesktopDriver) -> None:
    print("commands:  mk <name> | run <cmd...> | open <app> [name] | ls [name] | quit")
    while True:
        try:
            line = input("desktop> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        parts = line.split()
        cmd, rest = parts[0], parts[1:]
        if cmd in ("quit", "exit"):
            return
        if cmd == "mk" and rest:
            print(d.make_dir(rest[0]))
        elif cmd == "run" and rest:
            print(d.run_command(rest))
        elif cmd == "open" and rest:
            args = [d.make_dir(rest[1])["path"]] if len(rest) > 1 else []
            print(d.launch_app(rest[0], args))
        elif cmd == "ls":
            print(d.list_dir(rest[0] if rest else ""))
        else:
            print("?  mk <name> | run <cmd...> | open <app> [name] | ls [name] | quit")


def main() -> None:
    ap = argparse.ArgumentParser(description="Guarded desktop driver harness")
    ap.add_argument("--new-project", metavar="NAME",
                    help="create NAME and open it in Cursor")
    ap.add_argument("--app", default="cursor")
    args = ap.parse_args()

    d = DesktopDriver()
    print(f"jail: {cfg.JAIL_ROOT}   approval: {'on' if cfg.REQUIRE_APPROVAL else 'OFF'}")
    if args.new_project:
        new_project(d, args.new_project, args.app)
    else:
        repl(d)


if __name__ == "__main__":
    main()
