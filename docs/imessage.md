# iMessage

Apple has no API for Messages, so the source is always `~/Library/Messages/chat.db`
on the user's own Mac. It reaches Sparrow one of two ways.

| Sparrow runs | How texts arrive | Texts stored | Enable |
|---|---|---|---|
| On the Mac | Sparrow reads the file every 5 minutes | On that Mac | Always available |
| On a hosted seat | A helper on the Mac pushes them to the seat | On the server | `QUILL_IMESSAGE_RELAY=1` |

Anywhere else the connector shows "Mac only". Neither route can send a text.

## What's kept

Message text from iMessage and SMS, starting with the last 7 days. These never
land: one-time codes, reactions, attachment-only messages, business senders
(short codes and names like `AMAZON`), and anything the secret scan flags. On a
seat the filters run on the Mac and again on arrival.

Every text is stamped `sensitive`, so the peer channel, team layer, fleet relay,
records and MCP won't send that stored row anywhere.

Quoted into a prompt, a text keeps its `[imessage.message]` tag, and
`classify_text` treats that tag as `sensitive`. An answer that only paraphrases a
text has no tag, so a chat answer saved to memory is judged by its own words. The
one exception is a rated answer, which is marked when its prompt quoted a text.

## Training the user's model

Texts train that user's own model and no one else's. Every seat has its own store
and adapter (`QUILL_LORA_TAG_SUFFIX`).

- **Reply pairs:** in one-to-one chats, the texts someone received paired with what
  they wrote back within 6 hours (`connectors/imessage_learning.py`). Off with
  `QUILL_IMESSAGE_LEARN=0`. Group chats and replies under 12 characters are skipped.
- **Rated chat answers:** an answer the user rated that quoted a text trains the
  same model, and is marked so it can't be shared.
- **Quarantine:** reply pairs are stored `human_confirmed=False`, because the user
  wrote them but nobody judged them as model answers. They train only, up to 500
  per run, and stay out of the promotion holdout, exemplars and fleet sharing.
- **When it trains:** replies don't start a training run or count toward its
  threshold. Promotion needs rated answers to judge on, so a user with none gets
  no new model from texts alone. `QUILL_IDLE_TRAIN` must also be on for the seat.
- **Deleting:** purge removes the texts, reply pairs, marked rated pairs and any
  distillation row quoting a text. A model already trained keeps what it learned
  until it next retrains, and nothing forces that retrain.

**The stamp does not stop the chat model.** `gate_cloud` classifies prompt text
only and strips secrets, not names or numbers, so a text a chat answer draws on
reaches Claude. The per-chat Connectors toggle hides texts in normal chat, but
`llm.answer` (agent disabled) doesn't apply it. Both gaps affect every connector.

## Hosted seats

The user presses Connect in the webapp and agrees to the copy (the seat refuses
to pair without that, and records the sentence shown). The webapp then shows one
command to run in Terminal, which downloads the helper from the seat and pairs it.

- **Operator access:** texts sit in the seat's database, readable by anyone with
  admin access to the server.
- **Pairing:** the code is single-use and lasts 15 minutes. One Mac per seat; a
  new Mac replaces the old one. Secrets are stored only as hashes.
- **Push token:** can only push texts, not read anything. A stolen token could
  plant fake texts, which land observed-tier and can't authorize an action.
- **Disconnect:** revokes the token at once, offers to delete stored texts, and
  the helper removes itself the next time it checks in.
- **Pausing:** turning off background sync returns 403. The helper keeps its
  place and catches up when it's turned back on.
- **Routing:** the helper uses `/mcp/<seat>/imessage-relay/*` through the gateway
  (`_mcp_route`), with its own token rather than the seat token. Direct seats
  serve `/imessage-relay/*`.

Pair from a stable hostname. The helper keeps the address it was paired with, so
a quick-tunnel URL that later rotates strands it until the user pairs again. The
webapp warns when a Mac hasn't checked in for 3 hours.

### Turning it on

`deploy/hosted/gb10/docker-compose.yml` and `deploy/gateway/docker-compose.yml` set
the flag, but a running seat only picks it up when rebuilt or recreated.

## On the Mac

Full Disk Access is the only permission, and macOS never prompts for it. It covers
far more than Messages.

| Grant to | Needed for |
|---|---|
| Terminal (or Sparrow) | Sparrow running on the Mac; the helper's first run |
| The Python path the helper prints | The helper's background job after Terminal closes |

Helper commands, run with `/usr/bin/python3 ~/.sparrow/sparrow-imessage.pyz`:
`status`, `once`, `remove`.

## Not yet checked on a real Mac

Everything so far ran on Linux against a database built to the Messages schema.
Check these first:

1. Reading `attributedBody`, where newer macOS often keeps the text. If it's wrong,
   those texts are skipped with no error, so compare the count with Messages.
2. Contacts table names (`ZABCD*`). If wrong, people show as phone numbers.
3. Full Disk Access: what a blocked read looks like, and whether the grant on the
   printed Python path really covers the LaunchAgent.
4. TLS from Apple's Python to the seat, and the LaunchAgent surviving a reboot.
