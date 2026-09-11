"""Cross-tenant clip playback (Phase 4) — the one feature that hands another
tenant a file.

Everything else in the peer channel moves text this instance composed. This
moves the actual recording, so the tests are mostly about the credential:

  * a grant is minted by a HUMAN approving an answer, never by an auto policy;
  * it is scoped to the events behind THAT answer, expires, and dies with the
    ask or the pairing;
  * it is stored hash-only, and it is not a substitute for the pairing token;
  * every refusal looks identical, so the endpoint is not an oracle for which
    recordings a tenant holds.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import peer_channel as pch  # noqa: E402
from app.services import peer_clip  # noqa: E402
from tests.test_peer_channel import PeerChannelBase  # noqa: E402

NOW = 1_757_000_000.0


class ClipBase(PeerChannelBase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["QUILL_PEER_CLIP_GRANTS"] = str(
            Path(self._tmp) / "grants.json")
        os.environ["QUILL_PEER_CLIP_PLAYBACK"] = "1"

    def tearDown(self) -> None:
        for k in ("QUILL_PEER_CLIP_GRANTS", "QUILL_PEER_CLIP_PLAYBACK",
                  "QUILL_PEER_CLIP_TTL_S"):
            os.environ.pop(k, None)
        super().tearDown()


class GrantScopeTests(ClipBase):
    def test_a_grant_covers_only_the_events_it_names(self) -> None:
        g = peer_clip.mint("p1", "a1", [11, 12])
        self.assertTrue(peer_clip.check("p1", g["token"], 11)[0])
        self.assertTrue(peer_clip.check("p1", g["token"], 12)[0])
        # Not a standing read grant: a neighbouring event is still private.
        self.assertFalse(peer_clip.check("p1", g["token"], 13)[0])

    def test_a_grant_is_bound_to_the_peer_it_was_issued_to(self) -> None:
        g = peer_clip.mint("p1", "a1", [11])
        self.assertFalse(peer_clip.check("p2", g["token"], 11)[0])

    def test_an_unknown_token_is_refused(self) -> None:
        peer_clip.mint("p1", "a1", [11])
        self.assertFalse(peer_clip.check("p1", "made-up-token", 11)[0])
        self.assertFalse(peer_clip.check("p1", "", 11)[0])

    def test_the_token_is_stored_hash_only(self) -> None:
        """Same posture as the pairing token: the plaintext exists once, in
        the answer payload."""
        g = peer_clip.mint("p1", "a1", [11])
        raw = Path(os.environ["QUILL_PEER_CLIP_GRANTS"]).read_text()
        self.assertNotIn(g["token"], raw)
        self.assertIn(peer_clip._hash(g["token"]), raw)

    def test_scope_is_bounded(self) -> None:
        g = peer_clip.mint("p1", "a1", list(range(100)))
        self.assertEqual(len(g["events"]), peer_clip.MAX_SCOPE)

    def test_an_answer_with_nothing_playable_mints_nothing(self) -> None:
        """A grant naming no event would be a credential to nothing."""
        self.assertIsNone(peer_clip.mint("p1", "a1", []))
        self.assertIsNone(peer_clip.mint("p1", "a1", ["not-an-int"]))

    def test_playback_off_mints_nothing_and_checks_nothing(self) -> None:
        os.environ["QUILL_PEER_CLIP_PLAYBACK"] = "0"
        self.assertIsNone(peer_clip.mint("p1", "a1", [11]))
        self.assertFalse(peer_clip.check("p1", "anything", 11)[0])


class ExpiryAndRevocationTests(ClipBase):
    def test_a_grant_expires(self) -> None:
        g = peer_clip.mint("p1", "a1", [11], ttl_s=60, now=NOW)
        self.assertTrue(peer_clip.check("p1", g["token"], 11, now=NOW + 30)[0])
        self.assertFalse(peer_clip.check("p1", g["token"], 11, now=NOW + 61)[0])

    def test_revoking_the_ask_kills_its_grants(self) -> None:
        g = peer_clip.mint("p1", "a1", [11])
        other = peer_clip.mint("p1", "a2", [21])
        self.assertEqual(peer_clip.revoke_for_ask("a1"), 1)
        self.assertFalse(peer_clip.check("p1", g["token"], 11)[0])
        # Scoped to that ask only.
        self.assertTrue(peer_clip.check("p1", other["token"], 21)[0])

    def test_unpairing_kills_every_grant_that_peer_holds(self) -> None:
        """A clip grant is a separate credential with its own expiry, so
        unpairing without revoking would leave a live key behind."""
        claim = self._claimed_peer()
        peer_id = next(iter(self._registry()))
        g = peer_clip.mint(peer_id, "a1", [11])
        self.assertTrue(peer_clip.check(peer_id, g["token"], 11)[0])
        self.assertTrue(pch.revoke(peer_id))
        self.assertFalse(peer_clip.check(peer_id, g["token"], 11)[0])
        self.assertTrue(claim["ok"])

    def test_active_grants_report_metadata_only(self) -> None:
        g = peer_clip.mint("p1", "a1", [11])
        rows = peer_clip.active_grants()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("token_sha256", rows[0])
        self.assertNotIn(g["token"], json.dumps(rows))
        self.assertEqual(rows[0]["event_ids"], [11])

    def test_prune_drops_dead_rows(self) -> None:
        peer_clip.mint("p1", "a1", [11], ttl_s=10, now=NOW)
        self.assertEqual(peer_clip.prune(now=NOW + 11), 1)
        self.assertEqual(peer_clip.active_grants(now=NOW + 11), [])


class MintingPolicyTests(ClipBase):
    """WHO may mint is the whole design: a human, never a policy."""

    def _peer(self) -> dict:
        claim = self._claimed_peer()
        return pch.authenticate(f"Bearer {claim['token']}")

    def _composed(self) -> dict:
        return {"text": "Compute is free through November.",
                "claims": [{"fact_id": 1, "source_event_id": 77,
                            "text": "Compute is free through November",
                            "speaker": "Andy", "ts": NOW, "when": "today"}],
                "as_of": NOW, "near_miss": False, "redacted": []}

    def test_human_approval_mints_a_grant_for_the_approved_events(self) -> None:
        peer = self._peer()
        pch.handle_ask(peer, {"ask_id": "x1", "question": "compute?"})
        local_id = pch.pending_asks()[0]["id"]
        sent: list = []
        with mock.patch.object(pch, "_deliver",
                               side_effect=lambda rec, p: sent.append(p) or True), \
             mock.patch.object(pch, "compose_answer",
                               return_value=self._composed()), \
             mock.patch.object(peer_clip, "playable_path",
                               return_value="/data/clips/77.wav"):
            pch.decide_ask(local_id, True)
        grant = sent[0].get("clip_grant")
        self.assertIsNotNone(grant, "approval should mint a grant")
        self.assertEqual(grant["events"], [77])
        self.assertTrue(grant["token"])

    def test_an_auto_answered_ask_mints_nothing(self) -> None:
        """A pack that auto-answers work questions is consent to ANSWER, not
        consent to hand over recordings."""
        peer = self._peer()
        auto = mock.MagicMock(wraps=pch.settings)
        with mock.patch.object(pch, "compose_answer",
                              return_value=self._composed()), \
             mock.patch.object(pch, "_decide_action",
                               return_value=("auto", "work")):
            res = pch.handle_ask(peer, {"ask_id": "x2", "question": "compute?"})
        self.assertEqual(res["status"], "answered")
        self.assertNotIn("clip_grant", res)
        self.assertEqual(peer_clip.active_grants(), [])
        self.assertTrue(auto)

    def test_a_claim_with_no_playable_audio_is_not_granted(self) -> None:
        peer = self._peer()
        pch.handle_ask(peer, {"ask_id": "x3", "question": "compute?"})
        local_id = pch.pending_asks()[0]["id"]
        sent: list = []
        with mock.patch.object(pch, "_deliver",
                               side_effect=lambda rec, p: sent.append(p) or True), \
             mock.patch.object(pch, "compose_answer",
                               return_value=self._composed()), \
             mock.patch.object(peer_clip, "playable_path", return_value=None):
            pch.decide_ask(local_id, True)
        self.assertNotIn("clip_grant", sent[0])

    def test_declining_mints_nothing(self) -> None:
        peer = self._peer()
        pch.handle_ask(peer, {"ask_id": "x4", "question": "compute?"})
        local_id = pch.pending_asks()[0]["id"]
        with mock.patch.object(pch, "_deliver", return_value=True):
            pch.decide_ask(local_id, False)
        self.assertEqual(peer_clip.active_grants(), [])


class InboundGrantTests(ClipBase):
    def test_a_grant_arriving_with_an_answer_is_stored_with_it(self) -> None:
        claim = self._claimed_peer()
        peer_id = next(iter(self._registry()))
        with mock.patch.object(
                pch, "_post_peer",
                return_value={"ok": True, "status": "answered",
                              "answer": "- Compute is free through November",
                              "claims": [{"text": "Compute is free",
                                          "source_event_id": 77}],
                              "as_of": NOW,
                              "clip_grant": {"token": "t" * 40,
                                             "events": [77],
                                             "expires_at": NOW + 3600}}), \
             mock.patch.object(pch, "enrich_peer_question", side_effect=lambda q: q):
            res = pch.ask(peer_id, "what's the compute situation?")
        row = pch.sent_ask(res["ask_id"])
        self.assertEqual(row["clip_grant"]["events"], [77])
        self.assertTrue(claim["ok"])

    def test_a_malformed_grant_is_dropped_not_stored(self) -> None:
        for bad in (None, "nope", {}, {"token": ""}, {"events": [1]},
                    {"token": "t" * 40, "events": []},
                    {"token": "x" * 500, "events": [1]}):
            self.assertIsNone(pch._sanitize_grant(bad), bad)

    def test_grant_scope_is_bounded_on_the_way_in(self) -> None:
        got = pch._sanitize_grant({"token": "t" * 40,
                                   "events": list(range(50))})
        self.assertEqual(len(got["events"]), 12)


class ClipPathTests(unittest.TestCase):
    """A clip path comes from a capture pipeline's metadata, so it is still
    re-confined before anything is streamed."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.mine = self.root / "clips" / "77.wav"
        self.mine.parent.mkdir(parents=True)
        self.mine.write_bytes(b"RIFF....audio")
        self.outside = Path(tempfile.mkdtemp()) / "secret.wav"
        self.outside.write_bytes(b"RIFF....not mine")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _resolve(self, path):
        from types import SimpleNamespace
        with mock.patch.object(peer_clip, "playable_path", return_value=path), \
             mock.patch.object(peer_clip, "settings",
                               SimpleNamespace(storage=SimpleNamespace(
                                   data_dir=str(self.root)))):
            return peer_clip.resolve_for_send(77)

    def test_a_clip_inside_the_data_dir_resolves(self) -> None:
        self.assertEqual(self._resolve(str(self.mine)), self.mine.resolve())

    def test_a_path_outside_the_data_dir_is_refused(self) -> None:
        """Otherwise a clip grant becomes an arbitrary file read."""
        self.assertIsNone(self._resolve(str(self.outside)))

    def test_traversal_is_refused(self) -> None:
        self.assertIsNone(self._resolve(str(self.root / ".." / "etc" / "passwd")))

    def test_a_missing_file_resolves_to_nothing(self) -> None:
        self.assertIsNone(self._resolve(str(self.root / "clips" / "gone.wav")))

    def test_no_path_resolves_to_nothing(self) -> None:
        self.assertIsNone(self._resolve(None))


