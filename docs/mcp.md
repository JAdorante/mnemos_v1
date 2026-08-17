# Mnemos MCP memory server

Read-only memory tools for Claude Desktop, Claude Code, and Cursor. Mnemos stays the local substrate; those agents do not get write or action tools.

**Invariant:** retrieved memory is context. It never authorizes send, buy, or mutate. Approvals still happen in Mnemos.

## Enable

1. Set `QUILL_MCP=1` in `.env` (or the tester profile after you opt in).
2. Start Mnemos (`python run_all.py` or the installer).
3. A token is minted at `data/mcp_token` (loopback-only).

## Claude Desktop config

Add this to `claude_desktop_config.json` (Developer → Edit Config):

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": {
        "QUILL_MCP": "1",
        "QUILL_HOST": "127.0.0.1",
        "QUILL_PORT": "8000",
        "QUILL_DATA_DIR": "C:/absolute/path/to/mnemos/data"
      }
    }
  }
}
```

Use the same `QUILL_DATA_DIR` as the running Mnemos so the stdio process can read `data/mcp_token`.

Ask: **“what do I owe Justin?”** — that should hit `person_context` / `open_loops` and return provenance-tagged facts.

## Tools (v1)

| Tool | Purpose |
|---|---|
| `memory_search` | episodes + facts with source spans |
| `person_context` | who they are, open commitments, mentions |
| `open_loops` | open tasks/commitments, optional person filter |
| `org_brief` | org people/facts/open work |
| `provenance` | source quote + path-confined artifact |

There are **no** write tools and **no** action tools. `personal`-classed facts are denied by default (`data/mcp_policy.json`).

## Disclosure

Same classes as the peer channel: `availability` / `work` / `contact` / `personal` / `other`. Default `personal → deny`. Edit `data/mcp_policy.json` if you need to widen (never set `personal` to `auto` — the server refuses that).
