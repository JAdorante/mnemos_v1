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

## Adding a user

Copy a service block in `docker-compose.yml` (new name, port, volume,
`QUILL_LORA_TAG_SUFFIX`), add a token line to `.env`, `docker compose up -d`,
add one more `tailscale serve` line.

## Upgrade path

- Public domain instead of Tailscale: run Caddy with one subdomain per user
  reverse-proxying to 8001/8002/8003 — TLS is automatic.
- GPU ASR: needs a CUDA-enabled CTranslate2 on arm64 (build from source or a
  shared ASR sidecar); see ../README.md "GPU ASR". Not worth it for a pilot —
  CPU `distil` models on Grace keep up with bursty speech.
