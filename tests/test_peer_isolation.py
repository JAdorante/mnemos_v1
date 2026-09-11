"""Tenant isolation across the peer boundary (Phase 0.3) — a REGRESSION GUARD.

Two tenants on one hosted box makes it cheap to let a peer read the other
tenant's database or artifacts directly, and that shortcut silently turns the
product into a shared database: the disclosure gate stops being the thing that
decides what crosses, and the security story becomes fiction.

Nothing here is currently broken. These tests exist so it stays that way, and
specifically so that Phase 4 (clip playback across tenants) has to cross this
boundary DELIBERATELY — by minting a scoped capability token — rather than by
widening one of these surfaces by accident.

Pinned:
  * exactly three routes accept a peer token, and each is a gated verb;
  * a peer token is not an API token and vice versa;
  * an inbound ask under the default policy returns no memory text at all;
  * /artifact is confined to this tenant's data dir (traversal + symlink);
  * there is no route serving another tenant's artifact by peer token.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import peer_channel as pch  # noqa: E402
from tests.test_peer_channel import PeerChannelBase  # noqa: E402

# The complete set of peer-token-authenticated routes. Adding one is a
# deliberate act: it must be a gated verb, never a read of stored memory.
PEER_AUTHED_ROUTES = {"/peer/ask", "/peer/ping", "/peer/answer", "/peer/clip"}

# `/peer/clip` (Phase 4) is the one route that hands another tenant a file.
# It was added by deliberately updating this list — which is what this guard
# exists to force. The pairing token alone must never be enough to reach it:
# a second, human-minted capability grant is required, so the exception is
# bounded rather than a new standing read path.
FILE_SERVING_PEER_ROUTES = {"/peer/clip"}


class PeerSurfaceTests(unittest.TestCase):
    def _peer_routes(self) -> set[str]:
        """Every route whose handler authenticates a PEER token."""
        import inspect

        from app.api import routes as r
        src = inspect.getsource(r)
        found: set[str] = set()
        blocks = src.split("@router.")
        for b in blocks:
            if "peer_channel.authenticate(" not in b:
                continue
            first = b.split("\n", 1)[0]
            if '"' not in first:
                continue
            found.add(first.split('"')[1])
        return found

    def test_only_known_routes_accept_a_peer_token(self) -> None:
        self.assertEqual(self._peer_routes(), PEER_AUTHED_ROUTES,
                         "a new peer-authenticated route appeared — it must be "
                         "a gated verb, not a read of stored memory")

    def _peer_blocks(self) -> dict[str, str]:
        """{route path: handler source} for peer-authenticated routes."""
        import inspect

        from app.api import routes as r
        out: dict[str, str] = {}
        for block in inspect.getsource(r).split("@router.")[1:]:
            if "peer_channel.authenticate(" not in block:
                continue
            head = block.split("\n", 1)[0]
            if '"' in head:
                out[head.split('"')[1]] = block
        return out

    def test_only_the_clip_route_may_serve_a_file(self) -> None:
        """Phase 4 built the one cross-tenant read path. Any OTHER peer route
        that starts returning files is a new one, and must not appear by
        accident — building it means minting a scoped capability, never
        relaxing /artifact or riding on the pairing token."""
        for path, block in self._peer_blocks().items():
            if path in FILE_SERVING_PEER_ROUTES:
                continue
            self.assertNotIn("artifact", path.lower())
            self.assertNotIn("FileResponse", block,
                             f"{path}: a peer-authenticated route returning a "
                             "file is a cross-tenant artifact read")

    def test_the_clip_route_requires_a_capability_not_just_pairing(self) -> None:
        """The exception is only acceptable because the pairing token alone
        does not open it."""
        block = self._peer_blocks()["/peer/clip"]
        self.assertIn("peer_clip.check_scope(", block)
        # The grant is checked BEFORE any file is touched, so an invalid grant
        # never reaches the filesystem.
        self.assertLess(block.index("peer_clip.check_scope("),
                        block.index("clip_bytes_for_send("))

    def test_the_clip_route_gives_the_same_refusal_every_way_it_fails(self) -> None:
        """Distinguishing 'expired' from 'wrong event' from 'never existed'
        would make this endpoint an oracle for which recordings a tenant has."""
        block = self._peer_blocks()["/peer/clip"]
        details = re.findall(r'status_code=403, detail="([^"]+)"', block)
        self.assertGreaterEqual(len(details), 2)
        self.assertEqual(len(set(details)), 1, details)


class PeerTokenScopeTests(PeerChannelBase):
    def test_unknown_token_authenticates_to_nothing(self) -> None:
        self._claimed_peer()
        self.assertIsNone(pch.authenticate("Bearer not-a-real-token"))
        self.assertIsNone(pch.authenticate(None))
        self.assertIsNone(pch.authenticate("Basic abc"))

    def test_peer_token_is_not_the_api_token(self) -> None:
        """A peer's token opens the three peer verbs, never the LAN API."""
        claim = self._claimed_peer()
        from app.services import api_auth
        self.assertFalse(api_auth.token_matches(claim["token"]))
        self.assertFalse(api_auth.session_matches(claim["token"]))

    def test_default_policy_ask_returns_no_memory_text(self) -> None:
        """The whole posture in one assertion: an inbound ask queues for the
        human and the answering tenant's memory does not move."""
        self._claimed_peer()
        reg = self._registry()
        peer_id = next(iter(reg))
        res = pch.handle_ask({"peer_id": peer_id, **reg[peer_id]},
                             {"ask_id": "i1",
                              "question": "what is their home address?"})
        self.assertEqual(res["status"], "pending")
        self.assertNotIn("answer", res)

    def test_inbound_answer_must_match_an_ask_we_sent(self) -> None:
        """Otherwise a paired peer can push arbitrary text into our memory."""
        self._claimed_peer()
        reg = self._registry()
        peer_id = next(iter(reg))
        res = pch.handle_answer({"peer_id": peer_id, **reg[peer_id]},
                                {"ask_id": "never-sent",
                                 "answer": "Their salary is $220k."})
        self.assertFalse(res.get("ok"), res)


