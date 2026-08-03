"""Unit test for structured source-grounded approval packets.

    python scripts/test_approval_packet.py

Exercises the packet renderer and the approve/edit/cancel classifier, and runs a
full _approval_decision round-trip with a scripted on_ask (no browser, no API).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_agent import config as cfg
from browser_agent.orchestrator import (
    Agent, _render_packet, _classify_approval, _NullRecorder,
)

cfg.REQUIRE_APPROVAL = True   # exercise the gate regardless of env default


def test_render():
    fields = {
        "action": "Send email",
        "to": "Marc",
        "subject": "Pricing follow-up",
        "body": "Hi Marc,\n\nFollowing up on the $49/mo pricing we discussed.\n\nJustin",
        "why": "You promised this follow-up after today's meeting.",
        "source": "Meeting transcript, 2:14 PM",
    }
    out = _render_packet(fields)
    for label in ("Action:", "To: Marc", "Subject: Pricing follow-up",
                  "Body:", "Why:", "Source: Meeting transcript, 2:14 PM"):
        assert label in out, f"missing {label!r} in packet:\n{out}"
    # long/multi-line body is broken onto its own lines
    assert "Body:\nHi Marc," in out, f"body not block-rendered:\n{out}"
    print("[test] render packet OK\n")
    print(out)
    print()

    # flat fallback: only summary/details
    flat = _render_packet({"details": "click 'Send' on gmail"})
    assert "click 'Send' on gmail" in flat
    print("[test] flat fallback OK")


def test_classify():
    cases = {
        "approve": "approve", "yes send it": "approve", "go ahead": "approve",
        "cancel": "cancel", "no, stop": "cancel", "": "cancel", "don't send": "cancel",
        "change the subject to Q3 pricing": "edit",
        "make the tone warmer": "edit", "use his work email instead": "edit",
    }
    for reply, expected in cases.items():
        got = _classify_approval(reply)
        assert got == expected, f"{reply!r}: expected {expected}, got {got}"
    print("[test] classify approve/edit/cancel OK")


def test_decision_roundtrip():
    # Drive _approval_decision with a scripted reply — no browser needed, so bind
    # the method to a bare namespace instead of constructing a real Agent. A real
    # Agent always carries a recorder + last_route, so the stand-in provides them
    # (the no-op recorder just drops what would otherwise be logged).
    class Fake:
        _log = staticmethod(lambda s: None)
        _recorder = _NullRecorder()
        last_route = None
    replies = iter(["change the subject to Q3 pricing"])
    fake = Fake()
    fake._ask_fn = lambda prompt: next(replies)
    decision, feedback = Agent._approval_decision(
        fake, "Send email to Marc",
        {"action": "Send email", "to": "Marc", "subject": "Pricing follow-up"},
    )
    assert decision == "edit" and "Q3 pricing" in feedback, (decision, feedback)
    print("[test] _approval_decision edit round-trip OK")

    replies = iter(["approve"])
    fake._ask_fn = lambda prompt: next(replies)
    decision, _ = Agent._approval_decision(fake, "Send email to Marc", {})
    assert decision == "approve"
    print("[test] _approval_decision approve round-trip OK")


if __name__ == "__main__":
    test_render()
    test_classify()
    test_decision_roundtrip()
    print("\n[test] ALL PASS")
