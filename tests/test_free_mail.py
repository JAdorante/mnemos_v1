"""G3: one free-mail list, the vendored public one, for every place that
asks whether an address names an employer.

- The list is `free-email-domains` 1.16.6 (MIT), shipped verbatim beside its
  source, version, hash and licence, and it is the only free-mail set: the
  people pipeline (and through it exhaust ingest and the meeting debrief) and
  the context keys read the same object.
- Every host the code listed before is still free mail.
- A school or government domain is never free mail, even when the list has
  it, so a synthetic `.edu` contact is affiliation evidence.
- A synthetic employer domain is an org.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import free_mail
from app.services import people_pipeline as pp
from app.services.context import keys

DATA = Path(free_mail.__file__).with_name("data")
# Every host the two old sets listed (people_pipeline._FREE_MAIL and
# context/keys.GENERIC_EMAIL_DOMAINS) before the list was vendored.
OLD_HOSTS = ("gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk",
             "hotmail.com", "outlook.com", "live.com", "msn.com", "icloud.com",
             "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com",
             "mail.com", "gmx.com", "fastmail.com", "qq.com")


class VendoredListTests(unittest.TestCase):
    def test_the_file_is_the_pinned_upstream_artifact(self):
        raw = (DATA / "freemail_domains.json").read_bytes()
        version = (DATA / "freemail_domains.VERSION").read_text()
        self.assertIn(hashlib.sha256(raw).hexdigest(), version)
        self.assertIn("1.16.6", version)
        self.assertIn("a8e19ef52d32da59a953f61ddadace2b6a87632e", version)
        data = json.loads(raw)
        self.assertEqual(len(data), 30768)
        self.assertTrue(all(isinstance(d, str) for d in data))
        lic = (DATA / "freemail_domains.LICENSE").read_text()
        self.assertIn("MIT License", lic)
        self.assertIn("Microlink", lic)

    def test_it_is_tracked_and_bundled(self):
        repo = Path(__file__).resolve().parents[1]
        import subprocess
        out = subprocess.run(
            ["git", "check-ignore", "-q",
             "app/services/data/freemail_domains.json"],
            cwd=repo, capture_output=True)
        self.assertEqual(out.returncode, 1, "the list is gitignored")
        spec = (repo / "packaging" / "mnemos.spec").read_text()
        self.assertIn('"app" / "services" / "data"', spec)

    def test_every_old_host_is_still_free_mail(self):
        for host in OLD_HOSTS:
            self.assertTrue(free_mail.is_free_mail(host), host)
            self.assertIn(host, pp._FREE_MAIL)
            self.assertIn(host, keys.GENERIC_EMAIL_DOMAINS)

    def test_a_refreshed_or_missing_list_keeps_every_old_host(self):
        # A refresh that dropped an old host, or a build without the file,
        # never makes one of them an employer.
        tmp = Path(tempfile.mkdtemp(prefix="quill_free_mail_"))
        small = tmp / "freemail_domains.json"
        small.write_text(json.dumps(["burner.example-mail.net", "x.edu.pl"]))
        self.addCleanup(setattr, free_mail, "_domains", None)
        for path in (small, tmp / "missing.json"):
            with patch.object(free_mail, "_LIST_PATH", path), \
                    patch.object(free_mail, "_domains", None):
                for host in OLD_HOSTS:
                    self.assertTrue(free_mail.is_free_mail(host), (path, host))
                self.assertFalse(free_mail.is_free_mail("x.edu.pl"))
                self.assertEqual(
                    free_mail.is_free_mail("burner.example-mail.net"),
                    path == small)

    def test_the_list_reaches_hosts_the_old_sets_missed(self):
        for host in ("yandex.ru", "web.de", "mail.ru", "zoho.com", "gmx.de"):
            self.assertTrue(free_mail.is_free_mail(f"someone@{host}"), host)
            self.assertIsNone(pp.org_from_email_domain(f"someone@{host}"))

    def test_one_set_everywhere(self):
        self.assertIs(pp._FREE_MAIL, free_mail.FREE_MAIL_DOMAINS)
        self.assertIs(keys.GENERIC_EMAIL_DOMAINS, free_mail.FREE_MAIL_DOMAINS)
        from app.services import exhaust_ingest
        self.assertIs(exhaust_ingest._FREE_MAIL, free_mail.FREE_MAIL_DOMAINS)
        self.assertEqual(len(free_mail.FREE_MAIL_DOMAINS),
                         len(free_mail.domains()))
        self.assertIn("gmail.com", set(free_mail.FREE_MAIL_DOMAINS))
        self.assertNotIn(None, free_mail.FREE_MAIL_DOMAINS)
        self.assertIn("GMAIL.COM", free_mail.FREE_MAIL_DOMAINS)


class SchoolAndGovernmentTests(unittest.TestCase):
    SCHOOLS = ("northfield.edu", "grad.northfield.edu", "ox.ac.uk",
               "moe.edu.sg", "agency.gov", "tax.gov.au", "army.mil",
               "lincoln.k12.ne.us", "stmary.sch.uk",
               # Other registries' government labels.
               "tax.go.jp", "sat.gob.mx", "interior.gouv.fr", "ird.govt.nz")

    def test_the_rule(self):
        for dom in self.SCHOOLS:
            self.assertTrue(free_mail.is_school_or_government(dom), dom)
        for dom in ("gmail.com", "acme.io", "education.com", "edu", "",
                    "clearbrook-dental.com", "gov.example.com", "go.ro",
                    "gouv.example.com"):
            self.assertFalse(free_mail.is_school_or_government(dom), dom)

    def test_a_listed_school_domain_is_not_free_mail(self):
        listed = json.loads((DATA / "freemail_domains.json").read_text())
        schools = [d for d in listed
                   if free_mail.is_school_or_government(d)]
        self.assertGreater(len(schools), 50)
        for dom in schools:
            self.assertFalse(free_mail.is_free_mail(dom), dom)
        self.assertEqual(len(free_mail.domains()),
                         len(set(listed) | set(OLD_HOSTS)) - len(schools))

    def test_a_synthetic_school_contact_is_an_affiliation(self):
        self.assertEqual(pp.org_from_email_domain("jo@northfield.edu"),
                         "Northfield")
        self.assertTrue(pp.is_academic_domain("jo@northfield.edu"))
        # A real school the public list carries is still a school.
        self.assertEqual(pp.org_from_email_domain("x@grad.bryant.edu"),
                         "Bryant")

    def test_a_synthetic_employer_is_an_org(self):
        self.assertEqual(
            pp.org_from_email_domain("ana@clearbrook-dental.com"),
            "Clearbrook Dental")
        self.assertFalse(free_mail.is_free_mail("ana@clearbrook-dental.com"))


class DownstreamTests(unittest.TestCase):
    def test_a_free_mail_address_makes_no_works_at_edge(self):
        from app.storage import Store
        tmp = tempfile.mkdtemp(prefix="quill_free_mail_")
        store = Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "a")
        self.addCleanup(store.close)
        text = ("From: Dana Reyes <dana.reyes@yandex.ru>\n"
                "To: Jo Park <jo@northfield.edu>\n"
                "Cc: Ana Ortiz <ana@clearbrook-dental.com>\n\nHello")
        with patch.dict("os.environ", {"QUILL_PEOPLE_V2": "1"}):
            out = pp.ingest_email_network(
                text, store=store, event_id=99,
                event_source="desktop.screen",
                window="Inbox - Jane - Outlook", now=1.0)
        self.assertTrue(out.get("ok"), out)
        names = {e["name"] for e in store.all_entities()}
        self.assertNotIn("Yandex", names)
        self.assertTrue({"Northfield", "Clearbrook Dental"} <= names, names)
        # The school address is membership, the employer's is employment.
        preds = set()
        for addr in ("jo@northfield.edu", "ana@clearbrook-dental.com",
                     "dana.reyes@yandex.ru"):
            pid = store.find_person_by_contact("email", addr)
            self.assertIsNotNone(pid, addr)
            preds |= {(addr, e["predicate"]) for e in
                      store.relations_of("person", int(pid))["out"]
                      if e["obj_type"] == "entity"}
        self.assertEqual(preds, {("jo@northfield.edu", "member_of"),
                                 ("ana@clearbrook-dental.com", "works_at")})

    def test_the_debrief_knows_a_colleague_from_a_stranger(self):
        from app.services.meeting_debrief import suggest_relationship
        mine = {"clearbrook-dental.com"}
        self.assertEqual(suggest_relationship("x@yandex.ru", mine), ("", ""))
        self.assertEqual(suggest_relationship(
            "b@clearbrook-dental.com", mine)[0], "colleague")
        self.assertEqual(suggest_relationship("c@quarrystone.io", mine)[0],
                         "external")
        self.assertEqual(suggest_relationship("c@quarrystone.io",
                                              {"web.de"}), ("", ""))

    def test_a_free_mail_domain_is_a_generic_key(self):
        # A domain key names an organisation; a free-mail host names none.
        self.assertTrue(keys.is_generic(keys.domain("yandex.ru")))
        self.assertTrue(keys.is_generic(keys.domain("gmail.com")))
        self.assertFalse(keys.is_generic(keys.domain("clearbrook-dental.com")))
        self.assertFalse(keys.is_generic(keys.domain("northfield.edu")))


if __name__ == "__main__":
    unittest.main()
