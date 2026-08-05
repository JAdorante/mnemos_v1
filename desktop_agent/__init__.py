"""Guarded desktop/OS control for Mnemos — allowlist-first, default-deny.

This is the OS-side counterpart to `browser_agent/`. Where the browser agent is
sandboxed by the browser, a desktop agent has no sandbox — so *the allowlist is
the sandbox*. Nothing here runs a raw string; every action resolves through an
explicit allowlist (apps, shell verbs), is confined to a working-directory jail,
is screened for dangerous patterns, and — for anything mutating — passes a human
approval gate before it executes. Every attempt is audited.

Deliberately NOT wired into the live agent loop yet: this package is exercised on
its own so the guardrails can be reviewed in isolation first.
"""
from .driver import DesktopDriver
from .guards import Tier

__all__ = ["DesktopDriver", "Tier"]
