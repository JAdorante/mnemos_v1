"""Fleet envelope: the one shape the fleet, Sparrow, and the relay validate.

Proves the schema rejects unknown fields, order-like payloads, expired and
over-hop signals, and that the HMAC round-trips and fails on any byte change.
"""
from __future__ import annotations

import json
import time
import unittest

from app.services.fleet import envelope as env
from tests.fleet_support import full_signal


class SchemaTests(unittest.TestCase):
    def test_a_well_formed_signal_validates(self) -> None:
        sig = env.validate(full_signal())
        self.assertEqual(sig.instrument, "TLT")
        self.assertEqual(sig.sources[0].license, "internal_ok")

    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(env.SignalError) as cm:
            env.validate(full_signal(mood="spicy"))
        self.assertEqual(cm.exception.code, "unknown_field")

    def test_order_like_fields_are_malformed_not_unwise(self) -> None:
        for field in ("quantity", "price", "order_id", "position", "pnl",
                      "side", "notional"):
            with self.subTest(field=field):
                with self.assertRaises(env.SignalError) as cm:
                    env.validate(full_signal(**{field: 100}))
                self.assertEqual(cm.exception.code, "order_like_field")

    def test_order_like_field_inside_a_source_is_rejected(self) -> None:
        bad = full_signal(sources=[{"name": "x", "license": "internal_ok",
                                    "position": 1000}])
        with self.assertRaises(env.SignalError) as cm:
            env.validate(bad)
        self.assertEqual(cm.exception.code, "order_like_field")

    def test_schema_has_no_field_for_holdings_or_execution(self) -> None:
        props = set(env.SIGNAL_SCHEMA["properties"])
        self.assertFalse(props & env.ORDER_LIKE)
        self.assertFalse(env.SIGNAL_SCHEMA["additionalProperties"])
        self.assertEqual(props, set(env.FIELDS))

    def test_expired_signal_is_rejected(self) -> None:
        now = time.time()
        with self.assertRaises(env.SignalError) as cm:
            env.validate(full_signal(ts=now - 100, expires_at=now - 1))
        self.assertEqual(cm.exception.code, "expired")

    def test_over_hop_signal_is_rejected(self) -> None:
        with self.assertRaises(env.SignalError) as cm:
            env.validate(full_signal(hops=3), max_hops=2)
        self.assertEqual(cm.exception.code, "too_many_hops")
        env.validate(full_signal(hops=2), max_hops=2)

    def test_outbound_requires_internal_ok_licences(self) -> None:
        vendor = full_signal(sources=[{"name": "vendor feed",
                                       "license": "vendor_no_redistribution"}])
        env.validate(vendor)  # fine to hold locally
        with self.assertRaises(env.SignalError) as cm:
            env.validate(vendor, outbound=True)
        self.assertEqual(cm.exception.code, "license_not_shareable")

    def test_every_source_needs_a_licence(self) -> None:
        with self.assertRaises(env.SignalError) as cm:
            env.validate(full_signal(sources=[{"name": "x"}]))
        self.assertEqual(cm.exception.code, "missing_field")

    def test_bad_enums_and_ranges(self) -> None:
        for over in ({"direction": "buy"}, {"horizon": "forever"},
                     {"confidence": 1.5}, {"confidence": True},
                     {"thesis": "   "}, {"hops": -1}, {"topic": "Bad Topic"},
                     {"producer": "someone"}):
            with self.subTest(over=over):
                with self.assertRaises(env.SignalError):
                    env.validate(full_signal(**over))

    def test_ttl_and_clock_skew_bounds(self) -> None:
        now = time.time()
        with self.assertRaises(env.SignalError):
            env.validate(full_signal(ts=now, expires_at=now + env.MAX_TTL_S + 5))
        with self.assertRaises(env.SignalError):
            env.validate(full_signal(ts=now + 3600, expires_at=now + 7200))

    def test_oversize_signal_is_rejected(self) -> None:
        many = [{"name": "n" * 200, "license": "internal_ok",
                 "url": "u" * 500} for _ in range(20)]
        with self.assertRaises(env.SignalError) as cm:
            env.validate(full_signal(sources=many, thesis="t" * 4000))
        self.assertEqual(cm.exception.code, "too_large")

    def test_agent_input_cannot_stamp_identity(self) -> None:
        for field in ("producer", "origin_id", "hops", "sig", "ts"):
            with self.subTest(field=field):
                with self.assertRaises(env.SignalError) as cm:
                    env.check_agent_input({"topic": "t", field: "x"})
                self.assertEqual(cm.exception.code, "stamped_field")
        with self.assertRaises(env.SignalError) as cm:
            env.check_agent_input({"topic": "t", "quantity": 5})
        self.assertEqual(cm.exception.code, "order_like_field")


class HmacTests(unittest.TestCase):
    KEY = env.link_key("a-node-token-that-is-long-enough")

    def test_round_trip(self) -> None:
        signed = env.sign(full_signal(), self.KEY)
        self.assertTrue(signed["sig"])
        self.assertTrue(env.verify(signed, self.KEY))

    def test_sign_does_not_mutate_input(self) -> None:
        raw = full_signal()
        env.sign(raw, self.KEY)
        self.assertEqual(raw["sig"], "")

    def test_any_byte_change_fails(self) -> None:
        signed = env.sign(full_signal(), self.KEY)
        for field, value in (("thesis", "Term premium is rebuilding!"),
                             ("confidence", 0.71), ("hops", 1),
                             ("instrument", "TLU"),
                             ("sources", [{"name": "desk notes",
                                           "license": "internal_okk"}])):
            with self.subTest(field=field):
                tampered = dict(signed, **{field: value})
                self.assertFalse(env.verify(tampered, self.KEY))

    def test_wrong_key_and_missing_sig_fail(self) -> None:
        signed = env.sign(full_signal(), self.KEY)
        self.assertFalse(env.verify(signed, env.link_key("other-token")))
        self.assertFalse(env.verify(dict(signed, sig=""), self.KEY))
        self.assertFalse(env.verify(signed, ""))

    def test_canonical_is_sorted_compact_and_excludes_sig(self) -> None:
        raw = full_signal(sig="deadbeef")
        c = env.canonical(raw)
        self.assertNotIn(b'"sig"', c)
        self.assertNotIn(b": ", c)
        self.assertNotIn(b", ", c)
        keys = list(json.loads(c))
        self.assertEqual(keys, sorted(keys))

    def test_link_key_matches_the_coordinators_token_hash(self) -> None:
        """The relay stores hash_token(token); the HMAC key is the same."""
        from org_coordinator.auth import hash_token
        tok = "0123456789abcdef0123456789abcdef"
        self.assertEqual(env.link_key(tok), hash_token(tok))


class RestrictedListTests(unittest.TestCase):
    def test_venue_and_case_variants_collapse(self) -> None:
        r = env.load_restricted({"instruments": ["xyz"]})
        for s in ("XYZ", "xyz", "XYZ.N", "NYSE:XYZ"):
            self.assertTrue(env.is_restricted(s, r), s)
        self.assertFalse(env.is_restricted("XYZW", r))

    def test_malformed_list_raises(self) -> None:
        for bad in ({}, {"instruments": "XYZ"}, [], None):
            with self.assertRaises(ValueError):
                env.load_restricted(bad)


if __name__ == "__main__":
    unittest.main()
