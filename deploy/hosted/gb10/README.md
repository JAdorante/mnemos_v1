# GB10 pilot — running Sparrow for a few select users

One container per user, CPU ASR (CTranslate2 has no CUDA wheels on arm64),
GPU reserved for the single shared Ollama on the host. Containers bind to
loopback; Tailscale provides TLS + access control, so nothing is exposed to
the public internet and only people you invite to the tailnet can connect.

## One-time host setup (needs sudo)

```bash
# 1. Tailscale (TLS + private access for testers)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale cert   # enables HTTPS certs once serve is used

# 2. Make host Ollama reachable from containers (it defaults to loopback):
sudo systemctl edit ollama    # add:
#   [Service]
#   Environment="OLLAMA_HOST=0.0.0.0"
sudo systemctl restart ollama
```

## Bring up the containers

```bash
cd deploy/hosted/gb10
# .env already holds one generated QUILL_API_TOKEN per user (chmod 600, gitignored)
docker compose up -d --build
curl -s http://127.0.0.1:8001/health   # per-user sanity check
```

## Route users in over TLS

Tailscale serve maps one HTTPS port per user on the machine's tailnet name
(e.g. `gb10.tail1234.ts.net`):

```bash
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8001   # user1
sudo tailscale serve --bg --https=8444 http://127.0.0.1:8002   # user2
sudo tailscale serve --bg --https=8445 http://127.0.0.1:8003   # user3
```

Invite each tester to your tailnet (Tailscale admin console → "Invite
external users", or share the node). Then send each person:

- their URL: `https://gb10.<tailnet>.ts.net:8443` (their port)
- their token from `.env`

First visit: `/auth` → paste token → `/capture` → opt in → talk. The
"last heard" ticker is the end-to-end sanity check.

## Pairing testers with each other

On the `/peer` page, "Create an invite" prints one `sparrow://pair/…` line (with
a QR) that already carries the address, the name and the 6-digit code — the
teammate pastes it into the single box on their own `/peer` page. Nobody reads a
`*.trycloudflare.com` hostname aloud. The invite is the same single-use code
underneath: one claim, ten minutes, five wrong tries and it dies.

Every container also sets `QUILL_PEER_INTERNAL_URL` to its compose-network name,
which peers try *before* the public URL. So the five on-box seats talk to each
other over the docker network — a quick-tunnel restart cannot break an on-box
pair, and their questions never leave the machine. Off-box peers (the desktop at
10.0.0.81) only ever see `QUILL_PEER_BASE_URL`; peers re-advertise both addresses
on every ping, so a changed hostname heals itself.

## Google connector (Gmail + Calendar metadata)

**Register one redirect URI, once.** Google's Web OAuth client only accepts
redirect URIs registered ahead of time, and quick-tunnel hostnames rotate on
every restart — six seats would mean six new URIs to paste in each time. So
the pilot anchors OAuth on a hostname that never moves (a Tailscale Funnel)
and relays the code back to whichever tunnel the user actually started on.

How the round trip works:

1. The browser is on `https://<random>.trycloudflare.com`. Connect Google
   mints `state = <random>.<base64url(that origin)>` and sets
   `redirect_uri = $SPARROW_OAUTH_REDIRECT_BASE/oauth/google/callback`.
2. Google sends the code to the funnel hostname — the one registered URI.
3. That lands on whichever container the funnel points at. If it did not
   mint the `state`, it decodes the origin out of `state` and 302s the code
   there untouched (`app/api/adoption.py`, `google_oauth_callback`).
4. The originating seat exchanges the code using the funnel `redirect_uri`
   it saved at connect time, so Google's exact-match check passes and the
   tokens land in that user's own volume under `data/connectors/google/`.

Only the `state` string crosses containers; the relay never sees tokens and
never needs the other seat's state file. A tunnel restart changes nothing.

### Setup

```bash
# 1. A stable public hostname for the callback (free, no domain needed).
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# Expose ONLY the callback path publicly, pointed at any one container:
sudo tailscale funnel --bg --set-path=/oauth/google/callback \
     http://127.0.0.1:8001/oauth/google/callback
tailscale funnel status   # note the https://gb10.<tailnet>.ts.net hostname
```

```bash
# 2. .env on the GB10
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
SPARROW_OAUTH_REDIRECT_BASE=https://gb10.<tailnet>.ts.net
```

3. Google Cloud Console → **Web application** OAuth client → Authorized
   redirect URIs → add exactly one:
   `https://gb10.<tailnet>.ts.net/oauth/google/callback`.
   Enable the Gmail API + Google Calendar API on the project, and add each
   pilot user as a test user while the consent screen is unverified
   (`gmail.readonly` is a restricted scope).
4. `docker compose up -d --build`, then Onboarding → Calendar →
   **Connect Google**.

Leave `SPARROW_OAUTH_REDIRECT_BASE` unset and the old behaviour returns: the
redirect is minted from the live origin (`window.location.origin` / `Origin`
/ `X-Forwarded-Host`), which means registering every hostname by hand.
Desktop installs without a public HTTPS base keep the loopback OAuth flow.

If `tailscale funnel` on this version has no `--set-path`, funnel the whole
root instead (`sudo tailscale funnel --bg http://127.0.0.1:8001`) — user1's
app is then publicly reachable, but still behind the `/auth` token wall,
same posture as the quick tunnels.

## Adding a user

Copy a service block in `docker-compose.yml` (new name, port, volume,
`QUILL_LORA_TAG_SUFFIX`, `QUILL_PEER_INTERNAL_URL`), add a token line to `.env`,
`docker compose up -d`, add one more `tailscale serve` line.

## Upgrade path

- Public domain instead of Tailscale: run Caddy with one subdomain per user
  reverse-proxying to 8001/8002/8003 — TLS is automatic.
- GPU ASR: needs a CUDA-enabled CTranslate2 on arm64 (build from source or a
  shared ASR sidecar); see ../README.md "GPU ASR". Not worth it for a pilot —
  CPU `distil` models on Grace keep up with bursty speech.
