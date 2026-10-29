# Sparrow MCP memory server

Two ways in:

- **Local (stdio, below):** read-only memory tools for Claude Desktop, Claude Code, and Cursor on this machine.
- **Remote (HTTPS, [further down](#remote-chatgpt-claude-grok)):** ChatGPT, Claude, and Grok connect as custom connectors, so what you work out with them funnels back into Sparrow for your projects.

**Invariant:** retrieved memory is context. It never authorizes send, buy, or mutate. Approvals still happen in Sparrow. The one write tool, `save_to_sparrow` (remote only), *proposes* a note into a review inbox. Nothing becomes memory until you accept it, and an accepted note can never authorize an action.

## Enable

1. Set `QUILL_MCP=1` in `.env` (or the tester profile after you opt in).
2. Start Sparrow (`python run_all.py` or the installer).
3. A token is minted at `data/mcp_token` (loopback-only).

## Claude Desktop config

Add this to `claude_desktop_config.json` (Developer → Edit Config):

```json
{
  "mcpServers": {
    "sparrow": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": {
        "QUILL_MCP": "1",
        "QUILL_HOST": "127.0.0.1",
        "QUILL_PORT": "8000",
        "QUILL_DATA_DIR": "C:/absolute/path/to/sparrow/data"
      }
    }
  }
}
```

Use the same `QUILL_DATA_DIR` as the running Sparrow so the stdio process can read `data/mcp_token`.

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

## Remote: ChatGPT, Claude, Grok

One endpoint serves all three: `https://<your Sparrow>/mcp` (Streamable HTTP, OAuth 2.1). The **Assistants** page (`/assistants`, under More) shows the exact URL to paste, the review inbox, connected assistants, and access tokens.

### Enable

1. Set `QUILL_MCP_REMOTE=1`. Sparrow must also have an access token (`QUILL_API_TOKEN`), since that or your account password is what you sign in with on the consent screen.
2. Sparrow must be reachable over **stable** HTTPS. Assistants store the URL when you connect, so a quick-tunnel hostname that changes on restart breaks every connection.
   - **Hosted (gateway):** nothing to set. Each seat is at `https://sparrow.ravenry.us/mcp/<seat>/mcp`, and the Assistants page shows it.
   - **Own tunnel / domain:** set `QUILL_MCP_PUBLIC_URL=https://sparrow.example.com` if the page shows the wrong address.

### Connect

| Assistant | Where | Notes |
|---|---|---|
| ChatGPT | Settings → Apps & Connectors → Advanced → Developer mode → Create | Choose OAuth. Write tools may need a Business/Enterprise/Edu plan; on others it may only read. |
| Claude | Settings → Connectors → Add custom connector | Works on claude.ai, Desktop, and mobile. |
| Grok | grok.com/connectors → New Connector → Custom | For the xAI API's remote MCP tools, mint an access token on `/assistants` and send it as `Authorization: Bearer …`. |
| Claude Code / other | `claude mcp add --transport http sparrow <url>` | OAuth, or an access token header. |

Each assistant opens Sparrow's consent screen. Sign in if asked, then choose what it may do:

- **Save to your review inbox** (`capture` scope): enables `save_to_sparrow` and `list_projects`.
- **Read your Sparrow memory** (`read` scope): the same read tools as stdio, with the same `privacy_class` + `mcp_policy.json` filtering. Results go to that assistant's servers.

Then ask: *"save the decision we just made to Sparrow under the onboarding project."*

### What happens to a save

1. It lands in the inbox on `/assistants`, tagged with the assistant, project, kind, and source link. Text that looks like a key or password is flagged.
2. **Accept** (after editing the text or project if you like) stores it as an event with source `assistant.<chatgpt|claude|grok>`. The event is observed-tier and `never_authorizes`, secrets are redacted, and it goes through the same extractor as typed chat. The `assistant_capture` source class may produce claims, tasks, and project links, but not contacts or identity evidence.
3. **Reject** wipes the text and keeps only an audit row.

Disconnect any assistant on `/assistants`; its tokens stop working immediately.

### Security notes

- Every remote route checks its own credentials, loopback included, because a tunnel daemon on the same machine makes public requests look local.
- PKCE S256 only. Authorization codes are single-use (a replayed code revokes everything it minted), access tokens last 1 hour, and refresh tokens rotate. Only SHA-256 hashes are stored, in `data/assistant_connect.db`.
- Client registration (RFC 7591) is open by design: a client id grants nothing until you approve it, and registrations nobody approved are pruned after a day.
- The consent screen cannot be framed, and its form is same-origin only.

