"""HTTP client from a Sparrow node to the Org Coordinator."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from urllib import error, request

from app.config import settings


def _cfg():
    return settings.org


def enabled() -> bool:
    """True when the org network is on.

    Prefer live env (so toggling QUILL_ORG_NETWORK without a full settings
    rebuild still works), then frozen config, then "already registered"."""
    env = os.environ.get("QUILL_ORG_NETWORK")
    if env is not None and env != "":
        return env not in ("0", "false", "False")
    if _cfg().enabled:
        return True
    return bool(node_token())


def _state() -> dict:
    p = Path(_cfg().state_path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(data: dict) -> None:
    p = Path(_cfg().state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def node_token() -> str:
    return (_cfg().node_token or _state().get("token") or "").strip()


def node_id() -> str:
    return (_cfg().node_id or _state().get("node_id") or "").strip()


def manager_peer_id() -> str:
    return (_cfg().manager_peer_id
            or _state().get("manager_peer_id") or "").strip()


def role() -> str:
    return (_cfg().role or _state().get("role") or "ic").strip().lower()


def reports_to() -> str:
    return (_cfg().reports_to or _state().get("reports_to") or "").strip()


def coordinator_reachable(timeout_s: float = 2.0) -> bool:
    try:
        url = (_state().get("coordinator_url") or _cfg().coordinator_url).rstrip("/")
        req = request.Request(f"{url}/health", method="GET")
        with request.urlopen(req, timeout=timeout_s) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def request_json(method: str, path: str, body: dict | None = None,
                 *, auth: bool = True,
                 coordinator_url: str | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if auth:
        tok = node_token()
        if not tok:
            return {"ok": False, "error": "not registered (no node token)"}
        headers["Authorization"] = f"Bearer {tok}"
    base = (coordinator_url
            or _state().get("coordinator_url")
            or _cfg().coordinator_url).rstrip("/")
    req = request.Request(f"{base}{path}", data=data, headers=headers,
                          method=method)
    try:
        with request.urlopen(req, timeout=_cfg().http_timeout_s) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {"ok": True}
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return {"ok": False, "error": f"HTTP {exc.code}", "detail": detail}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def register(*, base_url: str = "", peer_id: str = "",
             display_name: str = "", role: str | None = None,
             reports_to: str | None = None,
             node_id_override: str | None = None,
             coordinator_url: str | None = None) -> dict[str, Any]:
    nid = (node_id_override or node_id() or
           (_cfg().display_name or "node").lower().replace(" ", "-")[:40])
    body = {
        "node_id": nid,
        "display_name": display_name or _cfg().display_name or nid,
        "role": (role or _cfg().role or "ic").lower(),
        "reports_to": reports_to if reports_to is not None else _cfg().reports_to,
        "base_url": base_url or f"http://{settings.host}:{settings.port}",
        "peer_id": peer_id or manager_peer_id(),
    }
    url = (coordinator_url or _cfg().coordinator_url).rstrip("/")
    res = request_json("POST", "/register", body, auth=False,
                       coordinator_url=url)
    if res.get("ok") and res.get("token"):
        st = {
            "node_id": nid,
            "token": res["token"],
            "role": body["role"],
            "reports_to": body["reports_to"],
            "registered_at": time.time(),
            "coordinator_url": url,
            "manager_peer_id": body["peer_id"],
            "display_name": body["display_name"],
        }
        _save_state(st)
        # Keep digest/escalate peer delivery working without restart.
        if body["peer_id"]:
            os.environ["QUILL_ORG_MANAGER_PEER_ID"] = body["peer_id"]
        os.environ["QUILL_ORG_NETWORK"] = "1"
        os.environ["QUILL_ORG_NODE_ID"] = nid
        os.environ["QUILL_ORG_ROLE"] = body["role"]
        if body["reports_to"]:
            os.environ["QUILL_ORG_REPORTS_TO"] = body["reports_to"]
    return res


def post_digest(digest: dict) -> dict[str, Any]:
    return request_json("POST", "/ingest/digest", digest)


def fetch_priorities() -> dict[str, Any]:
    return request_json("GET", "/priorities")


def cascade() -> dict[str, Any]:
    return request_json("POST", "/cascade", {})


def escalate(signal: dict) -> dict[str, Any]:
    return request_json("POST", "/escalate", signal)


def create_goal(title: str, **kwargs) -> dict[str, Any]:
    body = {"title": title, **kwargs}
    return request_json("POST", "/goals", body)


def status() -> dict[str, Any]:
    st = _state()
    url = st.get("coordinator_url") or _cfg().coordinator_url
    return {
        "enabled": enabled(),
        "coordinator_url": url,
        "coordinator_reachable": coordinator_reachable(),
        "node_id": node_id(),
        "role": role(),
        "reports_to": reports_to(),
        "registered": bool(node_token()),
        "manager_peer_id": manager_peer_id(),
        "digest_interval_h": _cfg().digest_interval_h,
        "ui": "/org-network",
    }
