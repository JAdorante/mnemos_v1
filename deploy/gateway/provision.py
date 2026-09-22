"""Seat provisioner — one private Sparrow container per human.

A "seat" is the unit of tenancy: its own container, its own named volume, its
own QUILL_API_TOKEN. Nothing is shared between seats except the host Ollama
(already the posture in deploy/hosted/gb10) and the gateway itself.

Seats are NOT published on host ports. They are reachable only on the gateway's
private Docker network, so the gateway is the single front door and an
unauthenticated request can never reach a container directly.
"""
from __future__ import annotations

import os
import secrets
import threading
from typing import Any

# Imported lazily inside client(): only seat creation needs the Docker SDK, and
# keeping it out of import time lets the proxy path (and tests) run without it.

# Mirrors deploy/hosted/gb10/docker-compose.yml. Kept here (rather than in a
# compose file) because seats are created at runtime, not declared up front.
SEAT_IMAGE = os.environ.get("SEAT_IMAGE", "sparrow-hosted")
SEAT_NETWORK = os.environ.get("SEAT_NETWORK", "sparrow-net")
SEAT_MEM_LIMIT = os.environ.get("SEAT_MEM_LIMIT", "6g")
SEAT_CPUS = float(os.environ.get("SEAT_CPUS", "2"))
MAX_SEATS = int(os.environ.get("MAX_SEATS", "25"))

_lock = threading.RLock()
_client: Any = None


def client() -> Any:
    global _client
    if _client is None:
        import docker
        _client = docker.from_env()
    return _client


def seat_host(seat: str) -> str:
    """Private DNS name of the seat on the gateway network."""
    return f"sparrow-{seat}"


def seat_url(seat: str) -> str:
    return f"http://{seat_host(seat)}:8000"


def _seat_env(seat: str, token: str) -> dict[str, str]:
    env = {
        "QUILL_API_TOKEN": token,
        "QUILL_TEXT_LOCAL": "1",
        "QUILL_OLLAMA_URL": os.environ.get(
            "SEAT_OLLAMA_URL", "http://host.docker.internal:11435"),
        "OLLAMA_HOST": os.environ.get(
            "SEAT_OLLAMA_HOST", "host.docker.internal:11435"),
        "QUILL_TEXT_LOCAL_MODEL": os.environ.get(
            "SEAT_TEXT_MODEL", "qwen2.5:7b-instruct"),
        "QUILL_LORA_TAG_SUFFIX": seat,
        # Peers on this box reach each other over the private network, so peer
        # traffic never leaves the machine and survives a tunnel restart. There
        # is no per-seat PUBLIC url any more — every seat shares one hostname
        # and is told apart by the gateway session — so QUILL_PEER_BASE_URL is
        # deliberately unset. Off-box peering is therefore not available here.
        "QUILL_PEER_INTERNAL_URL": seat_url(seat),
        "QUILL_PEER_DEFAULT_PACK": os.environ.get("SEAT_PEER_PACK", "pilot"),
        # Pilot posture: prepare freely, stop before anything irreversible.
        "AGENT_DRY_RUN": os.environ.get("SEAT_AGENT_DRY_RUN", "draft"),
    }
    for optional in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
                     "MS_OAUTH_CLIENT_ID", "MS_OAUTH_CLIENT_SECRET",
                     "MS_OAUTH_TENANT"):
        if os.environ.get(optional):
            env[optional] = os.environ[optional]
    if os.environ.get("SEAT_OAUTH_REDIRECT_BASE"):
        env["QUILL_OAUTH_REDIRECT_BASE"] = os.environ["SEAT_OAUTH_REDIRECT_BASE"]
    if os.environ.get("SEAT_ANTHROPIC_KEY"):
        env["ANTHROPIC_API_KEY"] = os.environ["SEAT_ANTHROPIC_KEY"]
    return env


def seat_count() -> int:
    return len(client().containers.list(
        all=True, filters={"label": "sparrow.seat"}))


def create_seat() -> tuple[str, str]:
    """Mint a seat id + upstream token and start the container. Returns
    (seat, token). The caller persists both in the user record."""
    with _lock:
        if seat_count() >= MAX_SEATS:
            raise RuntimeError("this deployment is full")
        seat = "seat-" + secrets.token_hex(4)
        token = secrets.token_urlsafe(32)
        cli = client()
        volume = f"sparrow-{seat}-data"
        cli.volumes.create(name=volume, labels={"sparrow.seat": seat})
        cli.containers.run(
            SEAT_IMAGE,
            name=seat_host(seat),
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            network=SEAT_NETWORK,
            environment=_seat_env(seat, token),
            volumes={volume: {"bind": "/srv/sparrow/data", "mode": "rw"}},
            extra_hosts={"host.docker.internal": "host-gateway"},
            mem_limit=SEAT_MEM_LIMIT,
            nano_cpus=int(SEAT_CPUS * 1e9),
            labels={"sparrow.seat": seat},
        )
        return seat, token


def seat_running(seat: str) -> bool:
    import docker
    try:
        return client().containers.get(seat_host(seat)).status == "running"
    except docker.errors.NotFound:
        return False


def ensure_running(seat: str) -> None:
    """Restart a seat whose container was stopped (host reboot, manual stop).
    Called on sign-in so a returning user never lands on a dead upstream."""
    import docker
    try:
        container = client().containers.get(seat_host(seat))
    except docker.errors.NotFound:
        return
    if container.status != "running":
        container.start()
