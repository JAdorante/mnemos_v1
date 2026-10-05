"""Fleet federation: an owner's agent fleet publishes structured signals into
their own Sparrow, and Sparrows exchange those signals only through the firm
relay (org_coordinator/). See docs/fleet-federation.md.

Invariants every module here preserves:
  1. Signals carry information, never instructions. Nothing inbound reaches
     agent_planner execution, browser_agent, or desktop_agent.
  2. Sparrow decides what leaves, by rule. Agents publish locally and cannot
     address a peer.
  3. What a signal may carry is declared per kind (envelope.py); unknown
     kinds, unknown fields, and a kind's forbidden fields never validate.
  4. Signal bytes are never rewritten by an LLM in transit.
  5. Everything fails closed: no rule, no route, no forward.

This package __init__ stays import-free on purpose: the relay imports
`envelope` and must not pull in app.config or the rest of Sparrow.
"""
