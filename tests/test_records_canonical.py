"""Records layer — canonical hashing (property tests) and claim kind schemas.

Phase 1 acceptance: canonical_hash is stable across key order, whitespace and
float formatting. Property tests use a seeded stdlib generator (no new test
dependency): every generated value is re-spelled many ways and must hash the
same, and anything that changes the value must change the hash.
"""
from __future__ import annotations

import json
import random
import unicodedata
import unittest

from app.services.records import claim_schemas
from app.services.records.canonical import (CanonicalError, canonical_hash,
                                            canonical_json, quote_hash)

SEED = 20261002
N_CASES = 400


def _rand_value(rng: random.Random, depth: int = 0):
    kinds = ["int", "float", "str", "bool", "null"]
    if depth < 3:
        kinds += ["list", "dict", "dict"]
    k = rng.choice(kinds)
    if k == "int":
        return rng.randint(-10 ** 6, 10 ** 6)
    if k == "float":
        return rng.choice([rng.uniform(-1e6, 1e6), float(rng.randint(-50, 50)),
                           rng.random() / 1000.0, 0.1, 1e-7, 2.5e10])
    if k == "str":
        alphabet = "abc xyz é ü 日本 \t\n\"\\/ é"
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
    if k == "bool":
        return rng.random() < 0.5
    if k == "null":
        return None
    if k == "list":
        return [_rand_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {f"k{rng.randint(0, 30)}_{i}": _rand_value(rng, depth + 1)
            for i in range(rng.randint(0, 5))}


def _respell(value, rng: random.Random) -> str:
    """Same value, different JSON text: key order, whitespace, float spelling."""
    def walk(v):
        if isinstance(v, dict):
            items = list(v.items())
            rng.shuffle(items)
            return "{" + (" , " if rng.random() < .5 else ",").join(
                json.dumps(k) + (" :  " if rng.random() < .5 else ":") + walk(x)
                for k, x in items) + "}"
        if isinstance(v, list):
            return "[ " + " ,\n".join(walk(x) for x in v) + "\t]"
        if isinstance(v, bool) or v is None:
            return json.dumps(v)
        if isinstance(v, float):
            if v.is_integer() and abs(v) < 2 ** 53:
                return rng.choice([f"{int(v)}", f"{int(v)}.0", f"{int(v)}.000",
                                   f"{v:e}" if v else "0e0"])
            return rng.choice([repr(v), f"{v:.17g}", f"{v:.17e}"])
        if isinstance(v, int):
            return rng.choice([str(v), f"{v}.0", f"{v}.00"])
        return json.dumps(v, ensure_ascii=rng.random() < .5)
    return walk(value)


class CanonicalHashProperties(unittest.TestCase):
    def test_stable_across_key_order_whitespace_and_float_spelling(self):
        rng = random.Random(SEED)
        for _ in range(N_CASES):
            value = _rand_value(rng)
            want = canonical_hash(value)
            for _ in range(5):
                text = _respell(value, rng)
                self.assertEqual(canonical_hash(json.loads(text)), want, text)

    def test_canonical_form_is_a_fixed_point(self):
        rng = random.Random(SEED + 1)
        for _ in range(N_CASES):
            once = canonical_json(_rand_value(rng))
            self.assertEqual(canonical_json(json.loads(once)), once)

    def test_changing_a_leaf_changes_the_hash(self):
        rng = random.Random(SEED + 2)
        seen = 0
        for _ in range(N_CASES):
            value = {"a": _rand_value(rng), "n": rng.randint(0, 10 ** 9)}
            other = dict(value, n=value["n"] + 1)
            self.assertNotEqual(canonical_hash(value), canonical_hash(other))
            seen += 1
        self.assertEqual(seen, N_CASES)

    def test_int_and_integral_float_agree_but_bool_does_not(self):
        self.assertEqual(canonical_hash({"x": 1}), canonical_hash({"x": 1.0}))
        self.assertEqual(canonical_hash(json.loads('{"x": 1.00}')),
                         canonical_hash(json.loads('{"x": 1e0}')))
        self.assertNotEqual(canonical_hash({"x": True}), canonical_hash({"x": 1}))
        self.assertNotEqual(canonical_hash({"x": 0.1}), canonical_hash({"x": 0.10000001}))

    def test_unicode_normalization(self):
        composed = unicodedata.normalize("NFC", "café")
        decomposed = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(composed, decomposed)
        self.assertEqual(canonical_hash({"t": composed}),
                         canonical_hash({"t": decomposed}))

    def test_nan_and_infinity_have_no_canonical_form(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(CanonicalError):
                canonical_hash({"x": bad})

    def test_non_json_types_refused(self):
        with self.assertRaises(CanonicalError):
            canonical_hash({"x": {1, 2}})
        with self.assertRaises(CanonicalError):
            canonical_hash({1: "int key"})

    def test_quote_hash_ignores_whitespace_runs(self):
        self.assertEqual(quote_hash("we  will ship\nFriday"),
                         quote_hash(" we will ship Friday "))
        self.assertNotEqual(quote_hash("we will ship Friday"),
                            quote_hash("we will ship Monday"))


class ClaimSchemaTests(unittest.TestCase):
    def test_every_kind_loads(self):
        for kind in claim_schemas.KINDS:
            self.assertTrue(claim_schemas.schema_version(kind).startswith(kind))

    def test_commitment(self):
        ok = {"text": "Send the deck", "owner": "me", "counterparty": "Sam",
              "due": "2026-10-09"}
        self.assertEqual(claim_schemas.validate("commitment", ok), [])
        self.assertTrue(claim_schemas.validate("commitment", {"owner": "me"}))
        self.assertTrue(claim_schemas.validate("commitment",
                                               dict(ok, due="next week")))
        self.assertTrue(claim_schemas.validate("commitment", dict(ok, extra=1)))

    def test_field_update_must_name_the_field(self):
        self.assertEqual(claim_schemas.validate(
            "field_update", {"field": "dealstage", "value": "closedwon"}), [])
        self.assertTrue(claim_schemas.validate("field_update", {"value": 3}))
        self.assertTrue(claim_schemas.validate(
            "field_update", {"field": "deal stage!", "value": 3}))
        self.assertTrue(claim_schemas.validate(
            "field_update", {"field": "amount", "value": [1, 2]}))

    def test_unknown_kind(self):
        with self.assertRaises(claim_schemas.SchemaError):
            claim_schemas.validate("gossip", {})


if __name__ == "__main__":
    unittest.main()