class RouteTests(ClipBase):
    """The two routes, driven directly: the answerer's gated serve and the
    asker's play. This is where the credential pair is actually enforced."""

    def setUp(self) -> None:
        super().setUp()
        from app.api import routes
        self.routes = routes
        self.clip = Path(self._tmp) / "clips" / "77.wav"
        self.clip.parent.mkdir(parents=True, exist_ok=True)
        self.clip.write_bytes(b"RIFF....the actual moment")
        claim = self._claimed_peer()
        self.peer = pch.authenticate(f"Bearer {claim['token']}")
        self.token = claim["token"]
        self.peer_id = self.peer["peer_id"]

    def _serve(self, token: str, event_id, auth: str | None = None):
        from fastapi import HTTPException
        with mock.patch.object(peer_clip, "clip_bytes_for_send",
                               return_value=(b"RIFF....trimmed", "audio/wav")):
            try:
                return self.routes.peer_clip_inbound(
                    {"token": token, "event_id": event_id},
                    authorization=auth or f"Bearer {self.token}"), None
            except HTTPException as exc:
                return None, exc

    def test_a_valid_grant_plus_pairing_token_serves_the_clip(self) -> None:
        g = peer_clip.mint(self.peer_id, "a1", [77])
        got, exc = self._serve(g["token"], 77)
        self.assertIsNone(exc)
        self.assertEqual(got.body, b"RIFF....trimmed")

    def test_the_approved_span_reaches_the_trimmer(self) -> None:
        """What is served is bound to the span recorded at approval time, not
        to anything the requesting peer supplies."""
        peer_clip.mint(self.peer_id, "a1",
                       [{"event_id": 77, "span": "compute is free"}])
        g2 = peer_clip.mint(self.peer_id, "a2",
                            [{"event_id": 78, "span": "the launch slipped"}])
        with mock.patch.object(peer_clip, "clip_bytes_for_send",
                               return_value=(b"x", "audio/wav")) as cut:
            self.routes.peer_clip_inbound(
                {"token": g2["token"], "event_id": 78},
                authorization=f"Bearer {self.token}")
        self.assertEqual(cut.call_args.args[1], "the launch slipped")

    def test_the_grant_alone_is_not_enough(self) -> None:
        """Two independent credentials: a leaked grant is useless without the
        pairing token."""
        g = peer_clip.mint(self.peer_id, "a1", [77])
        got, exc = self._serve(g["token"], 77, auth="Bearer not-a-peer-token")
        self.assertIsNone(got)
        self.assertEqual(exc.status_code, 401)

    def test_the_pairing_token_alone_is_not_enough(self) -> None:
        """...and a paired peer with no grant gets nothing. This is the whole
        reason the exception to tenant isolation is acceptable."""
        got, exc = self._serve("no-such-grant", 77)
        self.assertIsNone(got)
        self.assertEqual(exc.status_code, 403)

    def test_an_event_outside_the_grant_is_refused(self) -> None:
        g = peer_clip.mint(self.peer_id, "a1", [77])
        got, exc = self._serve(g["token"], 78)
        self.assertIsNone(got)
        self.assertEqual(exc.status_code, 403)

    def test_every_refusal_is_indistinguishable(self) -> None:
        """Expired, wrong event, and never-existed must not be tellable apart,
        or the endpoint becomes an oracle for what this tenant recorded."""
        g = peer_clip.mint(self.peer_id, "a1", [77], ttl_s=1, now=NOW)
        details = set()
        for token, eid in ((g["token"], 78), ("nope", 77), ("", 77)):
            _got, exc = self._serve(token, eid)
            details.add(exc.detail)
        self.assertEqual(len(details), 1, details)

    def test_the_asker_can_play_a_granted_clip(self) -> None:
        """End to end: the grant that arrived with an answer is spent to pull
        the other tenant's recording, and nothing is written to our disk."""
        with mock.patch.object(
                pch, "_post_peer",
                return_value={"ok": True, "status": "answered",
                              "answer": "- Compute is free",
                              "claims": [{"text": "Compute is free",
                                          "source_event_id": 77}],
                              "as_of": NOW,
                              "clip_grant": {"token": "t" * 40, "events": [77],
                                             "expires_at": NOW + 3600}}), \
             mock.patch.object(pch, "enrich_peer_question", side_effect=lambda q: q):
            res = pch.ask(self.peer_id, "what's the compute situation?")
        with mock.patch.object(peer_clip, "fetch_from_peer",
                               return_value=(b"RIFF....the actual moment",
                                             "audio/wav")) as fetch:
            out = self.routes.peer_clip_play(ask_id=res["ask_id"], event_id=77)
        self.assertEqual(out.body, b"RIFF....the actual moment")
        self.assertEqual(out.media_type, "audio/wav")
        # The grant we were given is what authorised it.
        self.assertEqual(fetch.call_args.args[1]["token"], "t" * 40)

    def test_the_asker_cannot_play_an_event_outside_the_grant(self) -> None:
        from fastapi import HTTPException
        with mock.patch.object(
                pch, "_post_peer",
                return_value={"ok": True, "status": "answered",
                              "answer": "- x", "claims": [],
                              "clip_grant": {"token": "t" * 40, "events": [77],
                                             "expires_at": NOW + 3600}}), \
             mock.patch.object(pch, "enrich_peer_question", side_effect=lambda q: q):
            res = pch.ask(self.peer_id, "compute?")
        with mock.patch.object(peer_clip, "fetch_from_peer") as fetch, \
                self.assertRaises(HTTPException) as ctx:
            self.routes.peer_clip_play(ask_id=res["ask_id"], event_id=99)
        self.assertEqual(ctx.exception.status_code, 403)
        fetch.assert_not_called()

    def test_playing_an_answer_that_carried_no_grant_is_a_404(self) -> None:
        from fastapi import HTTPException
        with mock.patch.object(
                pch, "_post_peer",
                return_value={"ok": True, "status": "answered",
                              "answer": "- x", "claims": []}), \
             mock.patch.object(pch, "enrich_peer_question", side_effect=lambda q: q):
            res = pch.ask(self.peer_id, "compute?")
        with self.assertRaises(HTTPException) as ctx:
            self.routes.peer_clip_play(ask_id=res["ask_id"], event_id=77)
        self.assertEqual(ctx.exception.status_code, 404)


