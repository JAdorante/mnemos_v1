"""Unit tests for app.services.phone_channel — the direct phone -> Sparrow
channel (pairing, per-device tokens, authenticated ingest).

Pin the trust model: codes are single-use, expiring, and brute-force-limited;
tokens are stored hash-only and die on revoke; ingest accepts only the typed
kinds, whitelists meta, clips text, and lands events as context (SYSTEM
modality, confidence contract attached) — never as commands.

Run with either:
    python -m unittest discover -s tests        # zero dependencies
    pytest tests/                               # if pytest is installed
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.events import Modality, bus  # noqa: E402
from app.services import phone_channel as pc  # noqa: E402


class PhoneChannelBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="phone_")
        os.environ["QUILL_PHONE_DEVICES"] = str(Path(self._tmp) / "devices.json")
        os.environ["QUILL_PHONE_OUTBOX"] = str(Path(self._tmp) / "outbox.json")
        pc._pairing = None
        self.events: list = []
        bus.subscribe(self._collect)

    def tearDown(self) -> None:
        os.environ.pop("QUILL_PHONE_DEVICES", None)
        os.environ.pop("QUILL_PHONE_OUTBOX", None)
        bus._subscribers.remove(self._collect)
        pc._pairing = None

    def _collect(self, ev) -> None:
        self.events.append(ev)

    def _pair(self, name: str = "Test iPhone", platform: str = "ios") -> dict:
        start = pc.start_pairing()
        self.assertTrue(start["ok"], start)
        claim = pc.claim_pairing(start["code"], name, platform)
        self.assertTrue(claim["ok"], claim)
        return claim


class PairingTests(PhoneChannelBase):
    def test_start_shape(self) -> None:
        r = pc.start_pairing()
        self.assertTrue(r["ok"])
        self.assertRegex(r["code"], r"^\d{6}$")
        self.assertIn(r["code"], r["setup_url"])
        self.assertIn("/phone/setup", r["setup_url"])

    def test_claim_roundtrip_and_single_use(self) -> None:
        start = pc.start_pairing()
        claim = pc.claim_pairing(start["code"], "My iPhone", "ios")
        self.assertTrue(claim["ok"])
        self.assertTrue(claim["token"])
        # The registry stores only the hash, never the token.
        on_disk = json.loads(
            Path(os.environ["QUILL_PHONE_DEVICES"]).read_text(encoding="utf-8"))
        blob = json.dumps(on_disk)
        self.assertNotIn(claim["token"], blob)
        self.assertIn("token_sha256", blob)
        # The code was consumed — the same code cannot pair a second device.
        again = pc.claim_pairing(start["code"], "Evil twin", "ios")
        self.assertFalse(again["ok"])

    def test_wrong_code_lockout(self) -> None:
        pc.start_pairing()
        last: dict = {}
        for _ in range(10):
            last = pc.claim_pairing("000000" , "x")
            if "cancelled" in (last.get("error") or ""):
                break
        self.assertFalse(last["ok"])
        self.assertIn("cancelled", last["error"])
        self.assertFalse(pc.pairing_active())

    def test_expired_code_refused(self) -> None:
        start = pc.start_pairing()
        pc._pairing["expires_at"] = 0  # force expiry
        r = pc.claim_pairing(start["code"], "late phone")
        self.assertFalse(r["ok"])


class AuthTests(PhoneChannelBase):
    def test_token_authenticates(self) -> None:
        claim = self._pair()
        dev = pc.authenticate(f"Bearer {claim['token']}")
        self.assertIsNotNone(dev)
        self.assertEqual(dev["name"], "Test iPhone")
        self.assertEqual(dev["device_id"], claim["device_id"])

    def test_bad_or_malformed_tokens_refused(self) -> None:
        self._pair()
        for header in (None, "", "Bearer wrong-token", "Basic abc", "justtoken"):
            self.assertIsNone(pc.authenticate(header), header)

    def test_revoke_kills_token(self) -> None:
        claim = self._pair()
        self.assertTrue(pc.revoke(claim["device_id"]))
        self.assertIsNone(pc.authenticate(f"Bearer {claim['token']}"))
        self.assertFalse(pc.revoke(claim["device_id"]))  # already gone

    def test_devices_listing_redacts_hashes(self) -> None:
        self._pair()
        rows = pc.devices()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("token_sha256", rows[0])
        self.assertEqual(rows[0]["platform"], "ios")


class IngestTests(PhoneChannelBase):
    def setUp(self) -> None:
        super().setUp()
        claim = self._pair()
        self.device = pc.authenticate(f"Bearer {claim['token']}")

    def test_note_becomes_accepted_context_event(self) -> None:
        r = pc.ingest(self.device, {"kind": "note", "text": "Buy strings for the guitar"})
        self.assertTrue(r["ok"], r)
        ev = self.events[-1]
        self.assertEqual(ev.source, "phone.note")
        self.assertEqual(ev.modality, Modality.SYSTEM)
        self.assertEqual(ev.epistemic, "accepted")   # user told Sparrow directly
        self.assertEqual(ev.meta["origin"], "phone")
        self.assertEqual(ev.meta["device"], "Test iPhone")

    def test_share_is_observed_not_accepted(self) -> None:
        r = pc.ingest(self.device, {"kind": "share", "text": "Some article text",
                                    "meta": {"url": "https://x.com/a", "evil": "x"}})
        self.assertTrue(r["ok"])
        ev = self.events[-1]
        self.assertEqual(ev.epistemic, "observed")   # third-party content
        self.assertEqual(ev.meta["url"], "https://x.com/a")
        self.assertNotIn("evil", ev.meta)            # meta is whitelisted

    def test_location_builds_text_from_meta(self) -> None:
        r = pc.ingest(self.device, {"kind": "location",
                                    "meta": {"place": "Villanova", "lat": 40.04, "lon": -75.34}})
        self.assertTrue(r["ok"], r)
        self.assertIn("Villanova", self.events[-1].raw)

    def test_bad_payloads_refused(self) -> None:
        for payload in ({"kind": "hack", "text": "x"},        # unknown kind
                        {"kind": "note", "text": "   "},      # empty
                        {"kind": "location"},                  # no text, no meta
                        "not a dict"):
            r = pc.ingest(self.device, payload)  # type: ignore[arg-type]
            self.assertFalse(r["ok"], payload)

    def test_text_clipped_to_cap(self) -> None:
        from app.config import settings
        r = pc.ingest(self.device, {"kind": "note",
                                    "text": "x" * (settings.phone.max_text_chars + 500)})
        self.assertTrue(r["ok"])
        self.assertEqual(len(self.events[-1].raw), settings.phone.max_text_chars)

    def test_ingest_updates_device_bookkeeping(self) -> None:
        pc.ingest(self.device, {"kind": "note", "text": "hello"})
        row = pc.devices()[0]
        self.assertEqual(row["events"], 1)
        self.assertEqual(row["last_kind"], "note")
        self.assertIsNotNone(row["last_seen"])


class PhotoTests(PhoneChannelBase):
    """Photo upload -> VLM describe -> VISION event (source=phone.photo). The VLM
    is stubbed; the image bytes are arbitrary (the file is just written)."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["QUILL_PHONE_PHOTOS"] = str(Path(self._tmp) / "photos")
        claim = self._pair()
        self.device = pc.authenticate(f"Bearer {claim['token']}")

    def tearDown(self) -> None:
        os.environ.pop("QUILL_PHONE_PHOTOS", None)
        super().tearDown()

    def test_photo_becomes_vision_event(self) -> None:
        fake_vlm = mock.Mock()
        fake_vlm.describe.return_value = {
            "description": "a handwritten to-do list", "ocr_text": "buy strings\ncall Abby",
            "objects": ["paper", "pen"], "confidence": 0.8}
        with mock.patch.dict("sys.modules", {"app.services.vlm":
                                             mock.Mock(vlm=fake_vlm)}), \
             mock.patch.object(pc, "_downscale_jpeg", side_effect=lambda b, **k: b):
            res = pc.ingest_photo(self.device, b"\xff\xd8\xff\xe0fake-jpeg",
                                  caption="my notebook")
        self.assertTrue(res["ok"], res)
        ev = self.events[-1]
        self.assertEqual(ev.source, "phone.photo")
        self.assertEqual(ev.modality, Modality.VISION)
        self.assertEqual(ev.epistemic, "extracted")   # VLM read of an observed photo
        self.assertIn("buy strings", ev.raw)          # OCR text is the searchable body
        self.assertEqual(ev.meta["caption"], "my notebook")
        self.assertTrue(Path(res["path"]).is_file())

    def test_empty_and_oversize_and_heic_refused(self) -> None:
        self.assertFalse(pc.ingest_photo(self.device, b"")["ok"])
        from app.config import settings
        big = b"x" * (settings.phone.max_photo_bytes + 1)
        self.assertFalse(pc.ingest_photo(self.device, big)["ok"])
        self.assertFalse(pc.ingest_photo(self.device, b"data",
                                         content_type="image/heic")["ok"])

    def test_taken_at_times_the_event_and_records_location(self) -> None:
        fake_vlm = mock.Mock()
        fake_vlm.describe.return_value = {"description": "an old whiteboard",
                                          "ocr_text": "", "objects": [], "confidence": 0.7}
        old = 1_600_000_000.0   # Sept 2020 — long before "now"
        with mock.patch.dict("sys.modules", {"app.services.vlm":
                                             mock.Mock(vlm=fake_vlm)}), \
             mock.patch.object(pc, "_downscale_jpeg", side_effect=lambda b, **k: b):
            res = pc.ingest_photo(self.device, b"\xff\xd8jpeg", taken_at=old,
                                  lat=40.04, lon=-75.34)
        self.assertTrue(res["ok"])
        ev = self.events[-1]
        self.assertEqual(ev.time, old)              # lands at capture time, not now
        self.assertEqual(ev.meta["taken_at"], old)
        self.assertEqual(ev.meta["lat"], 40.04)
        self.assertGreater(ev.meta["uploaded_at"], old)  # upload is recent

    def test_taken_at_parsing_forms(self) -> None:
        self.assertEqual(pc._parse_taken_at(1_600_000_000), 1_600_000_000.0)
        self.assertEqual(pc._parse_taken_at(1_600_000_000_000),  # ms -> s
                         1_600_000_000.0)
        self.assertAlmostEqual(pc._parse_taken_at("2020-09-13T12:26:40+00:00"),
                               1_600_000_000.0, delta=1)
        self.assertIsNone(pc._parse_taken_at(""))
        self.assertIsNone(pc._parse_taken_at("not-a-date"))

    def test_photo_survives_vlm_failure(self) -> None:
        boom = mock.Mock()
        boom.describe.side_effect = RuntimeError("vlm down")
        with mock.patch.dict("sys.modules", {"app.services.vlm":
                                             mock.Mock(vlm=boom)}), \
             mock.patch.object(pc, "_downscale_jpeg", side_effect=lambda b, **k: b):
            res = pc.ingest_photo(self.device, b"\xff\xd8jpeg", caption="fallback")
        self.assertTrue(res["ok"])                     # still saved + landed
        self.assertEqual(self.events[-1].meta["caption"], "fallback")


