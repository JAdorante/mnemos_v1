"""Operator CLI for the Org Record Service.

    python -m org_coordinator.records migrate
    python -m org_coordinator.records bootstrap --name "Acme" --admin-email a@acme.co
    python -m org_coordinator.records anchor [--org ORG_ID]
    python -m org_coordinator.records verify --org ORG_ID

Reads QUILL_ORG_DATABASE_URL. `bootstrap` prints the admin's invite code
once; the admin's Sparrow node redeems it (Records page -> Join an org).
"""
from __future__ import annotations

import argparse
import json
import sys
import time


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m org_coordinator.records")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    b = sub.add_parser("bootstrap")
    b.add_argument("--name", required=True)
    b.add_argument("--admin-email", required=True)
    b.add_argument("--rung", default="connected")
    a = sub.add_parser("anchor")
    a.add_argument("--org", required=True)
    a.add_argument("--day", default=time.strftime("%Y-%m-%d", time.gmtime()))
    v = sub.add_parser("verify")
    v.add_argument("--org", required=True)
    args = ap.parse_args(argv)

    from org_coordinator.repo import Database, migrate
    if args.cmd == "migrate":
        migrate()
        print("migrated to head")
        return 0
    db = Database(pool_size=2)
    try:
        if args.cmd == "bootstrap":
            from org_coordinator.records import service
            out = service.bootstrap_org(db, name=args.name,
                                        admin_email=args.admin_email,
                                        rung=args.rung)
            print(json.dumps(out, indent=2))
            return 0
        if args.cmd == "anchor":
            from org_coordinator.records import audit
            with db.tenant(args.org) as repo:
                out = audit.anchor(repo, day=args.day)
            print(json.dumps(out, indent=2))
            return 0
        if args.cmd == "verify":
            from org_coordinator.records import service
            out = service.verify_chain(db, args.org)
            print(json.dumps(out, indent=2))
            return 0 if out.get("ok") else 1
    finally:
        db.dispose()
    return 2


if __name__ == "__main__":
    sys.exit(main())