class GrantExposureTests(ClipBase):
    """The grant is a credential to another tenant's recordings. It is stored,
    spent server-side, and never handed to a browser."""

    def test_the_ui_projection_omits_the_token(self) -> None:
        claim = self._claimed_peer()
        peer_id = next(iter(self._registry()))
        with mock.patch.object(
                pch, "_post_peer",
                return_value={"ok": True, "status": "answered",
                              "answer": "- x", "claims": [], "as_of": NOW,
                              "clip_grant": {"token": "SECRET" * 8,
                                             "events": [77],
                                             "expires_at": NOW + 3600}}), \
             mock.patch.object(pch, "enrich_peer_question", side_effect=lambda q: q):
            pch.ask(peer_id, "compute?")
        rows = pch.answers()
        blob = json.dumps(rows)
        self.assertNotIn("SECRET", blob)
        # ...but the UI still learns which clips it may offer, and when.
        self.assertEqual(rows[-1]["clip_grant"]["events"], [77])
        self.assertEqual(rows[-1]["as_of"], NOW)
        self.assertTrue(claim["ok"])

    def test_an_answer_without_a_grant_shows_no_clip_affordance(self) -> None:
        claim = self._claimed_peer()
        peer_id = next(iter(self._registry()))
        with mock.patch.object(
                pch, "_post_peer",
                return_value={"ok": True, "status": "answered",
                              "answer": "- x", "claims": []}), \
             mock.patch.object(pch, "enrich_peer_question", side_effect=lambda q: q):
            pch.ask(peer_id, "compute?")
        self.assertNotIn("clip_grant", pch.answers()[-1])
        self.assertTrue(claim["ok"])

    def test_the_page_plays_through_the_server_side_route(self) -> None:
        """The player must point at /peer/clip/play, which resolves the grant
        by ask_id — not at anything that would need the token in the page."""
        from app.api.peer_page import PEER_PAGE
        self.assertIn("/peer/clip/play?ask_id=", PEER_PAGE)
        self.assertNotIn("clip_grant.token", PEER_PAGE)
        self.assertNotIn("g.token", PEER_PAGE)


