"""CAL Stage 0 — context-key extraction (app/services/context/keys.py).

These are the deterministic identifiers that let ~95% of events resolve without
a model, so the tests care about three things: that different spellings of one
identity collapse to one key, that weak signals are refused unless composed
with a strong scope, and that generic keys can never bind.
"""
from __future__ import annotations

import os
import unittest

from app.services.context import keys as k


REPO = "git@github.com:JAdorante/mnemos_v1.git"


class RemoteTests(unittest.TestCase):
    def test_every_spelling_collapses_to_one_key(self) -> None:
        """The same repo over ssh, https and git:// must be ONE node.

        Otherwise a terminal event and a browser event about the same work land
        on different keys and the graph splits the project in two.
        """
        want = "repo:github.com/jadorante/mnemos_v1"
        for url in (
            "git@github.com:JAdorante/mnemos_v1.git",
            "ssh://git@github.com:22/JAdorante/mnemos_v1.git",
            "https://github.com/jadorante/mnemos_v1",
            "https://ghp_tok@github.com/JAdorante/mnemos_v1.git/",
            "git://github.com/JAdorante/mnemos_v1",
        ):
            self.assertEqual(k.remote(url).key, want, url)

    def test_non_default_port_is_kept(self) -> None:
        sk = k.remote("ssh://git@git.acme.com:2222/team/svc.git")
        self.assertEqual(sk.key, "repo:git.acme.com:2222/team/svc")

    def test_local_and_junk_remotes_are_not_identities(self) -> None:
        for url in ("file:///home/x/repo", "/home/x/repo", "", "   ",
                    "git@localhost:repo.git"):
            self.assertIsNone(k.remote(url), url)

    def test_remote_is_identity_class_and_strong(self) -> None:
        sk = k.remote(REPO)
        self.assertEqual((sk.key_class, sk.tier), (k.IDENTITY, k.STRONG))


class ScopedKeyTests(unittest.TestCase):
    """§3.3 — composition is what promotes a weak signal to a strong one."""

    def test_file_key_is_repo_scoped(self) -> None:
        sk = k.file(k.remote(REPO), "./app/services/graph.py")
        self.assertEqual(
            sk.key, "file:github.com/jadorante/mnemos_v1#app/services/graph.py")
        self.assertEqual(sk.tier, k.STRONG)
        self.assertEqual(sk.scope, "repo:github.com/jadorante/mnemos_v1")

    def test_unscoped_filename_mints_nothing(self) -> None:
        """A bare filename stays a mention. `entity_resolver.py` exists in a
        hundred repos; binding it to whatever was open is exactly the bleed the
        design forbids."""
        self.assertIsNone(k.file("", "entity_resolver.py"))
        self.assertIsNone(k.file("entity_resolver.py", "x.py"))

    def test_weak_scope_is_refused(self) -> None:
        weak = k.bundle("com.todesktop.230313mzl4w4u92")   # supporting tier
        self.assertIsNone(k.file(weak, "app/x.py"))

    def test_traversal_cannot_escape_the_repo(self) -> None:
        repo = k.remote(REPO)
        for bad in ("../etc/passwd", "a/../../b", "..", "a/..", ".//../x",
                    "/", "./"):
            self.assertIsNone(k.file(repo, bad), bad)

    def test_branch_is_always_repo_scoped(self) -> None:
        repo = k.remote(REPO)
        sk = k.branch(repo, "refs/heads/feature/context-router")
        self.assertEqual(
            sk.key, "branch:github.com/jadorante/mnemos_v1#feature/context-router")
        self.assertIsNone(k.branch("path:/home/x", "feature/y"))
        self.assertIsNone(k.branch(repo, "HEAD"))


class GenericKeyTests(unittest.TestCase):
    """§3.4 — real identifiers that carry no project signal must not bind."""

    def test_generic_keys_demote_to_supporting(self) -> None:
        repo = k.remote(REPO)
        for sk in (k.domain("www.google.com"), k.branch(repo, "main"),
                   k.bundle("com.google.chrome"), k.path("~/Downloads")):
            self.assertEqual(sk.tier, k.SUPPORTING, sk.key)
            self.assertLessEqual(sk.strength, 0.3, sk.key)

    def test_discriminating_keys_keep_their_tier(self) -> None:
        repo = k.remote(REPO)
        self.assertEqual(k.domain("ravenry.ai").tier, k.STRONG)
        self.assertEqual(k.branch(repo, "feature/x").tier, k.MEDIUM)

    def test_consumer_mail_host_is_generic_as_a_domain_not_as_a_person(self) -> None:
        """A personal gmail address identifies a PERSON precisely;
        `domain:gmail.com` identifies no organization at all."""
        self.assertEqual(k.email("bob@gmail.com").tier, k.STRONG)
        self.assertEqual(k.domain("gmail.com").tier, k.SUPPORTING)


