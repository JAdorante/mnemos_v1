"""The GB10 hosted pilot's safety posture, asserted against the compose file.

These containers drive a real computer on hardware that is not the testers'.
The posture that makes that acceptable lives entirely in `x-env` — one YAML
anchor, merged into six services — so a single careless edit silently widens
what six instances may do, and nothing else in the suite would notice.

Checked here rather than in config.py because the question is not "what does
the code default to" but "what does the deployment actually set", which is the
only version that runs. PyYAML is not a declared dependency, so this skips
cleanly where it is unavailable.
"""
from __future__ import annotations

import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is not a project dependency
    yaml = None

COMPOSE = (Path(__file__).resolve().parent.parent
           / "deploy" / "hosted" / "gb10" / "docker-compose.yml")

# Levels that let the agent complete an irreversible step. `approval` still
# pauses for a human, but a pilot needs no commit path at all.
COMMIT_CAPABLE = {"approval", "full", "autonomous"}


@unittest.skipIf(yaml is None, "PyYAML not installed")
@unittest.skipUnless(COMPOSE.is_file(), "GB10 compose file not present")
class HostedPostureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        self.seats = {k: v for k, v in (self.doc.get("services") or {}).items()
                      if k.startswith("sparrow-user")}

    def _env(self, seat: str) -> dict:
        return dict((self.seats[seat].get("environment") or {}))

    def test_every_seat_is_covered(self) -> None:
        """A seat added without the shared anchor would inherit nothing."""
        self.assertGreaterEqual(len(self.seats), 6)
        for seat in self.seats:
            self.assertIn("QUILL_API_TOKEN", self._env(seat), seat)

    def test_the_agent_cannot_complete_an_irreversible_step(self) -> None:
        for seat in self.seats:
            level = str(self._env(seat).get("AGENT_DRY_RUN", "")).lower()
            self.assertNotIn(level, COMMIT_CAPABLE, seat)
            self.assertEqual(level, "draft", seat)

    def test_no_seat_runs_the_agent_autonomously(self) -> None:
        for seat in self.seats:
            env = self._env(seat)
            self.assertNotIn(str(env.get("AGENT_DRY_RUN", "")).lower(),
                             ("full", "autonomous"), seat)
            # Approval binding must never be downgraded to shadow/off: it is
            # what stops an approval being reused for different arguments.
            bind = str(env.get("QUILL_APPROVAL_BIND", "enforce")).lower()
            self.assertEqual(bind, "enforce", seat)

    def test_cross_tenant_clip_playback_is_a_deliberate_opt_in(self) -> None:
        """Handing a teammate the actual recording behind a claim is the
        largest disclosure in the product. It is off unless someone turns it
        on for this deployment on purpose — never inherited by omission."""
        for seat in self.seats:
            val = str(self._env(seat).get("QUILL_PEER_CLIP_PLAYBACK", "0"))
            self.assertIn(val, ("0", "1"), seat)
        from app.config import settings
        # The code default is off, so an unset deployment discloses nothing.
        import os
        self.assertNotIn("QUILL_PEER_CLIP_PLAYBACK", os.environ)
        self.assertFalse(settings.peer.clip_playback)

    def test_phone_link_stays_off(self) -> None:
        """An outbound channel to a real phone is not part of this trial."""
        for seat in self.seats:
            val = self._env(seat).get("QUILL_PHONE_LINK")
            self.assertIn(val, (None, "0", 0, False), seat)

    def test_headless_so_there_is_no_desktop_to_act_on(self) -> None:
        """Set in the Dockerfile, not the compose — assert it is still there."""
        dockerfile = (COMPOSE.parent.parent / "Dockerfile").read_text(
            encoding="utf-8")
        self.assertIn("QUILL_AGENT_HEADLESS=1", dockerfile)
        self.assertIn("QUILL_HEADLESS=1", dockerfile)

    def test_the_trial_disclosure_pack_never_auto_answers_personal(self) -> None:
        """The compose widens the default disclosure posture; that widening
        must stop short of private material."""
        from app.services.team_layer import POLICY_PACKS
        for seat in self.seats:
            pack = str(self._env(seat).get("QUILL_PEER_DEFAULT_PACK", "")).lower()
            if not pack:
                continue
            self.assertIn(pack, POLICY_PACKS, seat)
            self.assertNotEqual(POLICY_PACKS[pack]["personal"], "auto", seat)

    def test_only_seats_with_a_cloud_key_can_reach_the_router(self) -> None:
        """Web goals need a cloud key; the rest stay local-only chat. Keeping
        that narrow is what bounds the blast radius of the browser agent."""
        keyed = [s for s in self.seats
                 if self._env(s).get("ANTHROPIC_API_KEY")]
        self.assertLessEqual(len(keyed), 1, f"cloud key on {keyed}")

    def test_no_secret_is_hardcoded_in_the_compose(self) -> None:
        """Tokens and keys come from .env interpolation, never the tracked
        file — this one is committed to git."""
        raw = COMPOSE.read_text(encoding="utf-8")
        self.assertNotIn("sk-ant", raw)
        for seat in self.seats:
            token = str(self._env(seat).get("QUILL_API_TOKEN", ""))
            self.assertTrue(token.startswith("${"), f"{seat}: {token[:12]}")


if __name__ == "__main__":
    unittest.main()
