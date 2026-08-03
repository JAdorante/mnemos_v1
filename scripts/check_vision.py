"""Confirm the executor actually receives the screenshot (adaptive vision).

    python scripts/check_vision.py

Runs one read-only goal on example.com (a sparse page, so adaptive vision
attaches the screenshot), captures the agent's step logs, and asserts a
'+screenshot' step happened. Headless, no approval, no login.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from browser_agent.orchestrator import Agent

logs: list[str] = []
agent = Agent(headless=True, start_url="https://example.com",
              on_log=logs.append, on_ask=lambda q: "cancel")
try:
    result, status = agent.run_goal("Read the current page and report its main heading.")
finally:
    agent.close()

vision_steps = [l for l in logs if "+screenshot" in l]
print(f"status={status}")
print(f"result: {(result or '')[:120]!r}")
print(f"steps with screenshot attached: {len(vision_steps)}")
for l in vision_steps:
    print("  ", l)

assert vision_steps, "executor never received a screenshot — adaptive vision did not fire"
print("\n[check] executor vision fired.")
