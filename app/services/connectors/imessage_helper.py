"""The Mac helper for hosted seats: read new texts, push them to your seat.

Served by the seat as a zipapp (imessage_relay.build_helper) and run with the
Python that ships with macOS. It keeps its token and its place in
~/.sparrow/imessage.json, advances that place only after the seat accepts a
batch, and never writes to Messages.

  pair <url> <code>   connect this Mac, send what's waiting, stay running
  run                 keep sending (what the background job runs)
  once                send what's waiting now, then exit
  status              show what this Mac is connected to
  remove              stop the background job and forget the connection
"""
from __future__ import annotations

import json
import os
import platform
import plistlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from app.services.connectors import imessage_reader as reader

BATCH = 200
INTERVAL_S = 300.0
LABEL = "com.sparrow.imessage-helper"
# Cloudflare refuses urllib's default user agent, and the seats sit behind it.
USER_AGENT = "SparrowImessageHelper/1"
FDA_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


class Revoked(RuntimeError):
    """The seat no longer accepts this Mac's token."""


def _home() -> Path:
    raw = (os.environ.get("QUILL_IMESSAGE_HELPER_HOME") or "").strip()
    return Path(raw) if raw else Path.home() / ".sparrow"


def _config_path() -> Path:
    return _home() / "imessage.json"


def _load_config() -> dict:
    try:
        data = json.loads(_config_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_config(data: dict) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _safe_base(base: str) -> bool:
    """Texts and the token only travel over TLS; plain http is for a seat on
    this same machine."""
    u = urlparse(base)
    return u.scheme == "https" or (
        u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"))


def _post(base: str, path: str, payload: dict, token: str = "") -> tuple:
    req = urllib.request.Request(
        base.rstrip("/") + path, data=json.dumps(payload).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json",
                                "User-Agent": USER_AGENT})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def send_waiting(cfg: dict, *, now: float | None = None) -> int:
    """Push batches until caught up. Returns how many texts the seat kept. A
    paused seat (403) keeps the place, so nothing is lost while it's paused."""
    total = 0
    while True:
        items, cursor, more = reader.fetch_batch(
            cfg.get("cursor"), float(now if now is not None else time.time()), BATCH)
        code, reply = _post(cfg["base"], "/imessage-relay/push", {"items": items},
                            cfg["token"])
        # Only a 401 is the seat saying no. A 404 or 5xx can be a tunnel having
        # a bad minute, and giving up on one would strand the Mac for good.
        if code == 401:
            raise Revoked()
        if code == 403:
            print("Your seat has paused iMessage; nothing was sent.", flush=True)
            return total
        if code != 200:
            raise OSError(f"the seat answered {code}")
        total += int(reply.get("landed") or 0)
        cfg["cursor"] = cursor
        _save_config(cfg)
        if not more:
            return total


def _report_no_access(cfg: dict) -> None:
    """So the webapp can say why texts stopped. Best effort."""
    try:
        _post(cfg["base"], "/imessage-relay/push",
              {"items": [], "error": "no_access"}, cfg["token"])
    except (OSError, ValueError):
        pass


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launch_agent(python: str, pyz: str) -> dict:
    return {"Label": LABEL, "ProgramArguments": [python, pyz, "run"],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 60}


def _python_path() -> str:
    return os.path.realpath(sys.executable)


def _install_background() -> None:
    pyz = os.path.realpath(sys.argv[0])
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(launch_agent(_python_path(), pyz), fh)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(path)],
                   capture_output=True, check=False)
    subprocess.run(["launchctl", "bootstrap", domain, str(path)],
                   capture_output=True, check=False)


def _remove_background() -> None:
    path = _plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)],
                   capture_output=True, check=False)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def cmd_pair(base: str, code: str, *, background: bool = True) -> int:
    base = base.rstrip("/")
    if not _safe_base(base):
        print("Refusing to send texts to a non-https address.", file=sys.stderr)
        return 2
    # Before claiming: a failure here shouldn't burn the single-use code.
    try:
        reader.open_messages().close()
    except reader.AccessError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    device = socket.gethostname().split(".")[0] or platform.node() or "Mac"
    try:
        status, reply = _post(base, "/imessage-relay/claim",
                              {"code": code, "device": device})
    except (OSError, ValueError) as exc:
        print(f"Couldn't reach your seat: {exc}", file=sys.stderr)
        return 4
    if status != 200 or not reply.get("token"):
        print("That code didn't work. It may have expired or been used; press "
              "Connect in Sparrow to get a new one.", file=sys.stderr)
        return 5
    cfg = {"base": base, "token": reply["token"], "cursor": {}}
    _save_config(cfg)
    print(f"Connected. Sent {send_waiting(cfg)} texts to Sparrow.", flush=True)
    if background and sys.platform == "darwin":
        _install_background()
        print("\nOne more step so this keeps running after you close Terminal: "
              "in the Full Disk Access list that's opening, press +, press "
              "Cmd-Shift-G, and add:\n\n    " + _python_path() + "\n")
        subprocess.run(["open", FDA_SETTINGS], capture_output=True, check=False)
    return 0


def cmd_run() -> int:
    while True:
        cfg = _load_config()
        if not cfg.get("token"):
            print("Not connected.", file=sys.stderr)
            return 1
        try:
            send_waiting(cfg)
        except Revoked:
            return _on_revoked()
        except reader.AccessError as exc:
            print(str(exc), file=sys.stderr, flush=True)
            _report_no_access(cfg)
        except (OSError, ValueError) as exc:
            print(f"Will retry: {exc}", file=sys.stderr, flush=True)
        time.sleep(INTERVAL_S)


def _on_revoked() -> int:
    """Disconnected from the webapp. Stop for good: KeepAlive would otherwise
    keep restarting a helper the seat will never accept again."""
    print("This Mac was disconnected from Sparrow. Stopping.", file=sys.stderr)
    try:
        _config_path().unlink()
    except FileNotFoundError:
        pass
    if sys.platform == "darwin":
        _remove_background()
    return 0


def cmd_once() -> int:
    cfg = _load_config()
    if not cfg.get("token"):
        print("Not connected.", file=sys.stderr)
        return 1
    try:
        print(f"Sent {send_waiting(cfg)} texts to Sparrow.")
    except Revoked:
        return _on_revoked()
    except reader.AccessError as exc:
        print(str(exc), file=sys.stderr)
        _report_no_access(cfg)
        return 3
    except (OSError, ValueError) as exc:
        print(f"Couldn't send: {exc}", file=sys.stderr)
        return 4
    return 0


def cmd_status() -> int:
    cfg = _load_config()
    print(f"Connected to {cfg['base']}" if cfg.get("token") else "Not connected.")
    return 0


def cmd_remove() -> int:
    if sys.platform == "darwin":
        _remove_background()
    try:
        _config_path().unlink()
    except FileNotFoundError:
        pass
    print("Removed. Texts already sent stay in Sparrow until you delete them there.")
    return 0


def main(argv: list | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    if cmd == "pair" and len(args) == 3:
        return cmd_pair(args[1], args[2])
    if cmd in ("run", "once", "status", "remove") and len(args) == 1:
        return {"run": cmd_run, "once": cmd_once, "status": cmd_status,
                "remove": cmd_remove}[cmd]()
    print(__doc__)
    return 2
