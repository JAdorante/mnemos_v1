#!/usr/bin/env python
"""Operator-side invite-code -> API-key vending service (WS-D Tier 1).

Runs on the *operator's* infrastructure, not a tester's machine. It hands out
keys the operator pre-created — one revocable Anthropic workspace key per
tester — in exchange for a single-use invite code.

    pip install fastapi uvicorn
    QUILL_INVITE_DB=./invites.json \\
    uvicorn scripts.invite_service:app --host 0.0.0.0 --port 8090

`QUILL_INVITE_DB` is a JSON file the operator writes by hand:

    {
      "ABCD-EFGH-JKLM": {"provider": "anthropic", "key": "sk-ant-...",
                         "label": "Dana (Capital Connect)",
                         "expires_at": 1757000000},
      ...
    }

Redemption stamps `redeemed_at` back into that file, so a code works once.
Re-running the same installer on the same machine is the one case that would
hit a used code, so `--reissue` in the mint helper below exists for it.

Deliberately tiny and stateless-ish: this exists to remove a funnel stop for
~10 testers, not to become an identity service. If the hosted-cloud path is
confirmed, Tier 2 (a budget-enforcing proxy) replaces it — see
docs/key-vending.md for that schema.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no O/0, I/1 — codes are spoken


def db_path() -> Path:
    return Path(os.environ.get("QUILL_INVITE_DB", "invites.json"))


def load_db() -> dict[str, Any]:
    p = db_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_db(data: dict[str, Any]) -> None:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def mint_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


def redeem(code: str, *, now: float | None = None) -> tuple[int, dict[str, Any]]:
    """(http_status, body). Pure enough to unit-test without a server."""
    now = float(now if now is not None else time.time())
    code = (code or "").strip().upper()
    with _lock:
        data = load_db()
        row = data.get(code)
        if not isinstance(row, dict):
            return 404, {"detail": "That invite code was not found. "
                                   "Check it with whoever sent it."}
        if row.get("revoked"):
            return 410, {"detail": "That invite code was revoked."}
        expires = row.get("expires_at")
        if expires and now > float(expires):
            return 410, {"detail": "That invite code has expired. "
                                   "Ask for a new one."}
        if row.get("redeemed_at"):
            return 409, {"detail": "That invite code has already been used."}
        row["redeemed_at"] = now
        data[code] = row
        save_db(data)
    return 200, {"provider": row.get("provider", "anthropic"),
                 "key": row.get("key", ""),
                 "label": row.get("label", ""),
                 "expires_at": expires}


# --- HTTP ------------------------------------------------------------------
try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="Mnemos invite vending")

    @app.get("/health")
    def health() -> dict:
        data = load_db()
        return {"ok": True,
                "codes": len(data),
                "unredeemed": sum(1 for r in data.values()
                                  if isinstance(r, dict) and not r.get("redeemed_at"))}

    @app.post("/invite/redeem")
    async def redeem_route(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        status, payload = redeem(str((body or {}).get("code") or ""))
        return JSONResponse(payload, status_code=status)
except ImportError:  # pragma: no cover - the CLI half works without fastapi
    app = None


# --- operator CLI ----------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mint / list / revoke invite codes")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mint", help="create a code for a pre-made provider key")
    m.add_argument("key", help="the provider API key this code hands out")
    m.add_argument("--label", default="", help="who it is for (operator notes)")
    m.add_argument("--provider", default="anthropic")
    m.add_argument("--days", type=float, default=30.0)

    sub.add_parser("list", help="show codes and their state")

    r = sub.add_parser("revoke", help="disable a code")
    r.add_argument("code")

    s = sub.add_parser("reissue", help="let a code be redeemed again")
    s.add_argument("code")

    args = ap.parse_args(argv)
    data = load_db()

    if args.cmd == "mint":
        code = mint_code()
        data[code] = {"provider": args.provider, "key": args.key,
                      "label": args.label,
                      "expires_at": time.time() + args.days * 86400.0,
                      "created_at": time.time()}
        save_db(data)
        print(code)
        return 0
    if args.cmd == "list":
        for code, row in sorted(data.items()):
            state = ("revoked" if row.get("revoked")
                     else "used" if row.get("redeemed_at") else "open")
            print(f"{code}  {state:<8} {row.get('label', '')}")
        return 0
    if args.cmd in ("revoke", "reissue"):
        row = data.get(args.code)
        if not row:
            print(f"no such code: {args.code}")
            return 1
        if args.cmd == "revoke":
            row["revoked"] = True
        else:
            row.pop("redeemed_at", None)
            row.pop("revoked", None)
        save_db(data)
        print(f"{args.code} {'revoked' if args.cmd == 'revoke' else 'reissued'}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
