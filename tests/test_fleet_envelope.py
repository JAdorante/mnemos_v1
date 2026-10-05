"""Fleet envelope: the one shape the fleet, Sparrow, and the relay validate.

The envelope is domain-neutral; kinds declare body shape and forbidden
fields. Proves the core rejects unknown fields, unknown kinds, forbidden
fields, expired and over-hop signals, that kind definitions are checked, and
that the HMAC round-trips and fails on any byte change.
"""
from __future__ import annotations

import json
import time
import unittest

from app.services.fleet import envelope as env
from tests.fleet_support import EXAMPLE_KINDS, full_signal, kinds_registry

K = kinds_registry()


def v(sig, **kw):
    return env.validate(sig, kinds=K, **kw)


class CoreTests(unittest.TestCase):
    def test_a_well_formed_signal_validates(self) -> None:
        sig = v(full_signal())
        self.assertEqual(sig.kind, "status_update")
        self.assertEqual(sig.subject, "Atlas migration")
        self.assertEqual(sig.body, {"status": "at_risk"})
        self.assertEqual(sig.sources[0].license, "internal_ok")

    def test_note_is_built_in_and_needs_no_body_or_subject(self) -> None:
        sig = env.validate(full_signal(kind="note", subject=None, body={},
                                       confidence=None))
        self.assertEqual(sig.kind, "note")
        self.assertIsNone(sig.confidence)

    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(env.SignalError) as cm:
            v(full_signal(mood="spicy"))
        self.assertEqual(cm.exception.code, "unknown_field")

    def test_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(env.SignalError) as cm:
            v(full_signal(kind="gossip"))
        self.assertEqual(cm.exception.code, "unknown_kind")
        # Without the registry only built-ins exist.
        with self.assertRaises(env.SignalError) as cm:
            env.validate(full_signal())
        self.assertEqual(cm.exception.code, "unknown_kind")

    def test_expired_signal_is_rejected(self) -> None:
        now = time.time()
        with self.assertRaises(env.SignalError) as cm:
            v(full_signal(ts=now - 100, expires_at=now - 1))
        self.assertEqual(cm.exception.code, "expired")

    def test_over_hop_signal_is_rejected(self) -> None:
        with self.assertRaises(env.SignalError) as cm:
            v(full_signal(hops=3), max_hops=2)
        self.assertEqual(cm.exception.code, "too_many_hops")
        v(full_signal(hops=2), max_hops=2)

    def test_outbound_requires_internal_ok_licences(self) -> None:
        vendor = full_signal(sources=[{"name": "vendor feed",
                                       "license": "vendor_no_redistribution"}])
        v(vendor)  # fine to hold locally
        with self.assertRaises(env.SignalError) as cm:
            v(vendor, outbound=True)
        self.assertEqual(cm.exception.code, "license_not_shareable")

    def test_every_source_needs_a_licence(self) -> None:
        with self.assertRaises(env.SignalError) as cm:
            v(full_signal(sources=[{"name": "x"}]))
        self.assertEqual(cm.exception.code, "missing_field")

    def test_core_ranges(self) -> None:
        for over in ({"confidence": 1.5}, {"confidence": True},
                     {"summary": "   "}, {"hops": -1}, {"topic": "Bad Topic"},
                     {"producer": "someone"}, {"subject": "x" * 201}):
            with self.subTest(over=over):
                with self.assertRaises(env.SignalError):
                    v(full_signal(**over))

    def test_ttl_and_clock_skew_bounds(self) -> None:
        now = time.time()
        with self.assertRaises(env.SignalError):
            v(full_signal(ts=now, expires_at=now + env.MAX_TTL_S + 5))
        with self.assertRaises(env.SignalError):
            v(full_signal(ts=now + 3600, expires_at=now + 7200))

    def test_oversize_signal_is_rejected(self) -> None:
        many = [{"name": "n" * 200, "license": "internal_ok",
                 "url": "u" * 500} for _ in range(20)]
        with self.assertRaises(env.SignalError) as cm:
            v(full_signal(sources=many, summary="t" * 4000))
        self.assertEqual(cm.exception.code, "too_large")

    def test_agent_input_cannot_stamp_identity(self) -> None:
        for field in ("producer", "origin_id", "hops", "sig", "ts"):
            with self.subTest(field=field):
                with self.assertRaises(env.SignalError) as cm:
                    env.check_agent_input({"topic": "t", field: "x"}, K)
                self.assertEqual(cm.exception.code, "stamped_field")


