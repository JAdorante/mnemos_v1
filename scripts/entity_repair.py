"""Repair the entity rows a seat already has (`app/services/entity_repair.py`).

Dry run by default: the store is opened read-only, the plan (every visible
row: keep, re-kind, re-parent, merge, hide or drop, with its rule, reason
and evidence counts) is printed and written as JSON to the data dir
(`entity_repair_<UTC stamp>.json`). That file is the plan to review.

    python3 scripts/entity_repair.py --db data/quill.db
    python3 scripts/entity_repair.py --db data/quill.db --apply
    python3 scripts/entity_repair.py --db data/quill.db --plan-file data/entity_repair_<ts>.json --apply
    python3 scripts/entity_repair.py --db data/quill.db --auto
    python3 scripts/entity_repair.py --db data/quill.db --undo <run-id>

`--apply` makes a fresh plan and applies all of it (merges and deletions
included); with `--plan-file` it applies exactly that reviewed plan, and
refuses it (exit 2, nothing written) when the plan was made for another
database or a row it touches has changed since. `--auto` runs the automatic
path once (high-confidence, reversible changes; the rest goes to the Inbox).
Every write is preceded by a backup next to the database, made with
SQLite's backup API (safe while the app runs, WAL included); an earlier
backup is never overwritten, and only the newest few are kept
(QUILL_ENTITY_REPAIR_BACKUPS_KEEP, default 3). `--undo <run-id>` puts back
every logged field change of a run (`--undo-change <id>` one change); a
field changed since, by the user or anything else, is left as it is and
reported. Reports never overwrite one another: an undo writes
`entity_repair_undo_<ts>.json`, an automatic run `entity_repair_auto_<ts>.json`,
and a name already taken gets `-1`, `-2`... A name the user forgets later is
rewritten out of every report in the data dir (as a hash); backups keep it.

The data dir (where the assistant database with the filing registry and the
membership file live) is the database's own directory unless QUILL_DATA_DIR
says otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _args(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=None,
                    help="the seat's quill.db (default: the configured one)")
    ap.add_argument("--apply", action="store_true",
                    help="write the plan (default: dry run)")
    ap.add_argument("--plan-file", type=Path, default=None,
                    help="a reviewed plan to apply exactly (with --apply)")
    ap.add_argument("--auto", action="store_true",
                    help="run the automatic path once")
    ap.add_argument("--undo", metavar="RUN_ID", default=None,
                    help="undo every logged field change of a run")
    ap.add_argument("--undo-change", type=int, metavar="ID", default=None,
                    help="undo one logged change")
    ap.add_argument("--note-only", choices=("drop", "label"), default=None,
                    help="rows only assistant notes or screen text named: "
                         "drop them (default) or keep them as labels")
    ap.add_argument("--out", type=Path, default=None,
                    help="report path (default: <data dir>/"
                         "entity_repair_<ts>.json)")
    ap.add_argument("--json", action="store_true",
                    help="print the report as JSON")
    return ap.parse_args(argv)


def backup(db: Path, tag: str) -> Path:
    """Copy the database next to itself with SQLite's backup API, from a
    read-only connection (a live WAL is included). Only the newest few
    backups are kept (`entity_repair.prune_backups`)."""
    from app.services.entity_repair import prune_backups
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = db.with_name(f"{db.name}.bak-entity-repair-{tag}-{stamp}")
    n = 1
    while dest.exists():
        dest = db.with_name(f"{db.name}.bak-entity-repair-{tag}-{stamp}-{n}")
        n += 1
    src = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    prune_backups(db)
    return dest


def unique(path: Path) -> Path:
    """`path`, or `path` with -1, -2... before the suffix when it exists: a
    report never overwrites another (two runs in one second, a dry run and
    the undo after it)."""
    out, n = path, 1
    while out.exists():
        out = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return out


def unwritable(db: Path) -> str | None:
    if not os.access(db, os.W_OK):
        return f"{db} is read-only"
    if not os.access(db.parent, os.W_OK):
        return f"{db.parent} is read-only (the backup and the WAL go there)"
    return None


def describe(a: dict) -> str:
    act = a["action"]
    if act == "rekind":
        to = a.get("to_kind")
        src = a.get("kind_source")
        return f"{a.get('kind')} -> {to}" + (
            f" (source {src})" if to == a.get("kind") else "")
    if act == "reparent":
        return f"part of {a.get('parent_name')}" if a.get("parent_id") \
            else "taken out of its project"
    if act == "merge":
        return f"into {a.get('into_name')}"
    if act == "drop":
        return "delete" + (" + tombstone" if a.get("tombstone") else "") + (
            f" (person: {a['person']['name']})" if a.get("person") else "")
    if act == "hide":
        return "hide"
    return ""


def print_plan(p: dict) -> None:
    c = p["counts"]
    print(f"{p['db']['path']}: {p['db']['visible']} visible rows of "
          f"{p['db']['entities']}; note-only rows: {p['options']['note_only']}")
    print("actions: " + ", ".join(f"{k}={c[k]}" for k in
                                  ("keep", "rekind", "reparent", "merge",
                                   "hide", "drop")))
    print("  automatic path: " + (", ".join(
        f"{k}={v}" for k, v in sorted(c["auto"].items())) or "nothing"))
    print("  asked in the Inbox: " + (", ".join(
        f"{k}={v}" for k, v in sorted(c["review"].items())) or "nothing"))
    by = Counter()
    for a in p["actions"]:
        if a["action"] == "keep":
            continue
        by[a["rule"]] += 1
        print(f"  [{a['action']:8s} {a['route']:6s}] {a['name']!r} "
              f"({a.get('kind') or '?'}) {describe(a)}  <{a['rule']}: "
              f"{a['reason']}>")
    if by:
        print("by rule: " + ", ".join(f"{k}={v}" for k, v in
                                      sorted(by.items())))


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    if args.db is not None:
        db = args.db
        # The filing registry and the membership file live beside the db.
        os.environ.setdefault("QUILL_DATA_DIR", str(db.resolve().parent))
    from app.config import settings
    from app.services import entity_repair as er
    from app.storage import Store
    db = Path(args.db or settings.storage.db_path)
    if not db.is_file():
        print(f"no database at {db}", file=sys.stderr)
        return 2
    data_dir = Path(os.environ.get("QUILL_DATA_DIR") or db.parent)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    # Each kind of run names its own report (an undo never replaces the
    # plan it undoes), and none overwrites an earlier one.
    kind_ = ("undo_" if (args.undo or args.undo_change is not None)
             else "auto_" if args.auto else "")
    out = unique(args.out or (data_dir / f"entity_repair_{kind_}{stamp}.json"))
    writes = bool(args.apply or args.auto or args.undo or args.undo_change)
    report: dict = {"db": str(db), "at": time.time()}

    if writes:
        why = unwritable(db)
        if why:
            print(f"cannot write: {why}; nothing written.", file=sys.stderr)
            return 2

    if args.undo or args.undo_change is not None:
        # Undo writes too: the same backup first, before the store is opened
        # for writing (opening it may migrate the schema).
        try:
            bak = backup(db, "undo")
        except (OSError, sqlite3.Error) as exc:
            print(f"backup failed ({exc}); nothing written.", file=sys.stderr)
            return 1
        report["backup"] = str(bak)
        store = Store(db_path=db, audio_dir=db.parent / "audio")
        try:
            res = er.undo(store, run_id=args.undo,
                          change_id=args.undo_change)
            if res["undone"] or res["refused"]:
                # The operator's undo is a verdict like the route's.
                er.record_undo(store, run_id=args.undo,
                               change_id=args.undo_change, result=res,
                               actor="operator", via="script")
        finally:
            store.close()
        report["undo"] = res
        print(f"undone: {len(res['undone'])}, refused: "
              f"{len(res['refused'])}")
        for r in res["refused"]:
            print(f"  refused #{r['change_id']} {r.get('name')!r} "
                  f"{r['field']}: {r['why']}")
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False,
                                  default=str), encoding="utf-8")
        print(f"report: {out}")
        return 0

    if args.auto:
        try:
            bak = backup(db, "auto")
        except (OSError, sqlite3.Error) as exc:
            print(f"backup failed ({exc}); nothing written.", file=sys.stderr)
            return 1
        store = Store(db_path=db, audio_dir=db.parent / "audio")
        try:
            res = er.auto_run(store, force=True, note_only=args.note_only,
                              backup=str(bak))
            if res.get("ran"):
                er.record_apply(store, res, actor="operator", via="script")
        finally:
            store.close()
        report["auto"] = res
        if not res.get("ran"):
            print(f"automatic path did not run: {res.get('why')} "
                  f"{res.get('error') or ''}".strip(), file=sys.stderr)
            return 1
        print(f"automatic run {res['run_id']}: {res['summary']}; "
              f"{res['proposed']} left in the Inbox; backup {res['backup']}")
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False,
                                  default=str), encoding="utf-8")
        print(f"report: {out}")
        return 0

    if args.plan_file is not None:
        try:
            plan_data = json.loads(args.plan_file.read_text(encoding="utf-8"))
            plan_data = plan_data.get("plan", plan_data)
        except (OSError, ValueError) as exc:
            print(f"plan file: {exc}", file=sys.stderr)
            return 2
        # Every action of the file must still be what the rules decide on
        # the store as it is (a hand edit or new evidence refuses it).
        ro = Store(db_path=db, readonly=True)
        try:
            problems = er.check(ro, plan_data, verify=True)
        finally:
            ro.close()
        report["plan_file"] = str(args.plan_file)
        if problems:
            print(f"plan refused: {er.refusal_reason(problems)}; nothing "
                  "written.", file=sys.stderr)
            for p in problems[:20]:
                print(f"  entity {p['entity_id']}: {p['problem']} "
                      f"{p.get('fields') or ''}".rstrip(), file=sys.stderr)
            return 2
        if not args.apply:
            print("plan fits the store; re-run with --apply to write it.")
            return 0
    else:
        ro = Store(db_path=db, readonly=True)
        try:
            plan_data = er.plan(ro, note_only=args.note_only)
        finally:
            ro.close()
        report["plan"] = er.report_safe(plan_data)
        if not args.json:
            print_plan(plan_data)

    if args.apply:
        try:
            bak = backup(db, "reviewed")
        except (OSError, sqlite3.Error) as exc:
            print(f"backup failed ({exc}); nothing written.", file=sys.stderr)
            return 1
        store = Store(db_path=db, audio_dir=db.parent / "audio")
        try:
            res = er.apply(store, plan_data, mode="reviewed",
                           backup=str(bak))
            # The reviewed plan is the operator's approval of every action
            # in it: one decision for the run.
            er.record_apply(store, res, actor="operator", via="script",
                            plan_data=plan_data)
        except er.PlanRefused as exc:
            print(f"plan refused: {exc.reason}; nothing written.",
                  file=sys.stderr)
            return 2
        finally:
            store.close()
        report["applied"] = res
        report["backup"] = str(bak)
        print(f"applied run {res['run_id']}: {res['summary']}; "
              f"backup {bak}")
    else:
        print("DRY RUN: nothing written. Review the report, then apply it "
              "with --plan-file <report> --apply.")
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False,
                              default=str), encoding="utf-8")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
