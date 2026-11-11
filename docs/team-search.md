# Team search (org scope of "Google-style search")

Step 3 of the search work from the Sparrow 1.0 call notes (2026-11-08): one
search that reaches teammates' Sparrows, built on a central pointer index.
Steps 1–2 (passages, search budget, request-as-query, reranking) are in
`app/services/grounding.py` and `app/services/search_rank.py`.

## The four rules

1. **Pointers are minimal.** A published pointer carries the owner, the
   project / org tags, the item type, the people involved (names, never
   addresses), a timestamp and a location reference (owner, node, item id,
   source system). No titles and no snippets, except for items in a shared
   project, and then only when that text passes the egress privacy check.
   Only items filed under a project or org tag get a pointer at all (tags
   are the index), so untagged mail and its people stay home. Items above
   the work privacy class, and sources that are personal by nature (texts,
   calls, screen, audio, chat with Sparrow), get none either.
   The org database enforces the title rule itself:
   `ck_pointers_text_only_when_shared` (migration `0004_pointer_index`).
2. **Permissions are checked when a search runs.** Every candidate is put in
   its tier *before* anything is ranked:
   - *open*: the item is in a shared project that both the reader and the
     owner can read right now. The reader sees the full pointer, and its
     title and snippet count as match keys.
   - *routed*: everything else. At most one "ask <owner>" row per owner,
     matched on tags and people only, naming only the words the reader typed.
     A routed item is never found by its title.

   Each tier is ranked separately, so what a reader cannot open never
   changes what they can see. A reader's own items and departed members'
   items are skipped.
3. **Deletions and revocations propagate.**

   | What happens | What comes out | Where |
   |---|---|---|
   | item deleted or expired on a seat | that pointer, at once (retried until acknowledged) | store delete hook → `team_index._on_store_change` |
   | a delete the hook missed | that pointer, at the next full pass or the daily reconcile | `team_index.sync`, `/pointers/reconcile` |
   | member stops sharing a project | their pointers there, at once; republished later as routing-only | `POST /team/index/projects`, `/pointers/unshare` |
   | member loses read on a project (grant revoke, scope move) | their pointers there | `pointer_index.drop_unreadable` |
   | member departs or leaves | all their pointers, in the same transaction | `service.depart_member` / `leave` / CLI `depart` |
   | org leaves Connected, or turns publishing off | every pointer in the org, in the same transaction | `service.put_policy` |
   | seat setting turned off, or the seat's rung tightens | all of that seat's pointers | `team_index.sync` (empty reconcile) |

   Withdrawals never depend on the index being open.
4. **Sealed and Perimeter keep the alternative.** Publishing is a setting:

   | Rung | What happens |
   |---|---|
   | Connected | seats publish pointers to the org index; search, chat grounding and a connected assistant read it; works while a teammate is offline |
   | Sealed, Perimeter | nothing is published; the org service refuses pointers. A person's search goes to each paired teammate's Sparrow as a `pointer_query` peer ask, that seat answers from its own pointers by the same rule, and the owner approves each answer before it leaves |

## In chat

Team search is a command that runs as soon as it is typed (not a suggestion
chip): `/team <words>`, "search the team for <words>", "team search: <words>"
or "who on my team knows about <words>". It runs ahead of a pending approval
prompt, which stays pending.

- Connected: reads the index and lists the pointers, plus an
  "Ask <name>'s Sparrow" chip for each teammate it routes to. The chip sends
  the same words as an ordinary peer question, so the owner's disclosure gate
  still decides what comes back.
- Sealed / Perimeter: asks each paired teammate's Sparrow at once (in
  parallel), shows any answers that came straight back, and says who is
  waiting on approval, offline, or not paired. Approved answers arrive later
  as peer answers in the same chat.

Ordinary chat answers also include team pointers on Connected (grounding);
they never ask teammates' Sparrows.

## Settings

- `QUILL_TEAM_SEARCH` on the seat: `off` (default), `on` (publish on
  Connected, answer directly otherwise) or `federated` (never publish).
- Org policy `team_search.publish` (`PUT /orgs/{id}/policy`, admin): `false`
  closes the index for the whole org and empties it.
- The rung itself comes from the org (or `QUILL_EGRESS_RUNG` on a seat).

## Map

| Piece | File |
|---|---|
| pointer shape, match keys, the one ranking (stdlib, shared with the org image) | `app/services/records/pointers.py` |
| org index: publish, withdraw, reconcile, unshare, search, purges | `org_coordinator/records/pointer_index.py` |
| table + CHECK + RLS | `org_coordinator/migrations/versions/0004_pointer_index.py` |
| HTTP | `org_coordinator/records/api.py` (`/pointers*`, `/scopes/common`, `/orgs/{id}/members/{id}/depart`, `/members/leave`) |
| seat: build pointers, local table, sync, delete hook, unshare | `app/services/team_index.py` |
| seat: search, chat grounding, fan-out, the answering side of a `pointer_query` | `app/services/team_search.py` |
| peer kind `pointer_query` | `app/services/peer_channel.py` (`handle_ask`, `decide_ask`, `ask`) |
| egress paths `index` and `pointers` | `app/services/egress.py` |
| connected assistant tool `team_search` (needs the `team` scope) | `app/services/remote_mcp.py` |
| chat command | `team_search.parse_command` / `run_command`, intercepted in `routes.chat` |
| seat API | `app/api/team_search_routes.py` (`/team/search`, `/team/search/ask`, `/team/index*`) |
| 10-minute tick | `app/services/records/scheduler.py` (`team_index`) |
| tests | `tests/test_team_search.py`, `tests/test_team_search_org.py` (Postgres) |

## Judgment calls to confirm

- **Who counts as a project member.** Grants inherit down the scope tree, so
  everyone who can read a team reads every project under it (the existing
  records model). On the pilot that means the whole "Ravenry team".
- **"In a shared project" means tagged with one.** An item is in a shared
  project when its tags resolve to that project: an accepted note filed
  there, or an item the extractor linked to the project entity. Extractor
  tags are noisy, so a misfiled email's subject can show to that project's
  members. A stricter option is to give titles only to items the user filed.
- **Perimeter rule.** The egress rule now lets a *human-approved* team search
  answer cross a Perimeter seat (path `pointers`), next to approved records.
  Without that, Perimeter could not ask teammates at all.
- **Misses.** A teammate's search that matches nothing gets a typed "no
  match" reply without asking the owner, as plain peer questions already do.
  On Sealed and Perimeter that reply reveals one bit (no match) automatically.
- **Who can be asked.** Fan-out reaches only teammates this seat has paired
  with; unpaired teammates are listed, not skipped silently. A connected
  assistant never fans out, and neither does chat grounding, so nobody gets
  an approval prompt they did not cause.
- **Index scope.** Org-wide: routing can name anyone in the org, not only
  the reader's team.
- **Untagged items get no pointer.** This keeps personal mail out (the
  privacy classifier rates a doctor's appointment "internal"), but it also
  means "who has been emailing Rachel Osei?" only finds items filed under a
  project or org.
