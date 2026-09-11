"""CAL surface resolution + the identifiers->keys normalizer.

Two contracts. The normalizer is the binding GRAMMAR over identifiers.py's
mining: it must grade down what the miner over-produces, because prose slashes
reach that miner as repo slugs. Surface resolution is the path that actually
carries a browser-centric stream, where no identifier exists at all.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.services.context import keys as k, surfaces as sf
from app.storage import Store


class NormalizerTests(unittest.TestCase):
    """keys.from_identifiers — one extractor, one stoplist, graded output."""

    def test_prose_slug_is_not_a_repository(self) -> None:
        """A real pitch document reached the miner as VC/PE, ASR/VLM and
        payload_hash/expires_at. Minting repo keys from those asserts
        repositories that do not exist."""
        out = k.from_identifiers([
            {"kind": "repo", "value": "VC/PE", "norm": "PE", "src": "ocr_slug"},
            {"kind": "repo", "value": "ASR/VLM", "norm": "VLM", "src": "ocr_slug"},
        ])
        self.assertEqual(out, [])

    def test_host_anchored_slug_is_authoritative(self) -> None:
        for src in ("ocr", "browser_url"):
            out = k.from_identifiers([{"kind": "repo", "src": src,
                                       "value": "JAdorante/mnemos_v1",
                                       "norm": "mnemos_v1"}])
            self.assertEqual([s.key for s in out],
                             ["repo:github.com/jadorante/mnemos_v1"], src)
            self.assertEqual(out[0].tier, k.STRONG)

    def test_path_uses_value_not_the_root_word(self) -> None:
        """`norm` is the path's ROOT WORD. Feeding it to path() minted a key
        rooted at the reading process's cwd — a binding describing us."""
        out = k.from_identifiers([{"kind": "path", "norm": "console",
                                   "value": "/console/audio-health"}])
        self.assertEqual([s.key for s in out], ["path:/console/audio-health"])

    def test_title_segments_are_not_keys(self) -> None:
        """They are surfaces for entity resolution, a different mechanism."""
        self.assertEqual(k.from_identifiers([
            {"kind": "title_segment", "value": "Boostrun", "norm": "boostrun"},
            {"kind": "email_subject", "value": "Re: pilot", "norm": "re pilot"},
        ]), [])

    def test_dedupes_preserving_order(self) -> None:
        out = k.from_identifiers([
            {"kind": "ticket", "value": "LIN-412", "norm": "LIN-412"},
            {"kind": "ticket", "value": "LIN-0412", "norm": "LIN-0412"},
        ])
        self.assertEqual([s.key for s in out], ["issue:LIN-412"])


class ResourceUrlTests(unittest.TestCase):
    """The registrable domain is the wrong granularity for SaaS work."""

    def test_resource_key_survives_a_stoplisted_domain(self) -> None:
        """`google.com` names no project; a specific document under it does.
        The stoplist was discarding the identifier sitting in the path."""
        sk = k.url("drive.google.com/document", resource="a1b2c3d4e5f60718")
        self.assertEqual(sk.key,
                         "url:drive.google.com/document#a1b2c3d4e5f60718")
        self.assertEqual((sk.key_class, sk.tier), (k.IDENTITY, k.STRONG))

    def test_without_a_resource_it_degrades_to_the_host(self) -> None:
        sk = k.url("drive.google.com/document")
        self.assertEqual(sk.key, "domain:google.com")
        self.assertEqual(sk.tier, k.SUPPORTING, "and the stoplist applies")

    def test_private_suffix_keeps_the_org(self) -> None:
        """The repo's PSL carries the ICANN section only, which collapses every
        Jira customer onto atlassian.net."""
        self.assertEqual(k.domain("acme.atlassian.net").key,
                         "domain:acme.atlassian.net")

    def test_digest_is_opaque(self) -> None:
        sk = k.url("claude.ai/chat", resource="ffeeddccbbaa9988")
        self.assertNotIn("uuid", sk.key)
        self.assertTrue(sk.key.endswith("#ffeeddccbbaa9988"))


class SurfaceResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_surf_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "a")
        now = time.time()
        self.org = self.store.resolve_entity("Boostrun", "org", ts=now)
        self.tool = self.store.resolve_entity("Claude", "tool", ts=now)
        self.person = self.store.resolve_person("Andy Karos", ts=now)
        self.me = self.store.resolve_person("Justin Adorante", ts=now)
        self.idx = sf.SurfaceIndex(self.store)

    def test_org_inside_a_search_query_resolves(self) -> None:
        """Whole-segment matching missed this: the segment is the full query,
        the entity is a substring of it."""
        hits = self.idx.from_title(
            "Andrew Andy Karos Boostrun - Google Search - Chromium")
        self.assertEqual({(h.node_type, h.name) for h in hits},
                         {("entity", "Boostrun"), ("person", "Andy Karos")})

    def test_a_person_resolves_at_all(self) -> None:
        """entity_alias.resolve never consults the people table, so the single
        most frequent segment in the measured corpus matched nothing."""
        hits = self.idx.from_title(
            "Mail - Justin Adorante - Outlook — Mozilla Firefox")
        self.assertEqual([(h.node_type, h.name, h.method) for h in hits],
                         [("person", "Justin Adorante", "exact")])

    def test_exact_outranks_containment(self) -> None:
        exact = self.idx.resolve("Claude")[0]
        contains = self.idx.resolve("Claude Status")[0]
        self.assertEqual(exact.method, "exact")
        self.assertEqual(contains.method, "contains")
        self.assertGreater(exact.strength, contains.strength)

    def test_chrome_never_resolves(self) -> None:
        """Resolving these would attach an entity named Home to a third of the
        stream. Every one appeared in the measured corpus."""
        for seg in ("Login Page", "Sign in", "Google Search", "Recent",
                    "Sent Items", "Mail", "Calendar", "about:blank", "Home"):
            self.assertTrue(sf.is_chrome(seg), seg)
            self.assertEqual(self.idx.resolve(seg), [], seg)

    def test_generic_names_never_match_by_containment(self) -> None:
        """This graph really does hold entities called `unknown`, `company` and
        `CEO`; graph._STOP_NAMES covers pronouns only."""
        for junk in ("unknown", "company", "CEO", "MVP"):
            self.store.resolve_entity(junk, "idea", ts=time.time())
        idx = sf.SurfaceIndex(self.store)
        hits = idx.resolve("Quarterly company planning for the CEO")
        self.assertEqual(hits, [])

    def test_exact_match_on_a_generic_name_still_works(self) -> None:
        """Containment is the dangerous direction, not equality."""
        eid = self.store.resolve_entity("MVP", "idea", ts=time.time())
        idx = sf.SurfaceIndex(self.store)
        hits = idx.resolve("MVP")
        self.assertEqual([(h.node_type, h.node_id) for h in hits],
                         [("entity", eid)])

    def test_never_mints(self) -> None:
        """A surface string can never create a node — unknowns stay unknown,
        which is what new-entity detection is built from."""
        before = len(self.store.recent_entities(999))
        self.assertEqual(self.idx.resolve("Wholly Unheard Of Thing"), [])
        self.assertEqual(len(self.store.recent_entities(999)), before)

    def test_hidden_person_is_not_a_surface(self) -> None:
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE people SET hide_from_people=1 WHERE id=?", (self.person,))
            self.store._conn.commit()
        idx = sf.SurfaceIndex(self.store)
        hits = idx.from_title("Andrew Andy Karos Boostrun - Google Search - X")
        self.assertNotIn("person", {h.node_type for h in hits})


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