class DomainTests(unittest.TestCase):
    def test_registrable_not_hostname(self) -> None:
        self.assertEqual(k.domain("https://mail.acme.com/inbox").key,
                         "domain:acme.com")
        self.assertEqual(k.domain("shop.acme.co.uk").key, "domain:acme.co.uk")

    def test_private_suffixes_keep_the_org_subdomain(self) -> None:
        """One org per subdomain — collapsing these would merge every Jira
        customer into a single node."""
        self.assertEqual(k.domain("acme.atlassian.net").key,
                         "domain:acme.atlassian.net")
        self.assertEqual(k.domain("https://team.slack.com/x").key,
                         "domain:team.slack.com")

    def test_ip_and_localhost_are_not_domains(self) -> None:
        for host in ("127.0.0.1", "localhost", "192.168.1.4:8000", ""):
            self.assertIsNone(k.domain(host), host)


class EmailAndThreadTests(unittest.TestCase):
    def test_display_name_and_plus_tag_are_stripped(self) -> None:
        self.assertEqual(k.email("Sarah B <Sarah.B+newsletter@Ravenry.AI>").key,
                         "email:sarah.b@ravenry.ai")

    def test_thread_roots_on_references_head(self) -> None:
        """Rooting on References[0] is what keeps a four-deep reply on the same
        key as the message that started it."""
        sk = k.thread(references="<root@acme.com> <mid@acme.com>",
                      in_reply_to="<mid@acme.com>",
                      message_id="<leaf@acme.com>")
        self.assertEqual(sk.key, "thread:root@acme.com")

    def test_thread_falls_back_through_reply_then_self(self) -> None:
        self.assertEqual(k.thread(in_reply_to="<a@x.com>").key, "thread:a@x.com")
        self.assertEqual(k.thread(message_id="<b@x.com>").key, "thread:b@x.com")
        self.assertIsNone(k.thread(message_id="not an id"))


class ChannelAndCalendarTests(unittest.TestCase):
    def test_channel_uses_ids(self) -> None:
        self.assertEqual(k.channel("Slack", "T04XX", "C07YY").key,
                         "channel:slack/T04XX/C07YY")

    def test_channel_name_is_refused(self) -> None:
        """Names get renamed and `#eng` exists in every workspace — a key minted
        from one would be wrong later, silently."""
        self.assertIsNone(k.channel("slack", "T04XX", "#general"))

    def test_calendar_uid(self) -> None:
        self.assertEqual(k.calendar_uid("<ABC-123-DEF@google.com>").key,
                         "calendar_uid:abc-123-def@google.com")
        self.assertIsNone(k.calendar_uid("short"))


class IssueKeyTests(unittest.TestCase):
    def test_finds_and_normalizes(self) -> None:
        got = [sk.key for sk in k.issues("fixed LIN-412, see ABC-0099 and LIN-412")]
        self.assertEqual(got, ["issue:LIN-412", "issue:ABC-99"])

    def test_lookalikes_are_denied(self) -> None:
        """`UTF-8` and `SHA-1` are shaped like issue keys and never are."""
        self.assertEqual(k.issues("UTF-8 SHA-1 RFC-822 COVID-19 HTTP-2"), [])


class PathTests(unittest.TestCase):
    def test_path_is_absolute_and_case_normalized(self) -> None:
        sk = k.path(os.getcwd())
        self.assertTrue(os.path.isabs(sk.key_value))
        self.assertEqual(sk.key_value, os.path.normcase(sk.key_value))

    def test_relative_fragment_is_not_a_path_key(self) -> None:
        """A bare word mined out of prose must not become a key rooted at the
        reading process's cwd — that binding describes us, not the user."""
        for frag in ("console", "./console", "a/b", ""):
            self.assertIsNone(k.path(frag, resolve=False), frag)

    def test_path_is_convention_class(self) -> None:
        """Paths are habits, not authorities — medium at best, so one sighting
        can never bind on its own."""
        sk = k.path(os.getcwd())
        self.assertEqual((sk.key_class, sk.tier), (k.CONVENTION, k.MEDIUM))

    def test_unresolvable_path_still_yields_a_key(self) -> None:
        sk = k.path("/no/such/dir/xyz", resolve=False)
        self.assertEqual(sk.key_value, os.path.normcase("/no/such/dir/xyz"))


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