class SpanTrimTests(unittest.TestCase):
    """A captured moment holds more than the claim: other speakers, adjacent
    conversation. The approver said yes to a claim, so only the words behind
    that claim are sent."""

    def setUp(self) -> None:
        import wave
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.wav = self.root / "clip.wav"
        # 10 seconds of 8 kHz mono silence — long enough to prove a window was
        # actually cut out of the middle.
        with wave.open(str(self.wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 8000 * 10)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _dur(self, data: bytes) -> float:
        import io
        import wave
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.getnframes() / w.getframerate()

    def test_trim_returns_only_the_requested_window(self) -> None:
        out = peer_clip.trim_wav(self.wav, 3.0, 5.0)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(self._dur(out), 2.0, places=2)
        self.assertLess(len(out), self.wav.stat().st_size)

    def test_trim_clamps_to_the_recording(self) -> None:
        out = peer_clip.trim_wav(self.wav, -5.0, 999.0)
        self.assertAlmostEqual(self._dur(out), 10.0, places=2)

    def test_trim_refuses_an_empty_window(self) -> None:
        self.assertIsNone(peer_clip.trim_wav(self.wav, 5.0, 5.0))
        self.assertIsNone(peer_clip.trim_wav(self.wav, 8.0, 2.0))

    def test_trim_returns_nothing_for_a_non_wav(self) -> None:
        other = self.root / "clip.m4a"
        other.write_bytes(b"not really audio")
        self.assertIsNone(peer_clip.trim_wav(other, 0.0, 1.0))

    def test_word_timestamps_give_an_exact_window(self) -> None:
        words = [{"word": "the", "start": 0.0, "end": 0.2},
                 {"word": "compute", "start": 4.0, "end": 4.5},
                 {"word": "is", "start": 4.5, "end": 4.7},
                 {"word": "free", "start": 4.7, "end": 5.0},
                 {"word": "unrelated", "start": 8.0, "end": 8.4}]
        store = mock.MagicMock()
        store.get_event.return_value = {"meta": {"word_timestamps": words},
                                        "raw": "the compute is free unrelated"}
        got = peer_clip.span_window(77, "compute is free", store=store)
        self.assertIsNotNone(got)
        start, end = got
        # The approved words, plus a little breathing room either side.
        self.assertAlmostEqual(start, 4.0 - peer_clip.SPAN_PAD_S, places=2)
        self.assertAlmostEqual(end, 5.0 + peer_clip.SPAN_PAD_S, places=2)
        # And decisively not the neighbouring sentence.
        self.assertLess(end, 8.0)

    def test_word_matching_ignores_punctuation_and_case(self) -> None:
        words = [{"word": " Compute,", "start": 1.0, "end": 1.4},
                 {"word": " free!", "start": 1.4, "end": 1.8}]
        store = mock.MagicMock()
        store.get_event.return_value = {"meta": {"word_timestamps": words},
                                        "raw": "Compute, free!"}
        self.assertIsNotNone(peer_clip.span_window(77, "compute free",
                                                   store=store))

    def test_a_span_not_in_the_words_falls_through(self) -> None:
        words = [{"word": "something", "start": 1.0, "end": 1.4}]
        store = mock.MagicMock()
        store.get_event.return_value = {"meta": {"word_timestamps": words},
                                        "raw": "something"}
        with mock.patch.object(peer_clip, "playable_path", return_value=None):
            self.assertIsNone(peer_clip.span_window(77, "entirely different",
                                                    store=store))

    def test_without_word_timestamps_the_window_is_estimated(self) -> None:
        """Approximate, but still far narrower than the whole moment."""
        transcript = ("filler " * 40) + "compute is free " + ("filler " * 40)
        store = mock.MagicMock()
        store.get_event.return_value = {"meta": {}, "raw": transcript}
        with mock.patch.object(peer_clip, "playable_path",
                               return_value=str(self.wav)):
            got = peer_clip.span_window(77, "compute is free", store=store)
        self.assertIsNotNone(got)
        start, end = got
        self.assertGreater(start, 2.0)
        self.assertLess(end, 8.0)

    def test_an_unlocatable_span_sends_nothing_by_default(self) -> None:
        """Span-only is the default: shipping the surrounding conversation
        because trimming was inconvenient is not what the approver agreed to."""
        store = mock.MagicMock()
        store.get_event.return_value = {"meta": {}, "raw": "nothing matching"}
        with mock.patch.object(peer_clip, "resolve_for_send",
                               return_value=self.wav), \
             mock.patch.object(peer_clip, "playable_path",
                               return_value=str(self.wav)):
            self.assertIsNone(peer_clip.clip_bytes_for_send(
                77, "a span that is not there", store=store))

    def test_span_only_can_be_turned_off(self) -> None:
        os.environ["QUILL_PEER_CLIP_SPAN_ONLY"] = "0"
        try:
            store = mock.MagicMock()
            store.get_event.return_value = {"meta": {}, "raw": "nothing"}
            with mock.patch.object(peer_clip, "resolve_for_send",
                                   return_value=self.wav), \
                 mock.patch.object(peer_clip, "playable_path",
                                   return_value=str(self.wav)):
                got = peer_clip.clip_bytes_for_send(77, "absent", store=store)
            self.assertIsNotNone(got)
            self.assertAlmostEqual(self._dur(got[0]), 10.0, places=2)
        finally:
            os.environ.pop("QUILL_PEER_CLIP_SPAN_ONLY", None)

    def test_the_sent_clip_is_the_trimmed_one(self) -> None:
        words = [{"word": "compute", "start": 4.0, "end": 4.5},
                 {"word": "free", "start": 4.5, "end": 5.0}]
        store = mock.MagicMock()
        store.get_event.return_value = {"meta": {"word_timestamps": words},
                                        "raw": "compute free"}
        with mock.patch.object(peer_clip, "resolve_for_send",
                               return_value=self.wav):
            got = peer_clip.clip_bytes_for_send(77, "compute free", store=store)
        self.assertIsNotNone(got)
        data, ctype = got
        self.assertEqual(ctype, "audio/wav")
        self.assertAlmostEqual(self._dur(data), 1.0 + 2 * peer_clip.SPAN_PAD_S,
                               places=1)


class FetchBoundsTests(ClipBase):
    def test_an_oversized_clip_is_refused_rather_than_truncated(self) -> None:
        """A truncated WAV would play as a corrupt clip; refusing is honest."""
        os.environ["QUILL_PEER_CLIP_MAX_BYTES"] = "10"
        try:
            body = b"x" * 500

            class _Resp:
                headers = {"Content-Type": "audio/wav"}

                def read(self, n):
                    return body[:n]

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            with mock.patch("urllib.request.urlopen", return_value=_Resp()):
                got = peer_clip.fetch_from_peer(
                    {"base_url": "http://h:1", "outbound_token": "t"},
                    {"token": "g"}, 77)
            self.assertIsNone(got)
        finally:
            os.environ.pop("QUILL_PEER_CLIP_MAX_BYTES", None)

    def test_no_grant_token_means_no_request(self) -> None:
        with mock.patch("urllib.request.urlopen") as open_:
            self.assertIsNone(peer_clip.fetch_from_peer(
                {"base_url": "http://h:1"}, {}, 77))
        open_.assert_not_called()


if __name__ == "__main__":
    unittest.main()
