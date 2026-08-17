"""Thin stdio MCP adapter — HTTP to localhost only, no service imports."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _data_dir() -> Path:
    return Path(os.environ.get("QUILL_DATA_DIR", "data"))


def token_path() -> Path:
    override = os.environ.get("QUILL_MCP_TOKEN")
    return Path(override) if override else _data_dir() / "mcp_token"


def _token() -> str:
    p = token_path()
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return ""


def _base() -> str:
    host = os.environ.get("QUILL_HOST", "127.0.0.1")
    port = os.environ.get("QUILL_PORT", "8000")
    return f"http://{host}:{port}"


def _post_tool(name: str, arguments: dict) -> dict[str, Any]:
    url = _base() + "/mcp/tool"
    body = json.dumps({"name": name, "arguments": arguments}).encode()
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    tok = _token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except URLError as exc:
        return {"ok": False, "error": f"API unreachable: {exc}"}


def _get_schemas() -> list:
    url = _base() + "/mcp/tools"
    req = Request(url, method="GET")
    tok = _token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("tools") or []
    except Exception:
        return []


def run_stdio() -> None:
    if os.environ.get("QUILL_MCP", "0") in ("0", "false", "False"):
        print("QUILL_MCP=0 — MCP server not started.", file=sys.stderr)
        sys.exit(0)

    def reply(msg_id, result=None, error=None):
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        msg_id = req.get("id")
        if method == "initialize":
            reply(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mnemos-memory", "version": "1.0.0"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(msg_id, {"tools": _get_schemas()})
        elif method == "tools/call":
            params = req.get("params") or {}
            result = _post_tool(params.get("name") or "", params.get("arguments") or {})
            reply(msg_id, {
                "content": [{"type": "text", "text": json.dumps(result, default=str)}],
            })
        elif msg_id is not None:
            reply(msg_id, error={"code": -32601, "message": f"unknown method {method}"})
