"""Seed a seat's own name vocabulary (`Store.seat_vocab`) from a terms file.

`name_quality`'s word lists hold generic words only. Words that are one
seat's own (a brand on its user's desktop, a coined project name) live in
that seat's store, so a seat that relied on them keeps behaving as before
once seeded. The terms file is kept outside the repo.

Terms file: one `<role> <term>` per line, `#` starts a comment. Roles:
`never_person_token` (no person's name holds the word), `nonperson_token`
(the word keeps a two-word name from reading as a person's), `corp_word`
(an organisation word, same effect), each one word; and three whole-name
roles, one or more words each: `junk_entity_name` (a name this seat's
cleanup hides on sight when nothing links to the row, `ambient_cleanup`),
`heading_junk_name` (a node name a page heading never names on this seat)
and `containment_stop_name` (a node name too generic on this seat to be
found inside a longer window-title segment), both read by
`context.surfaces`.

Dry run by default: the store is opened read-only, and the report lists the
terms that would be added and every decision that would change on the
store's own entity, person and people-mention names (person or not, person
shaped, may mint as an entity, junk by its name alone, never named from a
heading, may be found inside a title segment).
`--apply` first copies the database next to itself with SQLite's backup API
(safe with a live WAL; an earlier backup is never overwritten), then
writes. When every term is already there it writes nothing, backup
included; a read-only store is refused before anything is written. A
running seat picks the words up within `Store.SEAT_VOCAB_TTL_S`.

    python3 scripts/seed_seat_vocab.py --db data/quill.db --terms-file words.txt
    python3 scripts/seed_seat_vocab.py --db data/quill.db --terms-file words.txt --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.services import ambient_cleanup as ac  # noqa: E402
from app.services import name_quality as nq  # noqa: E402
from app.services.context import surfaces  # noqa: E402
from app.storage import Store  # noqa: E402


def parse_terms(text: str) -> list[tuple[str, str]]:
    """[(role, term)] from a terms file; ValueError on a bad line."""
    out: list[tuple[str, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            raise ValueError(f"line {i}: want '<role> <term>', got {line!r}")
        role = parts[0]
        if role not in Store.SEAT_VOCAB_SEED_ROLES:
            raise ValueError(f"line {i}: unknown role {role!r} "
                             f"(one of {', '.join(Store.SEAT_VOCAB_SEED_ROLES)})")
        whole = role in Store.SEAT_VOCAB_NAME_ROLES
        if not whole and len(parts) != 2:
            raise ValueError(f"line {i}: want '<role> <term>', got {line!r}")
        term = " ".join(parts[1:])
        t = Store.seat_vocab_term(term, role)
        if not t:
            raise ValueError(f"line {i}: {term!r} is not one word"
                             if not whole
                             else f"line {i}: {term!r} is too long a name")
        out.append((role, t))
    return list(dict.fromkeys(out))


def store_names(db: Path) -> list[tuple[str, str]]:
    """(name, kind) for every entity name and alias, person name and alias
    and people-mention text in the store. Kind is "person" for people."""
    con = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    def rows(sql: str) -> list:
        try:
            return con.execute(sql).fetchall()
        except sqlite3.OperationalError:
            return []

    def aliases(raw) -> list[str]:
        try:
            v = json.loads(raw or "[]")
        except Exception:
            return []
        return [a for a in v if isinstance(a, str)] if isinstance(v, list) else []

    out: list[tuple[str, str]] = []
    for r in rows("SELECT canonical_name, aliases, kind FROM entities"):
        kind = r["kind"] or ""
        out.append((r["canonical_name"] or "", kind))
        out.extend((a, kind) for a in aliases(r["aliases"]))
    for r in rows("SELECT a.alias, e.kind FROM entity_aliases a "
                  "LEFT JOIN entities e ON e.id = a.entity_id"):
        out.append((r["alias"] or "", r["kind"] or ""))
    for r in rows("SELECT canonical_name, aliases FROM people"):
        out.append((r["canonical_name"] or "", "person"))
        out.extend((a, "person") for a in aliases(r["aliases"]))
    for r in rows("SELECT raw_text, normalized_text FROM person_mentions"):
        out.append((r["raw_text"] or "", "person"))
        out.append((r["normalized_text"] or "", "person"))
    con.close()
    return [x for x in dict.fromkeys(out) if x[0].strip()]


class VocabView:
    """A stand-in store that answers `name_quality` with a given vocabulary
    and the real store's learned names, so the dry run can judge every name
    as it would be judged after seeding without writing anything."""

    def __init__(self, store: Store, vocab: dict[str, frozenset[str]]):
        self._store, self._vocab = store, vocab

    def seat_vocab(self) -> dict[str, frozenset[str]]:
        return self._vocab

    def is_seat_nonperson_name(self, name: str) -> bool:
        return self._store.is_seat_nonperson_name(name)


def decisions(names, store) -> dict[tuple[str, str], tuple[bool, ...]]:
    """Every decision the seat's vocabulary takes part in, on each name:
    could it be a person; is it a person-shaped entity name; may it mint
    under its own kind (a person's name: as a project); would cleanup hide
    an orphan entity row by this name alone (False for a person's name);
    is a node by this name never named from a page heading; may it be
    found inside a longer title segment (`context.surfaces`, which reads
    people and entities alike)."""
    out = {}
    for name, kind in names:
        k = "project" if kind == "person" else (kind or "idea")
        out[(name, kind)] = (
            nq.is_plausible_person(name, store=store),
            nq.is_person_shaped_entity_name(name, store=store),
            nq.should_mint_as_entity(name, k, store=store),
            kind != "person" and ac._junk_by_name(name, store=store),
            surfaces.is_heading_junk(name, store=store),
            surfaces.may_contain(name, store=store),
        )
    return out


def plan(db: Path, terms: list[tuple[str, str]]) -> dict:
    """What seeding `terms` into `db` adds and changes. Read-only."""
    store = Store(db_path=db, readonly=True)
    try:
        now = store.seat_vocab()
        after = {r: set(v) for r, v in now.items()}
        new = [(r, t) for r, t in terms if t not in now.get(r, frozenset())]
        for r, t in new:
            after.setdefault(r, set()).add(t)
        names = store_names(db)
        before_d = decisions(names, VocabView(store, now))
        after_d = decisions(
            names, VocabView(store, {r: frozenset(v) for r, v in after.items()}))
    finally:
        store.close()
    changed = [{"name": n, "kind": k, "before": list(before_d[(n, k)]),
                "after": list(after_d[(n, k)])}
               for (n, k) in names if before_d[(n, k)] != after_d[(n, k)]]
    return {"db": str(db), "terms": len(terms), "new": new,
            "present": len(terms) - len(new), "names": len(names),
            "changed": changed,
            "seat_terms": sum(len(v) for r, v in now.items()
                              if r in Store.SEAT_VOCAB_SEED_ROLES)}


def unwritable(db: Path) -> str | None:
    """Why `--apply` cannot write here (a read-only file, directory or
    mount), or None."""
    if not os.access(db, os.W_OK):
        return f"{db} is read-only"
    if not os.access(db.parent, os.W_OK):
        return f"{db.parent} is read-only (the backup and the WAL go there)"
    return None


def apply(db: Path, terms: list[tuple[str, str]], *, source: str) -> dict:
    """Back the database up next to itself, then add every term."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = db.with_name(f"{db.name}.bak-seat-vocab-{stamp}")
    n = 1
    while backup.exists():          # never overwrite an earlier backup
        backup = db.with_name(f"{db.name}.bak-seat-vocab-{stamp}-{n}")
        n += 1
    src = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    dst = sqlite3.connect(backup)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    # Only the newest few backups stay (each holds every name the store has).
    from app.services.entity_repair import prune_backups
    prune_backups(db)
    store = Store(db_path=db, audio_dir=db.parent / "audio")
    try:
        # One transaction: a failure part way seeds nothing, and `main`
        # says the seat was not seeded.
        added = store.add_seat_vocab_many(terms, source=source)
        total = sum(len(v) for r, v in store.seat_vocab().items()
                    if r in Store.SEAT_VOCAB_SEED_ROLES)
    finally:
        store.close()
    return {"backup": str(backup), "added": added, "seat_terms": total}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, type=Path,
                    help="the seat's quill.db")
    ap.add_argument("--terms-file", required=True, type=Path,
                    help="'<role> <term>' lines (keep it outside the repo)")
    ap.add_argument("--apply", action="store_true",
                    help="write the terms (default: dry run)")
    ap.add_argument("--source", choices=("migration", "user"),
                    default="migration",
                    help="recorded with each term (default: migration)")
    ap.add_argument("--json", action="store_true",
                    help="print the report as JSON")
    args = ap.parse_args(argv)

    if not args.db.is_file():
        print(f"no database at {args.db}", file=sys.stderr)
        return 2
    try:
        # utf-8-sig: a file saved by an editor that writes a BOM.
        terms = parse_terms(args.terms_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print(f"terms file: {exc}", file=sys.stderr)
        return 2

    try:
        report = plan(args.db, terms)
    except sqlite3.Error as exc:
        print(f"cannot read {args.db}: {exc}", file=sys.stderr)
        return 2
    if args.apply and not report["new"]:
        # Already seeded: no write and no backup (a second run must not
        # leave a backup that is no longer the state before seeding).
        report["applied"] = {"backup": None, "added": 0,
                             "seat_terms": report["seat_terms"]}
    elif args.apply:
        why = unwritable(args.db)
        if why:
            print(f"cannot seed: {why}; nothing written.", file=sys.stderr)
            return 2
        try:
            report["applied"] = apply(args.db, terms, source=args.source)
        except (OSError, sqlite3.Error) as exc:
            print(f"cannot seed {args.db}: {exc}", file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print(f"{report['db']}: {report['terms']} terms in the file, "
          f"{len(report['new'])} new, {report['present']} already there.")
    print(f"{report['names']} names in the store; "
          f"{len(report['changed'])} decisions change with the new terms:")
    for c in report["changed"]:
        print(f"  {c['name']!r} ({c['kind'] or '?'}): "
              f"person/person-shaped/mints/junk/heading-junk/contained "
              f"{c['before']} -> "
              f"{c['after']}")
    if args.apply and report["applied"]["backup"] is None:
        print(f"Nothing to write: every term is there "
              f"({report['seat_terms']} seat words in all).")
    elif args.apply:
        a = report["applied"]
        print(f"Applied: {a['added']} rows written ({a['seat_terms']} seat "
              f"words in all). Backup: {a['backup']}")
    else:
        print("DRY RUN: nothing written. Re-run with --apply to seed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
