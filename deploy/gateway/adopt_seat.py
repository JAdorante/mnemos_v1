"""Adopt an ALREADY-RUNNING Sparrow container as a gateway seat.

For the gb10 pilot users: their data is in place and their container is fine,
they just have no email/password and live on a different Docker network. This
does the two things that fixes, without moving a byte of data:

1. Attaches the container to the gateway network under the alias the gateway
   resolves (``sparrow-<seat>``), so the proxy can reach it. The container
   keeps its original name, network and compose stack — its quick tunnel goes
   on working, so nothing is cut over until you choose to.
2. Writes the user record, reusing the container's OWN QUILL_API_TOKEN read
   straight from its environment. The token is never typed or echoed.

Run it INSIDE the gateway container (that is where the store and the Docker
socket both live):

    docker cp adopt_seat.py gateway-gateway-1:/srv/gateway/
    docker exec -it gateway-gateway-1 python /srv/gateway/adopt_seat.py \\
        --email you@example.com --container gb10-sparrow-user1-1 --seat seat-user1

Copy it to /srv/gateway, NOT /tmp: Python puts the script's own directory on
sys.path, so from /tmp it cannot import store/provision.

The password is prompted for, never passed as an argument, so it stays out of
your shell history and the process list.
"""
from __future__ import annotations

import argparse
import getpass
import sys

import docker

import provision
import store


def container_token(container) -> str:
    """Pull QUILL_API_TOKEN out of the container's own environment."""
    for entry in container.attrs["Config"]["Env"]:
        if entry.startswith("QUILL_API_TOKEN="):
            return entry.split("=", 1)[1]
    raise SystemExit(
        f"{container.name} has no QUILL_API_TOKEN — is it a Sparrow container?")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", required=True)
    ap.add_argument("--container", required=True,
                    help="existing container name, e.g. gb10-sparrow-user1-1")
    ap.add_argument("--seat", required=True,
                    help="seat id to file it under, e.g. seat-user1")
    args = ap.parse_args()

    email = store.normalize_email(args.email)
    if store.get_user(email):
        raise SystemExit(f"{email} already has a seat — nothing to do")

    cli = docker.from_env()
    try:
        container = cli.containers.get(args.container)
    except docker.errors.NotFound:
        raise SystemExit(f"no container named {args.container}")
    token = container_token(container)

    password = getpass.getpass("Password for this account: ")
    if len(password) < 10:
        raise SystemExit("password must be at least 10 characters")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("passwords do not match")

    alias = provision.seat_host(args.seat)          # sparrow-<seat>
    network = cli.networks.get(provision.SEAT_NETWORK)
    already = provision.SEAT_NETWORK in container.attrs["NetworkSettings"]["Networks"]
    if already:
        print(f"· {container.name} is already on {provision.SEAT_NETWORK}")
    else:
        network.connect(container, aliases=[alias])
        print(f"· attached {container.name} to {provision.SEAT_NETWORK} "
              f"as {alias}")

    store.create_user(email, password, args.seat, token)
    print(f"· filed {email} -> {args.seat} (token reused from the container)")
    print("\nDone. Sign in at https://sparrow.ravenry.us/signin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
