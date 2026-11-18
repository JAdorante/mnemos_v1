"""Entity model, stage G4: an email domain names its org by the registry's
structure, for any country.

`people_pipeline.org_from_email_domain` names the org after the label just
left of the public suffix (the vendored Public Suffix List), so a registry
level never names it: no "Sch" for x@school.lea.sch.uk, no "Ne" for
x@district.k12.ne.us, no "Edu" for x@school.edu.sg. Without the list, a
structural fallback gives the same answers. `is_academic_domain` reads the
registry's academic labels (.edu, .edu.<cc>, .ac.<cc>, .sch.<cc>, .k12.*), so
such an address is `member_of`, not `works_at`. Every caller is checked: the
email-network ingest, the exhaust ingest and the email-domain prior.

Every school, district and employer here is invented.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.perception import psl
from app.services import free_mail
from app.services import people_pipeline as pp
from app.storage import Store

NOW = 1_700_000_000.0

# address -> (org, academic)
CASES = {
    "jo@northfield.edu": ("Northfield", True),
    "jo@grad.northfield.edu": ("Northfield", True),
    "ana@moe-north.edu.sg": ("Moe North", True),
    "ana@cs.northfield.ac.uk": ("Northfield", True),
    "ana@kestrel.ac.jp": ("Kestrel", True),
    "ana@stmarys.lancs.sch.uk": ("Stmarys", True),
    "ana@hillview.sch.ae": ("Hillview", True),
    "ana@lincoln.k12.ne.us": ("Lincoln", True),
    "ana@jesup.k12.ia.us": ("Jesup", True),
    "ana@school.nsw.edu.au": ("School", True),
    "ana@tsinghuo.edu.cn": ("Tsinghuo", True),
    "ana@kestrel.ac": ("Kestrel", True),        # the .ac TLD, as before
    # Registry levels with no registrant name no org.
    "x@co.uk": (None, False),
    "x@k12.ne.us": (None, True),
    "x@stmary.sch.uk": (None, True),
    # Employers and agencies keep their name and are not schools.
    "ana@clearbrook-dental.com": ("Clearbrook Dental", False),
    "ana@mail.clearbrook-dental.com": ("Clearbrook Dental", False),
    "ana@quarrystone.co.uk": ("Quarrystone", False),
    "ana@quarrystone.com.au": ("Quarrystone", False),
    "ana@quarrystone.co.jp": ("Quarrystone", False),
    "ana@tax.gov.uk": ("Tax", False),
    "ana@ward.portland.or.us": ("Portland", False),
    "ana@edu.quarrystone.com": ("Quarrystone", False),
    "ana@ac.quarrystone.com": ("Quarrystone", False),
}


class EmailDomainTests(unittest.TestCase):
    def test_every_case(self):
        for addr, (org, academic) in CASES.items():
            self.assertEqual(pp.org_from_email_domain(addr), org, addr)
            self.assertEqual(pp.is_academic_domain(addr), academic, addr)
            # The bare domain works for the academic check too.
            self.assertEqual(pp.is_academic_domain(addr.split("@")[1]),
                             academic, addr)

    def test_free_mail_and_examples_name_no_org(self):
        for addr in ("x@gmail.com", "x@yandex.ru", "x@example.com",
                     "x@team.example.com", "nobody", ""):
            self.assertIsNone(pp.org_from_email_domain(addr), addr)

    def test_without_the_suffix_list_the_shape_gives_the_same_answer(self):
        # A registry the fallback cannot know from shape alone: under the
        # list "nsw.edu.au" and "or.us" are suffixes, without it "nsw" and
        # "or" are just labels.
        list_only = {"ana@school.nsw.edu.au", "ana@ward.portland.or.us",
                     "x@stmary.sch.uk", "ana@stmarys.lancs.sch.uk",
                     "ana@hillview.sch.ae"}
        with patch.object(psl, "rule_count", return_value=0):
            for addr, (org, academic) in CASES.items():
                if addr in list_only:
                    continue
                self.assertEqual(pp.org_from_email_domain(addr), org, addr)
                self.assertEqual(pp.is_academic_domain(addr), academic, addr)
            # Still never a registry label.
            self.assertNotIn(pp.org_from_email_domain(
                "ana@stmarys.lancs.sch.uk"), ("Sch", "Uk"))
            self.assertNotIn(pp.org_from_email_domain("x@stmary.sch.uk"),
                             ("Sch", "Uk"))
            # The fallback reads a Unicode or punycode host the same way.
            self.assertEqual(pp.org_from_email_domain("ana@müller.co.uk"),
                             "Müller")
            self.assertEqual(pp.org_from_email_domain(
                "ana@xn--mller-kva.k12.ne.us"), "Müller")

    def test_unicode_punycode_case_and_trailing_dots(self):
        # A Unicode host (which the URL parser refuses) and its punycode
        # spelling name the same org, and the list's internationalised
        # suffixes (it writes 公司.cn in Unicode) are suffixes in both.
        self.assertIsNone(psl.host_of("müller.co.uk"))
        for addr in ("ana@müller.co.uk", "ana@xn--mller-kva.co.uk",
                     "ana@mail.XN--MLLER-KVA.CO.UK", "ana@müller.k12.ne.us"):
            self.assertEqual(pp.org_from_email_domain(addr), "Müller", addr)
        for addr in ("ana@quarrystone.公司.cn",
                     "ana@quarrystone.xn--55qx5d.cn",
                     "ana@Mail.Quarrystone.CO.UK.", "ana@quarrystone.com."):
            self.assertEqual(pp.org_from_email_domain(addr), "Quarrystone",
                             addr)
        self.assertIsNone(pp.org_from_email_domain("ana@xn--55qx5d.cn"))
        # A trailing dot is the same free-mail host, not an org "Gmail".
        for addr in ("x@gmail.com.", "x@GMAIL.COM", "x@yandex.ru."):
            self.assertIsNone(pp.org_from_email_domain(addr), addr)
        self.assertIn("gmail.com.", pp._FREE_MAIL)
        # An address at an IP names no org.
        for addr in ("x@10.20.30.40", "x@[10.20.30.40]"):
            self.assertIsNone(pp.org_from_email_domain(addr), addr)
        self.assertTrue(pp.is_academic_domain("ana@cs.Northfield.AC.UK."))

    def test_the_academic_rule_is_structural(self):
        for dom in ("northfield.edu", "x.edu.pl", "x.ac.ke", "x.sch.ae",
                    "x.k12.tr", "a.b.k12.ca.us"):
            self.assertTrue(free_mail.is_academic(dom), dom)
        for dom in ("education.com", "edu", "", "x.gov", "tax.gov.uk",
                    "edu.quarrystone.com", "x.sch.example.com",
                    "academy.io"):
            self.assertFalse(free_mail.is_academic(dom), dom)


class CallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_domain_orgs_")
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")
        self.addCleanup(self.store.close)
        env = patch.dict(os.environ, {"QUILL_PEOPLE_V2": "1",
                                      "QUILL_DATA_DIR": self.tmp})
        env.start()
        self.addCleanup(env.stop)

    def affiliations(self, addr) -> set:
        pid = self.store.find_person_by_contact("email", addr)
        self.assertIsNotNone(pid, addr)
        names = {e["id"]: e["name"] for e in self.store.all_entities()}
        return {(e["predicate"], names.get(e["obj_id"]))
                for e in self.store.relations_of("person", int(pid))["out"]
                if e["obj_type"] == "entity"}

    def test_the_email_network_ingest(self):
        text = ("From: Jo Park <jo.park@moe-north.edu.sg>\n"
                "To: Ana Ortiz <ana@stmarys.lancs.sch.uk>\n"
                "Cc: Lee Wong <lee@lincoln.k12.ne.us>, "
                "Sam Diaz <sam@quarrystone.co.uk>\n\nHello")
        out = pp.ingest_email_network(
            text, store=self.store, event_id=99, event_source="desktop.screen",
            window="Inbox - Jo - Outlook", now=NOW)
        self.assertTrue(out.get("ok"), out)
        names = {e["name"] for e in self.store.all_entities()}
        self.assertFalse(names & {"Sch", "Ne", "Edu", "Uk", "Lancs", "K12"},
                         names)
        self.assertEqual(self.affiliations("jo.park@moe-north.edu.sg"),
                         {("member_of", "Moe North")})
        self.assertEqual(self.affiliations("ana@stmarys.lancs.sch.uk"),
                         {("member_of", "Stmarys")})
        self.assertEqual(self.affiliations("lee@lincoln.k12.ne.us"),
                         {("member_of", "Lincoln")})
        self.assertEqual(self.affiliations("sam@quarrystone.co.uk"),
                         {("works_at", "Quarrystone")})

    def test_the_exhaust_ingest(self):
        from app.services import exhaust_ingest as ex
        stats = {"lee@lincoln.k12.ne.us": {"name": "Lee Wong",
                                           "last_seen": NOW},
                 "ana@kestrel.ac.jp": {"name": "Ana Ortiz", "last_seen": NOW},
                 "sam@quarrystone.com.au": {"name": "Sam Diaz",
                                            "last_seen": NOW}}
        with patch.object(ex, "_ledger_path",
                          return_value=Path(self.tmp) / "l.json"):
            ex.apply_stats(self.store, stats, {}, messages=[], events=[],
                           now=NOW)
        names = {e["name"] for e in self.store.all_entities()}
        self.assertFalse(names & {"Ne", "Ac", "Com", "K12"}, names)
        self.assertEqual(self.affiliations("lee@lincoln.k12.ne.us"),
                         {("member_of", "Lincoln")})
        self.assertEqual(self.affiliations("ana@kestrel.ac.jp"),
                         {("member_of", "Kestrel")})
        self.assertEqual(self.affiliations("sam@quarrystone.com.au"),
                         {("works_at", "Quarrystone")})

    def test_the_email_domain_prior(self):
        from app.services import entity_typing as et
        pid = self.store.insert_person("Lee Wong", ts=NOW)
        for addr in ("lee@lincoln.k12.ne.us", "lee@hillview.sch.ae"):
            self.store.upsert_contact_point(
                person_id=pid, type_="email", value_display=addr,
                value_normalized=addr, confidence=0.9,
                attribution_method="email_header",
                verification_status="attributed", source_event_id=None,
                evidence_quote=None, discourse_role=None, ts=NOW,
                created_by="system", pipeline_version="t")
        et._domain_cache.clear()
        keys = et.email_domain_org_keys(self.store)
        self.assertEqual(keys, frozenset({"lincoln", "hillview"}))


if __name__ == "__main__":
    unittest.main()