class OutboxTests(PhoneChannelBase):
    """The reverse channel: desktop queues, the phone drains. Trust mirror of
    ingest — tokens can only READ their own queue, never enqueue."""

    def setUp(self) -> None:
        super().setUp()
        claim = self._pair()
        self.device = pc.authenticate(f"Bearer {claim['token']}")

    def test_queue_then_drain_marks_delivered(self) -> None:
        r = pc.queue_outbox("notify", "Mix feedback is ready")
        self.assertTrue(r["ok"], r)
        first = pc.drain_outbox(self.device)
        self.assertEqual(first["count"], 1)
        self.assertEqual(first["items"][0]["kind"], "notify")
        self.assertEqual(first["items"][0]["text"], "Mix feedback is ready")
        # Delivered means delivered: a second drain is empty.
        self.assertEqual(pc.drain_outbox(self.device)["count"], 0)

    def test_peek_does_not_mark(self) -> None:
        pc.queue_outbox("reminder", "buy guitar strings")
        self.assertEqual(pc.drain_outbox(self.device, peek=True)["count"], 1)
        self.assertEqual(pc.drain_outbox(self.device)["count"], 1)  # still there

    def test_device_targeting(self) -> None:
        other = self._pair("Second phone", "android")
        other_dev = pc.authenticate(f"Bearer {other['token']}")
        pc.queue_outbox("notify", "for the second phone only",
                        device_id=other["device_id"])
        # The first phone never sees a pinned item for another device.
        self.assertEqual(pc.drain_outbox(self.device)["count"], 0)
        self.assertEqual(pc.drain_outbox(other_dev)["count"], 1)

    def test_bad_items_refused(self) -> None:
        self.assertFalse(pc.queue_outbox("hack", "x")["ok"])       # unknown kind
        self.assertFalse(pc.queue_outbox("notify", "   ")["ok"])   # empty
        self.assertFalse(pc.queue_outbox("notify", "x",
                                         device_id="nope")["ok"])  # unknown device

    def test_pending_cap(self) -> None:
        from app.config import settings
        for i in range(settings.phone.max_outbox_pending):
            self.assertTrue(pc.queue_outbox("notify", f"item {i}")["ok"])
        self.assertFalse(pc.queue_outbox("notify", "one too many")["ok"])

    def test_query_roundtrip(self) -> None:
        # Sparrow asks; the phone drains the query and answers with kind="data",
        # linking back via meta.reply_to — the pseudo-read loop.
        q = pc.queue_outbox("query", "battery")
        self.assertTrue(q["ok"], q)
        drained = pc.drain_outbox(self.device)
        self.assertEqual(drained["items"][0]["kind"], "query")
        qid = drained["items"][0]["id"]
        r = pc.ingest(self.device, {"kind": "data", "text": "Battery at 68%",
                                    "meta": {"reply_to": qid, "name": "battery",
                                             "value": 68}})
        self.assertTrue(r["ok"], r)
        ev = self.events[-1]
        self.assertEqual(ev.source, "phone.data")
        self.assertEqual(ev.epistemic, "observed")  # measured, not asserted
        self.assertEqual(ev.meta["reply_to"], qid)
        self.assertEqual(ev.meta["value"], 68)

    def test_sync_exchange_sends_and_receives_in_one_call(self) -> None:
        # Queue something for the phone, then one exchange with text both sends
        # the note AND drains the queued item.
        pc.queue_outbox("notify", "Mix is ready")
        res = pc.sync_exchange(self.device, {"kind": "note", "text": "on my way"})
        self.assertTrue(res["ok"])
        self.assertTrue(res["sent"])                 # the note was ingested
        self.assertEqual(res["count"], 1)            # the queued item came back
        self.assertEqual(res["items"][0]["text"], "Mix is ready")
        # The sent note landed as a phone.note event.
        self.assertTrue(any(e.source == "phone.note" for e in self.events))

    def test_sync_exchange_empty_is_receive_only(self) -> None:
        pc.queue_outbox("reminder", "buy strings")
        before = len(self.events)
        res = pc.sync_exchange(self.device, {})
        self.assertFalse(res["sent"])                # nothing sent
        self.assertEqual(res["count"], 1)            # but still received
        self.assertEqual(len(self.events), before)   # no ingest event published

    def test_pending_listing_and_status(self) -> None:
        pc.queue_outbox("url", "https://example.com/mix")
        rows = pc.outbox_pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "url")
        self.assertEqual(len(pc.status()["outbox_pending"]), 1)
        pc.drain_outbox(self.device)
        self.assertEqual(pc.outbox_pending(), [])


if __name__ == "__main__":
    unittest.main()
