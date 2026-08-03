"""Read-my-documents ingestion: file text -> the shared extraction pipeline.

Covers the four layers independently: text extraction per format (txt/md/pdf/
docx, real fixtures), paragraph chunking, filesystem discovery (filtering / caps
/ junk-skip / root confinement), and the ingest itself (OBSERVED document events
+ EXTRACTED, unreviewed, reversible facts, idempotent re-runs). The extractor's
LLM call is stubbed so the suite is offline and deterministic — we test OUR
wiring, not Claude. A generality test proves the code carries no user-specific
literals and learns whoever's machine it runs on.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.services import documents as D


# --- a valid, extractable minimal PDF (correct xref offsets) ----------------
def _make_pdf(text: str) -> bytes:
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        None,
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    stream = b"BT /F1 24 Tf 72 700 Td (" + text.encode("latin-1") + b") Tj ET"
    objs[3] = b"<</Length %d>>stream\n%s\nendstream" % (len(stream), stream)
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj" % i + body + b"endobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer<</Root 1 0 R/Size %d>>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1, xref_pos)
    return out


def _write_docx(path: Path, *paragraphs: str) -> None:
    import docx
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    d.save(str(path))


def _cfg(**over):
    """A stand-in settings.documents so tests control caps without env import
    timing. Attribute-shaped (SimpleNamespace) — documents.py reads plain attrs."""
    base = dict(enabled=True,
                exts=frozenset({".txt", ".md", ".markdown", ".pdf", ".docx"}),
                max_docs=40, max_bytes=3_000_000, max_chars=40_000,
                chunk_chars=2500, max_depth=4, roots_raw="",
                state_path=over.pop("_state", ""))
    base.update(over)
    return SimpleNamespace(documents=SimpleNamespace(**base))


# --- text extraction --------------------------------------------------------
class ExtractTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_docs_"))

    def test_plain_txt_and_md(self) -> None:
        (self.tmp / "a.txt").write_text("hello world", encoding="utf-8")
        (self.tmp / "b.md").write_text("# Title\n\nbody", encoding="utf-8")
        self.assertEqual(D.extract_text(self.tmp / "a.txt"), "hello world")
        self.assertIn("body", D.extract_text(self.tmp / "b.md"))

    def test_docx(self) -> None:
        fp = self.tmp / "c.docx"
        _write_docx(fp, "Bob Smith owes Acme a report by Friday.", "Second line.")
        got = D.extract_text(fp)
        self.assertIn("Bob Smith owes Acme", got)
        self.assertIn("Second line", got)

    def test_pdf(self) -> None:
        fp = self.tmp / "d.pdf"
        fp.write_bytes(_make_pdf("Invoice for Acme due Friday"))
        self.assertEqual(D.extract_text(fp), "Invoice for Acme due Friday")

    def test_unsupported_extension_returns_empty(self) -> None:
        fp = self.tmp / "e.xyz"
        fp.write_text("data", encoding="utf-8")
        self.assertEqual(D.extract_text(fp), "")

    def test_corrupt_pdf_is_graceful(self) -> None:
        fp = self.tmp / "bad.pdf"
        fp.write_bytes(b"%PDF-1.4 not really a pdf")
        self.assertEqual(D.extract_text(fp), "")   # no raise, just empty

    def test_char_cap(self) -> None:
        (self.tmp / "big.txt").write_text("x" * 5000, encoding="utf-8")
        self.assertEqual(len(D.extract_text(self.tmp / "big.txt", cap=100)), 100)


# --- chunking ---------------------------------------------------------------
class ChunkTests(unittest.TestCase):
    def test_splits_on_paragraphs_within_size(self) -> None:
        text = "\n\n".join([f"Para {i} " + "w" * 40 for i in range(10)])
        chunks = D.chunk_text(text, size=200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 220 for c in chunks))  # ~size, small slack

    def test_hard_slices_a_giant_paragraph(self) -> None:
        chunks = D.chunk_text("z" * 900, size=200)
        self.assertEqual(len(chunks), 5)
        self.assertTrue(all(len(c) <= 200 for c in chunks))

    def test_empty_and_blank(self) -> None:
        self.assertEqual(D.chunk_text(""), [])
        self.assertEqual(D.chunk_text("   \n\n  "), [])


# --- discovery --------------------------------------------------------------
class DiscoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_docs_"))

    def test_matches_exts_and_skips_others(self) -> None:
        (self.tmp / "keep.txt").write_text("a", encoding="utf-8")
        (self.tmp / "skip.exe").write_bytes(b"MZ")
        (self.tmp / "keep.md").write_text("b", encoding="utf-8")
        with mock.patch.object(D, "settings", _cfg()):
            names = {p.name for p in D.discover([self.tmp])}
        self.assertEqual(names, {"keep.txt", "keep.md"})

    def test_skips_junk_dirs(self) -> None:
        junk = self.tmp / "node_modules" / "pkg"
        junk.mkdir(parents=True)
        (junk / "readme.md").write_text("x", encoding="utf-8")
        (self.tmp / "real.md").write_text("x", encoding="utf-8")
        with mock.patch.object(D, "settings", _cfg()):
            names = {p.name for p in D.discover([self.tmp])}
        self.assertEqual(names, {"real.md"})

    def test_respects_size_cap(self) -> None:
        (self.tmp / "small.txt").write_text("ok", encoding="utf-8")
        (self.tmp / "huge.txt").write_text("x" * 5000, encoding="utf-8")
        with mock.patch.object(D, "settings", _cfg(max_bytes=100)):
            names = {p.name for p in D.discover([self.tmp])}
        self.assertEqual(names, {"small.txt"})

    def test_respects_max_docs(self) -> None:
        for i in range(10):
            (self.tmp / f"f{i}.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(D, "settings", _cfg(max_docs=3)):
            self.assertEqual(len(D.discover([self.tmp])), 3)

    def test_depth_cap(self) -> None:
        deep = self.tmp / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "deep.txt").write_text("x", encoding="utf-8")
        (self.tmp / "top.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(D, "settings", _cfg(max_depth=2)):
            names = {p.name for p in D.discover([self.tmp])}
        self.assertIn("top.txt", names)
        self.assertNotIn("deep.txt", names)

    def test_newest_first(self) -> None:
        old = self.tmp / "old.txt"; old.write_text("x", encoding="utf-8")
        new = self.tmp / "new.txt"; new.write_text("x", encoding="utf-8")
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        with mock.patch.object(D, "settings", _cfg()):
            order = [p.name for p in D.discover([self.tmp])]
        self.assertEqual(order[0], "new.txt")

    def test_skips_dev_and_code_files(self) -> None:
        # "Read my documents" must target user docs, not code/build/dep/log files
        # (on a dev machine those flood the graph with 'FastAPI server' etc.).
        for junk in ("README.md", "requirements.txt", "pnpm-install-log.txt",
                     "SKILL.md", "package.json", "CHANGELOG.md"):
            (self.tmp / junk).write_text("x", encoding="utf-8")
        (self.tmp / "my_essay.md").write_text("real user note", encoding="utf-8")
        with mock.patch.object(D, "settings", _cfg()):
            names = {p.name for p in D.discover([self.tmp])}
        self.assertIn("my_essay.md", names)
        for junk in ("README.md", "requirements.txt", "pnpm-install-log.txt",
                     "SKILL.md", "package.json", "CHANGELOG.md"):
            self.assertNotIn(junk, names)

    def test_confined_to_roots(self) -> None:
        inside = self.tmp / "in"; inside.mkdir()
        (inside / "a.txt").write_text("x", encoding="utf-8")
        (self.tmp / "outside.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(D, "settings", _cfg()):
            paths = D.discover([inside])
        self.assertTrue(all(D._within(inside, p) for p in paths))
        self.assertEqual({p.name for p in paths}, {"a.txt"})


# --- ingest -----------------------------------------------------------------
def _fake_extract_factory():
    """A stubbed extractor: turn each chunk into one claim echoing its head, plus
    a 'me'-owned task on chunks that say 'todo'. Lets us assert real content flows
    through without a Claude call — and prove no cross-contamination (generality)."""
    def fake(text: str) -> dict:
        head = text.strip().split("\n")[0][:80]
        out = {"tasks": [], "commitments": [], "claims": [
                   {"text": f"Document says: {head}", "confidence": 0.7,
                    "source_span": head}],
               "entities": [], "relations": []}
        if "todo" in text.lower():
            out["tasks"].append({"text": "Follow up", "owner": "me", "due": "",
                                 "confidence": 0.8, "source_span": head})
        return out
    return fake


class IngestTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.storage import Store
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_docs_"))
        self.docs = self.tmp / "docs"; self.docs.mkdir()
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        self.state = str(self.tmp / "state.json")

    def _ingest(self, **cfg_over):
        from app.services.extractor import extractor
        cfg = _cfg(_state=self.state, **cfg_over)
        with mock.patch.object(D, "settings", cfg), \
             mock.patch.object(extractor, "_extract_text", _fake_extract_factory()):
            return D.ingest(roots=[self.docs], store=self.store)

    def test_creates_observed_document_events(self) -> None:
        (self.docs / "notes.txt").write_text(
            "Roadmap for Q3.\n\ntodo ship the beta.", encoding="utf-8")
        res = self._ingest()
        self.assertTrue(res["ok"])
        self.assertEqual(res["documents"], 1)
        evs = [ev for _, ev in self.store.all_with_ids()
               if ev.source == D.SOURCE]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].modality.value, "document")
        self.assertEqual(evs[0].epistemic, "observed")     # read verbatim off disk
        self.assertIn("notes.txt", evs[0].summary)

    def test_facts_are_extracted_unreviewed_and_anchored(self) -> None:
        (self.docs / "spec.md").write_text(
            "The launch is Monday.\n\ntodo book the venue.", encoding="utf-8")
        self._ingest()
        claims = self.store.list_facts(kind="claim", limit=100)
        tasks = self.store.list_facts(kind="task", limit=100)
        self.assertTrue(claims)
        self.assertTrue(tasks)
        # Unreviewed -> shows up in the Console's approve/edit/dismiss loop.
        self.assertTrue(all(f["review"] is None for f in claims + tasks))
        # Every fact anchors back to the (single) document event for provenance.
        doc_ids = {i for i, ev in self.store.all_with_ids() if ev.source == D.SOURCE}
        self.assertTrue(all(f["source_event_id"] in doc_ids for f in claims + tasks))

    def test_reversible_source_tag(self) -> None:
        (self.docs / "a.txt").write_text("hello", encoding="utf-8")
        self._ingest()
        self.assertTrue(any(ev.source == "documents.scan"
                            for _, ev in self.store.all_with_ids()))

    def test_idempotent_unchanged_file_skipped(self) -> None:
        (self.docs / "a.txt").write_text("content here", encoding="utf-8")
        first = self._ingest()
        self.assertEqual(first["documents"], 1)
        second = self._ingest()
        self.assertEqual(second["documents"], 0)
        self.assertEqual(second["skipped"], 1)

    def test_changed_file_reingests(self) -> None:
        fp = self.docs / "a.txt"
        fp.write_text("original", encoding="utf-8")
        self._ingest()
        fp.write_text("edited and now different length", encoding="utf-8")
        os.utime(fp, (3_000_000, 3_000_000))   # bump mtime so the signature changes
        again = self._ingest()
        self.assertEqual(again["documents"], 1)

    def test_unreadable_file_counted_and_not_retried(self) -> None:
        (self.docs / "broken.pdf").write_bytes(b"%PDF-1.4 garbage")
        first = self._ingest()
        self.assertEqual(first["documents"], 0)
        self.assertEqual(first["unreadable"], 1)
        # Ledgered, so a second run skips it rather than re-attempting.
        second = self._ingest()
        self.assertEqual(second["unreadable"], 0)
        self.assertEqual(second["skipped"], 1)

    def test_document_event_marked_extracted(self) -> None:
        (self.docs / "a.txt").write_text("hi", encoding="utf-8")
        self._ingest()
        # No DOCUMENT event should linger as unextracted work.
        pend = self.store.unextracted_events(limit=100)
        self.assertFalse(any(ev.source == D.SOURCE for _, ev in pend))

    def test_disabled_flag(self) -> None:
        (self.docs / "a.txt").write_text("hi", encoding="utf-8")
        res = self._ingest(enabled=False)
        self.assertFalse(res["ok"])
        self.assertIn("disabled", res["error"])

    def test_dues_are_coerced_like_the_audio_path(self) -> None:
        # Regression (July 28 audit): this path stored dues raw, so ISO-ish
        # variants (space separator, padding) could fail datetime.fromisoformat
        # downstream and the item was silently never overdue. Dues must pass
        # through _coerce_due exactly as the audio extractor's do.
        (self.docs / "plan.txt").write_text(
            "Q3 plan.\n\nsend the deck.", encoding="utf-8")

        def fake(text: str) -> dict:
            head = text.strip().split("\n")[0][:80]
            return {"tasks": [{"text": "Send the deck", "owner": "",
                               "due": "2026-07-31 14:00",
                               "confidence": 0.8, "source_span": head}],
                    "commitments": [{"text": "Deliver the report",
                                     "from_person": "", "to_person": "",
                                     "due": " 2026-08-01 ",
                                     "confidence": 0.8, "source_span": head}],
                    "claims": [], "entities": [], "relations": []}

        from app.services.extractor import extractor
        cfg = _cfg(_state=self.state)
        with mock.patch.object(D, "settings", cfg), \
             mock.patch.object(extractor, "_extract_text", fake):
            D.ingest(roots=[self.docs], store=self.store)

        tasks = self.store.list_facts(kind="task", limit=10)
        comms = self.store.list_facts(kind="commitment", limit=10)
        self.assertEqual(tasks[0]["due"], "2026-07-31T14:00:00")
        self.assertEqual(comms[0]["due"], "2026-08-01")


# --- generality: same code, a different person, zero leakage ----------------
class GeneralityTests(unittest.TestCase):
    """The scanner reads whatever documents are on the machine — no baked-in
    identity. On a different person's machine it learns THEIR content."""

    def setUp(self) -> None:
        from app.storage import Store
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_docs_"))
        self.docs = self.tmp / "docs"; self.docs.mkdir()
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        self.state = str(self.tmp / "state.json")

    def test_learns_a_different_persons_documents(self) -> None:
        from app.services.extractor import extractor
        # "Dana Okonkwo"'s machine — nothing to do with the developer.
        (self.docs / "resume.txt").write_text(
            "Dana Okonkwo — marine biologist at Reef Institute.", encoding="utf-8")
        (self.docs / "grant.md").write_text(
            "Coral restoration grant proposal for the Pacific project.",
            encoding="utf-8")
        cfg = _cfg(_state=self.state)
        with mock.patch.object(D, "settings", cfg), \
             mock.patch.object(extractor, "_extract_text", _fake_extract_factory()):
            res = D.ingest(roots=[self.docs], store=self.store)
        self.assertEqual(res["documents"], 2)
        blob = " ".join(f["text"] for f in self.store.list_facts(limit=100)).lower()
        self.assertIn("dana okonkwo", blob)
        self.assertIn("coral restoration", blob)
        # No developer data leaked in from anywhere.
        for leak in ("justin", "adorante", "alpaca", "dtc", "villanova", "mnemos"):
            self.assertNotIn(leak, blob)

    def test_source_has_no_personal_literals(self) -> None:
        src = Path(D.__file__).read_text(encoding="utf-8").lower()
        for leak in ("justin", "adorante", "jadorant", "villanova", "dt-capital",
                     "alpaca_market", "dtc_agent"):
            self.assertNotIn(leak, src)


if __name__ == "__main__":
    unittest.main()
