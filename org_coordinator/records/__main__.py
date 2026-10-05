"""Operator CLI for the Org Record Service.

    python -m org_coordinator.records migrate
    python -m org_coordinator.records bootstrap --name "Acme" --admin-email a@acme.co
    python -m org_coordinator.records anchor [--org ORG_ID]
    python -m org_coordinator.records verify --org ORG_ID
    python -m org_coordinator.records worker [--once]     # write-back sync
    python -m org_coordinator.records drift [--org ORG_ID] # nightly sweep

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
    w = sub.add_parser("worker")
    w.add_argument("--once", action="store_true")
    w.add_argument("--idle-s", type=float, default=5.0)
    d = sub.add_parser("drift")
    d.add_argument("--org", default=None)
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
        if args.cmd == "worker":
            from org_coordinator.records import sync
            return _worker(db, sync, once=args.once, idle_s=args.idle_s)
        if args.cmd == "drift":
            from org_coordinator.records import sync
            orgs = [args.org] if args.org else db.org_ids()
            print(json.dumps({o: sync.drift_sweep(db, o) for o in orgs},
                             indent=2))
            return 0
        if args.cmd == "verify":
            from org_coordinator.records import service
            out = service.verify_chain(db, args.org)
            print(json.dumps(out, indent=2))
            return 0 if out.get("ok") else 1
    finally:
        db.dispose()
    return 2


def _worker(db, sync, *, once: bool, idle_s: float) -> int:
    """Drain due sync jobs forever; run the drift sweep once per UTC day."""
    last_drift_day = None
    while True:
        counts = sync.drain(db)
        if counts:
            print(json.dumps({"at": time.time(), "jobs": counts}))
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if last_drift_day != day and time.gmtime().tm_hour >= 3:
            for org in db.org_ids():
                print(json.dumps({"drift": org, **sync.drift_sweep(db, org)}))
            last_drift_day = day
        if once:
            return 0
        time.sleep(idle_s)


if __name__ == "__main__":
    sys.exit(main())
