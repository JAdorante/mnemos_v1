# Sparrow at `sparrow.ravenry.us` — multi-user front door

Sign in, sign up, and one **private Sparrow instance per person**.

## Why a gateway and not multi-user accounts

The app is single-tenant all the way down: one `data/` dir, one credential
record (`app/services/account.py`), and per-person perception, peer and LoRA
state. Rather than rewrite that, tenancy lives out here. A signup mints a
**seat** — a whole container with its own volume and its own
`QUILL_API_TOKEN` — and the gateway proxies each signed-in browser into its
owner's seat. The app itself is unmodified.

```
browser ──TLS──> Cloudflare ──named tunnel──> gateway:8080
                                                  │  session cookie -> seat
                                                  ├──> sparrow-seat-a1b2c3d4:8000
                                                  └──> sparrow-seat-9f8e7d6c:8000
```

Seats publish **no host port**. They exist only on the private `sparrow-net`,
so the gateway is the single way in.

## The token, and where it lives

The per-seat `QUILL_API_TOKEN` is generated at provisioning time and **never
reaches the browser**. The gateway injects it as a `Bearer` header on every
proxied hop, which clears the app's LAN gate. Users only ever have an email
and a password.

That Bearer also exempts the hop from the app's own CSRF middleware, so the
gateway does the `Origin`/`Referer` check itself for `POST/PUT/PATCH/DELETE`
(`_origin_ok` in `gateway.py`).

## Deploy

1. **Put `ravenry.us` on Cloudflare** (free plan is enough).
2. **Create a named tunnel**: Zero Trust → Networks → Tunnels → Create.
   Add a Public Hostname: `sparrow.ravenry.us` → `HTTP` → `gateway:8080`.
   Cloudflare writes the DNS record. Copy the tunnel token.
   A *named* tunnel, not a quick tunnel — the hostname must survive restarts.
3. **Configure and build**, from the repo root:

   ```sh
   cp deploy/gateway/.env.example deploy/gateway/.env
   # paste TUNNEL_TOKEN, optionally set SIGNUP_INVITE_CODE
   docker build -f deploy/hosted/Dockerfile -t sparrow-hosted .
   docker compose -f deploy/gateway/docker-compose.yml up -d --build
   ```

4. Visit `https://sparrow.ravenry.us` → `/signup`.

TLS terminates at Cloudflare, which satisfies `getUserMedia`'s secure-context
requirement on `/capture`.

## Signup flow

`/signup` → validate → `provision.create_seat()` starts the container →
session cookie set → `/provisioning` polls `/api/seat/status` until the seat
answers `/health` (first boot loads the ASR model under
`QUILL_ASR_WARMUP=1`, so expect a minute) → redirect to `/`.

Returning users go to `/signin`; their seat is restarted if the host rebooted
and left it stopped.

**Gating signup.** Open by default. Set `SIGNUP_INVITE_CODE` in `.env` to
require a shared code — the field appears on the form automatically. Cap
capacity with `MAX_SEATS`, and size `SEAT_MEM_LIMIT` / `SEAT_CPUS` to the box:
every seat runs its own CPU ASR.

## Security posture

| Concern | Handling |
|---|---|
| Passwords | scrypt + per-account salt (`store.py`) |
| Session cookies | random token, stored **sha256-hashed**; HttpOnly, Secure, SameSite=Lax |
| Seat selection | read only from the session — never from a path, header, or query |
| Client `Authorization` | stripped before the upstream hop; users cannot present their own token |
| Gateway cookie | stripped before the upstream hop; the app never sees it |
| CSRF | gateway-side Origin/Referer check on state-changing methods |
| Sign-in errors | one message for unknown-email and wrong-password (no existence oracle) |
| Brute force | per-IP throttle, 8 failures / 15 min |

The gateway mounts the Docker socket, which is root-equivalent on the host. It
is the only container given it, and no user input reaches an image name,
command, or bind mount.

## Back this up

`gateway-data` (users.json + sessions.json) is the **only** record of who owns
which seat and of the seat tokens. Lose it and the seat volumes are orphaned.

## Tests

```sh
cd deploy/gateway && python3 -m unittest test_gateway -v
```

24 tests. They run a real upstream HTTP server and proxy to it, covering token
injection, cookie stripping, multi `Set-Cookie` passthrough, the CSRF check,
throttling, and seat isolation. No Docker needed.

## Known limits

* **Peering between users works; pairing with a Sparrow *outside* this box
  does not.** Everyone who signs up is a seat on this box, so every pair is an
  on-box pair: seats reach each other directly on `sparrow-net`, the traffic
  never leaves the machine, and it never touches the gateway (peer calls carry
  their own bearer token and go container-to-container). Each seat advertises
  two addresses — the stable DNS name from `QUILL_PEER_INTERNAL_URL`, tried
  first, and the `my_base_url()` fallback of `http://<container-ip>:8000`.
  Container IPs churn on restart, but the DNS name does not and peers
  re-advertise both on every ping, so records self-heal.

  The gap is only a Sparrow that is **not** a seat here — someone running the
  desktop app on their own laptop. It can reach neither address, and there is
  no per-seat public URL to give it because every seat shares one hostname.
  Supporting that needs per-seat hostnames (`seat-xyz.sparrow.ravenry.us`) plus
  a wildcard DNS record.

  Note this leaves peer hops on plain HTTP inside the box
  (`QUILL_PEER_REQUIRE_TLS` defaults off, as in the gb10 pilot). That is the
  private Docker network, not the wire.
* **No password reset**, no email verification, no account deletion. Signup is
  the only account operation. Gate with `SIGNUP_INVITE_CODE` until that lands.
* **Seats are never reclaimed.** An abandoned account keeps its container and
  volume. Prune by hand: `docker ps -a --filter label=sparrow.seat`.
* One shared host Ollama serves every seat, as in the gb10 pilot — concurrent
  chat across many users queues on it.
