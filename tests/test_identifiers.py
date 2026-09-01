"""WS2 — identifier extraction from OCR text: each regex family's positives,
adversarial near-misses, normalization idempotence, and cap enforcement."""
from __future__ import annotations

import unittest

from app.perception import identifiers as idn


def _kinds(items):
    return {(i["kind"], i["norm"]) for i in items}


class RepoTests(unittest.TestCase):
    def test_repo_slug_positive(self):
        out = idn.extract_identifiers("pushed to JAdorante/mnemos_v1 today")
        self.assertIn(("repo", "mnemos_v1"), _kinds(out))
        row = next(i for i in out if i["kind"] == "repo")
        self.assertEqual(row["value"], "JAdorante/mnemos_v1")

    def test_prose_slash_negative(self):
        for text in ("input/output streams", "and/or the plan",
                     "either/or choices", "the on/off switch"):
            out = idn.extract_identifiers(text)
            self.assertFalse(any(i["kind"] == "repo" for i in out), text)

    def test_date_negative(self):
        out = idn.extract_identifiers("meeting on 12/25 at noon")
        self.assertFalse(any(i["kind"] == "repo" for i in out))

    def test_mid_path_segments_not_repos(self):
        out = idn.extract_identifiers("see app/services/context_anchor.py")
        self.assertFalse(any(i["kind"] == "repo" for i in out))


class UrlTests(unittest.TestCase):
    def test_url_with_scheme(self):
        out = idn.extract_identifiers(
            "docs at https://docs.python.org/3/library/re.html?x=1")
        row = next(i for i in out if i["kind"] == "url")
        self.assertEqual(row["norm"], "docs.python.org/3")
        self.assertNotIn("?", row["value"])

    def test_bare_github_path_emits_repo_too(self):
        out = idn.extract_identifiers("github.com/JAdorante/mnemos_v1")
        self.assertIn(("repo", "mnemos_v1"), _kinds(out))
        self.assertIn(("url", "github.com/JAdorante"), _kinds(out))

    def test_bare_domain_without_path_negative(self):
        out = idn.extract_identifiers("ask on example.com maybe")
        self.assertFalse(any(i["kind"] == "url" for i in out))


class PathTests(unittest.TestCase):
    def test_windows_path_root(self):
        out = idn.extract_identifiers(
            r"editing C:\Users\Dell AI User\Downloads\nexus_v1\app\storage.py")
        row = next(i for i in out if i["kind"] == "path")
        self.assertEqual(row["norm"], "nexus_v1")

    def test_posix_path_root(self):
        out = idn.extract_identifiers("/home/justin/code/mnemos/app/x.py")
        row = next(i for i in out if i["kind"] == "path")
        self.assertEqual(row["norm"], "mnemos")

    def test_prose_negative(self):
        out = idn.extract_identifiers("the cat sat on the mat")
        self.assertEqual(out, [])


class TicketTests(unittest.TestCase):
    def test_ticket_positive(self):
        out = idn.extract_identifiers("fixed in VPULSE-142 yesterday")
        self.assertIn(("ticket", "VPULSE-142"), _kinds(out))

    def test_acronym_number_negative(self):
        for text in ("COVID-19 response", "UTF-8 text", "ISO-9001 audit",
                     "SHA-256 hash", "RFC-2616 says"):
            out = idn.extract_identifiers(text)
            self.assertFalse(any(i["kind"] == "ticket" for i in out), text)


class TitleSegmentTests(unittest.TestCase):
    def test_window_title_project_segment(self):
        out = idn.extract_identifiers(
            "", window="storage.py - nexus_v1 - Cursor")
        self.assertIn(("title_segment", "nexus_v1"), _kinds(out))
        # The filename and the app name never become identifiers.
        norms = {i["norm"] for i in out if i["kind"] == "title_segment"}
        self.assertNotIn("storage.py", norms)
        self.assertNotIn("Cursor", norms)

    def test_prose_title_negative(self):
        out = idn.extract_identifiers("", window="Quarterly plan - Google Chrome")
        self.assertFalse(any(i["kind"] == "title_segment" for i in out))


class MailSubjectTests(unittest.TestCase):
    def test_subject_only_in_mail_windows(self):
        text = "Subject: Pricing follow-up for VenturePulse\nbody text"
        out = idn.extract_identifiers(text, window="Inbox - Outlook")
        row = next(i for i in out if i["kind"] == "email_subject")
        self.assertEqual(row["norm"], "pricing follow-up for venturepulse")
        self.assertEqual(row["privacy"], "personal")
        # Same text in an IDE window: no subject identifier.
        out2 = idn.extract_identifiers(text, window="notes.md - Cursor")
        self.assertFalse(any(i["kind"] == "email_subject" for i in out2))


class InvariantTests(unittest.TestCase):
    def test_normalization_idempotent(self):
        text = ("github.com/JAdorante/mnemos_v1 VPULSE-9 "
                r"C:\Users\x\repos\capital-connect\main.py")
        for i in idn.extract_identifiers(text):
            self.assertEqual(idn.normalize_identifier(i).strip(),
                             idn.normalize_identifier(i))

    def test_cap_enforced(self):
        text = " ".join(f"PROJ-{n}" for n in range(1, 100))
        out = idn.extract_identifiers(text)
        self.assertLessEqual(len(out), 24)

    def test_dedupe_within_frame(self):
        out = idn.extract_identifiers("VPULSE-1 VPULSE-1 VPULSE-1")
        self.assertEqual(len([i for i in out if i["kind"] == "ticket"]), 1)

    def test_entity_candidate_names(self):
        out = idn.extract_identifiers(
            "github.com/JAdorante/mnemos_v1 VPULSE-9",
            window="a - nexus_v1 - Cursor")
        names = idn.entity_candidate_names(out)
        self.assertIn("mnemos_v1", names)
        self.assertIn("nexus_v1", names)
        self.assertFalse(any("vpulse" in n.lower() for n in names))


if __name__ == "__main__":
    unittest.main()