class ArtifactConfinementTests(unittest.TestCase):
    """`/artifact` serves this tenant's clips. Each tenant is its own container
    with its own volume, so the path guard is the second line of defence — but
    it is the one that survives a future single-process multi-tenant mode."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="tenant_a_")
        self._other = tempfile.mkdtemp(prefix="tenant_b_")
        self._secret = Path(self._other) / "theirs.wav"
        self._secret.write_bytes(b"RIFF....their private audio")
        self._mine = Path(self._tmp) / "mine.wav"
        self._mine.write_bytes(b"RIFF....my audio")

    def _artifact(self, path: str):
        from types import SimpleNamespace
        from unittest import mock

        from app.api import routes
        from fastapi import HTTPException
        # settings is a frozen dataclass — patch the module reference, never
        # the field (and never reload app.config, which leaks a stale frozen
        # settings across the whole session).
        fake = SimpleNamespace(storage=SimpleNamespace(data_dir=self._tmp))
        with mock.patch.object(routes, "settings", fake):
            try:
                return routes.artifact(path=path), None
            except HTTPException as exc:
                return None, exc

    def test_serves_a_clip_inside_this_tenants_data_dir(self) -> None:
        got, exc = self._artifact(str(self._mine))
        self.assertIsNone(exc)
        self.assertIsNotNone(got)

    def test_refuses_another_tenants_data_dir(self) -> None:
        got, exc = self._artifact(str(self._secret))
        self.assertIsNone(got)
        self.assertEqual(exc.status_code, 403)

    def test_refuses_dotdot_traversal_out_of_the_data_dir(self) -> None:
        sneaky = str(Path(self._tmp) / ".." /
                     Path(self._other).name / "theirs.wav")
        got, exc = self._artifact(sneaky)
        self.assertIsNone(got)
        self.assertEqual(exc.status_code, 403)

    def test_refuses_a_symlink_pointing_at_another_tenant(self) -> None:
        link = Path(self._tmp) / "link.wav"
        try:
            link.symlink_to(self._secret)
        except OSError:
            self.skipTest("symlinks unavailable on this filesystem")
        got, exc = self._artifact(str(link))
        self.assertIsNone(got, "a symlink walked out of the tenant's data dir")
        self.assertEqual(exc.status_code, 403)


if __name__ == "__main__":
    unittest.main()