class KindBodyTests(unittest.TestCase):
    def test_body_follows_the_kind(self) -> None:
        for body, code in (({}, "missing_field"),
                           ({"status": "fine"}, "bad_value"),
                           ({"status": "done", "progress": 2}, "bad_value"),
                           ({"status": "done", "progress": "half"}, "bad_type"),
                           ({"status": "done", "blockers": "one"}, "bad_type"),
                           ({"status": "done", "colour": "red"},
                            "unknown_field")):
            with self.subTest(body=body):
                with self.assertRaises(env.SignalError) as cm:
                    v(full_signal(body=body))
                self.assertEqual(cm.exception.code, code)
        v(full_signal(body={"status": "done", "progress": 1,
                            "blockers": ["none"]}))

    def test_subject_rules(self) -> None:
        with self.assertRaises(env.SignalError) as cm:
            v(full_signal(subject=None))
        self.assertEqual(cm.exception.code, "missing_field")
        mv = full_signal(kind="market_view", subject="not a ticker!",
                         body={"direction": "bearish", "horizon": "weeks"})
        with self.assertRaises(env.SignalError):
            v(mv)
        v(dict(mv, subject="TLT"))

    def test_forbidden_fields_are_refused_anywhere_with_a_named_reason(self) -> None:
        cases = (
            ("market_view", {"quantity": 500}, "top level"),
            ("market_view", {"body": {"direction": "bearish",
                                      "horizon": "weeks", "price": 101}},
             "body"),
            ("lead", {"sources": [{"name": "crm", "license": "internal_ok",
                                   "email": "a@b.c"}]}, "source"),
        )
        bodies = {"market_view": {"direction": "bearish", "horizon": "weeks"},
                  "lead": {"stage": "warm"}}
        for kind, over, where in cases:
            with self.subTest(kind=kind, where=where):
                sig = full_signal(kind=kind, subject="TLT",
                                  body=bodies[kind])
                sig.update(over)
                with self.assertRaises(env.SignalError) as cm:
                    v(sig)
                self.assertEqual(cm.exception.code, "forbidden_field")
                with self.assertRaises(env.SignalError) as cm:
                    env.check_agent_input(
                        {k: x for k, x in sig.items()
                         if k in env.AGENT_FIELDS or k in over}, K)
                self.assertEqual(cm.exception.code, "forbidden_field")

    def test_forbidden_is_per_kind(self) -> None:
        """`price` is only forbidden where a kind says so."""
        v(full_signal(kind="finding", subject=None,
                      body={"severity": "low", "tags": ["price"]}))


class KindRegistryTests(unittest.TestCase):
    def test_example_registry_loads_with_builtins(self) -> None:
        self.assertEqual(set(K), {"note", "status_update", "finding", "lead",
                                  "market_view"})

    def test_bad_definitions_raise(self) -> None:
        bad = (
            {"kinds": {"Bad Name": {}}},
            {"kinds": {"k": {"fields": {"x": {"type": "object"}}}}},
            {"kinds": {"k": {"fields": {}, "required": ["x"]}}},
            {"kinds": {"k": {"subject": "maybe"}}},
            {"kinds": {"k": {"fields": {"x": {"type": "string"}},
                             "forbidden": ["x"]}}},
            {"kinds": {"k": {"surprise": 1}}},
            {"kinds": {"note": {}}},
            {"kinds": []},
            {"kinds": {"k": {"fields": {"x": {"type": "array",
                                              "items": {"type": "array"}}}}}},
        )
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    env.load_kinds(raw)

    def test_kind_schema_is_published(self) -> None:
        sch = env.kind_schema("market_view", K["market_view"])
        self.assertFalse(sch["additionalProperties"])
        self.assertIn("quantity", sch["x-forbidden"])
        self.assertEqual(sch["required"], ["direction", "horizon"])

    def test_envelope_schema_matches_fields(self) -> None:
        self.assertEqual(set(env.SIGNAL_SCHEMA["properties"]), set(env.FIELDS))
        self.assertFalse(env.SIGNAL_SCHEMA["additionalProperties"])
        self.assertIn("kinds", EXAMPLE_KINDS)


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
        for field, value in (("summary", "Cutover slipped a week!"),
                             ("confidence", 0.71), ("hops", 1),
                             ("subject", "Atlas migratio"),
                             ("body", {"status": "blocked"}),
                             ("sources", [{"name": "standup notes",
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
        c = env.canonical(full_signal(sig="deadbeef"))
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


class BlocklistTests(unittest.TestCase):
    def test_exact_and_pattern_matches_are_case_and_space_insensitive(self) -> None:
        b = env.load_blocklist({"subjects": ["Project  Falcon"],
                                "patterns": ["acme*"]})
        for s in ("project falcon", "PROJECT FALCON", " Project Falcon "):
            self.assertTrue(b.blocks(s), s)
        self.assertTrue(b.blocks("ACME Corp"))
        self.assertFalse(b.blocks("Falcon"))
        self.assertFalse(b.blocks(None))

    def test_malformed_list_raises(self) -> None:
        for bad in ({}, {"subjects": "x"}, [], None, {"patterns": 3}):
            with self.assertRaises(ValueError):
                env.load_blocklist(bad)


if __name__ == "__main__":
    unittest.main()
