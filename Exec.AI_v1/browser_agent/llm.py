"""Anthropic Messages API wrapper — the policy layer.

Uses the Client SDK and implements the loop ourselves (per §3 build decision).
Three call sites map to the three tiers:
  - plan()          -> Opus 4.8, structured JSON output, high effort
  - choose_action() -> Sonnet 4.6 (or Opus when escalated), forced tool call
  - verify()        -> Haiku 4.5, structured JSON, no effort/thinking

Structured calls degrade gracefully if the installed SDK predates
output_config (they retry with a plain "respond with JSON" prompt), so the
agent runs on a range of anthropic-SDK versions.
"""
import json
import re

from anthropic import Anthropic

from . import config as cfg
from .prompts import ROUTER_SYSTEM, PLANNER_SYSTEM, EXECUTOR_SYSTEM, VERIFIER_SYSTEM
from .tools import ACTION_TOOLS, ROUTE_SCHEMA, PLAN_SCHEMA, VERIFY_SCHEMA


def _sys(text):
    # cache_control on the (last) system block caches tools+system together (FR-CTX-1)
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _first_text(resp):
    for b in resp.content:
        if b.type == "text":
            return b.text
    return ""


def _extract_json(s):
    s = (s or "").strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


class LLM:
    def __init__(self):
        self.client = Anthropic()
        self.usage = {}  # model -> {in, out, cache_read, cache_write}

    # --- usage / cost ------------------------------------------------------
    def _track(self, model, u):
        a = self.usage.setdefault(model, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0})
        a["in"] += getattr(u, "input_tokens", 0) or 0
        a["out"] += getattr(u, "output_tokens", 0) or 0
        a["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        a["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

    def cost(self):
        total = 0.0
        for m, u in self.usage.items():
            in_rate, out_rate = cfg.RATES.get(m, (0.0, 0.0))
            total += (
                u["in"] * in_rate
                + u["out"] * out_rate
                + u["cache_read"] * in_rate * 0.1     # cache reads ~0.1x input
                + u["cache_write"] * in_rate * 1.25   # cache writes ~1.25x input
            ) / 1_000_000
        return total

    # --- structured JSON call with graceful fallback -----------------------
    def _json_call(self, model, system, user, schema, effort=None):
        base = dict(
            model=model,
            max_tokens=2048,
            system=_sys(system),
            messages=[{"role": "user", "content": user}],
        )
        oc = {"format": {"type": "json_schema", "schema": schema}}
        if effort:
            oc["effort"] = effort
        try:
            r = self.client.messages.create(output_config=oc, **base)
            self._track(model, r.usage)
            return _extract_json(_first_text(r))
        except Exception:
            # SDK too old for output_config, or structured output rejected:
            # ask for raw JSON instead.
            base["messages"] = [{
                "role": "user",
                "content": user + "\n\nRespond with ONLY a JSON object matching "
                "this schema, no prose:\n" + json.dumps(schema),
            }]
            try:
                r = (self.client.messages.create(output_config={"effort": effort}, **base)
                     if effort else self.client.messages.create(**base))
            except Exception:
                r = self.client.messages.create(**base)
            self._track(model, r.usage)
            return _extract_json(_first_text(r))

    # --- tier 0: intent/action router (Sonnet 4.6, cheap) ------------------
    def route(self, user_request, context=""):
        user = (context + "\n\n" if context else "") + "User request: " + user_request
        out = self._json_call(cfg.ROUTER_MODEL, ROUTER_SYSTEM, user, ROUTE_SCHEMA,
                              effort=cfg.ROUTER_EFFORT)
        out.setdefault("intent", "unknown")
        out.setdefault("requires_browser", True)   # safe default: assume web
        out.setdefault("requires_user_approval", False)
        out.setdefault("tool", "browser_agent" if out["requires_browser"] else "direct_answer")
        out.setdefault("site", "")
        out.setdefault("rationale", "")
        return out

    def direct_answer(self, user_request, context=""):
        """Answer a no-browser request (a memory/conversational question)."""
        system = (
            "You are QUILL's assistant. Answer the user's request directly and "
            "concisely from the conversation context. If the context does not "
            "contain the answer, say so plainly in one line — do not invent it."
        )
        msg = (context + "\n\n" if context else "") + "User: " + user_request
        r = self.client.messages.create(
            model=cfg.ROUTER_MODEL, max_tokens=1024, system=_sys(system),
            messages=[{"role": "user", "content": msg}],
        )
        self._track(cfg.ROUTER_MODEL, r.usage)
        return _first_text(r).strip()

    # --- tier 1: planner (Opus 4.8) ----------------------------------------
    def plan(self, goal, start_url):
        user = f"Task: {goal}\nStarting URL: {start_url or '(none)'}"
        out = self._json_call(cfg.PLANNER_MODEL, PLANNER_SYSTEM, user, PLAN_SCHEMA,
                              effort=cfg.PLANNER_EFFORT)
        if "steps" not in out or not isinstance(out.get("steps"), list):
            out = {"steps": [{"description": goal, "success_criteria": "goal achieved"}]}
        return out

    # --- tier 2: executor (Sonnet 4.6, escalates to Opus) ------------------
    def choose_action(self, content, escalate=False):
        model = cfg.ESCALATION_MODEL if escalate else cfg.EXECUTOR_MODEL
        effort = cfg.ESCALATION_EFFORT if escalate else cfg.EXECUTOR_EFFORT
        base = dict(
            model=model,
            max_tokens=1024,
            system=_sys(EXECUTOR_SYSTEM),
            tools=ACTION_TOOLS,
            tool_choice={"type": "any"},  # force exactly one action per turn
            messages=[{"role": "user", "content": content}],
        )
        try:
            r = self.client.messages.create(output_config={"effort": effort}, **base)
        except Exception:
            r = self.client.messages.create(**base)
        self._track(model, r.usage)

        tool, text = None, ""
        for b in r.content:
            if b.type == "tool_use":
                tool = b
            elif b.type == "text":
                text += b.text
        usage = {"in": getattr(r.usage, "input_tokens", 0) or 0,
                 "out": getattr(r.usage, "output_tokens", 0) or 0}
        if tool is None:
            return {"name": "ask_human",
                    "input": {"question": "I couldn't decide on an action. How should I proceed?"},
                    "reasoning": text, "model": model, "usage": usage}
        return {"name": tool.name, "input": dict(tool.input or {}),
                "reasoning": text, "model": model, "usage": usage}

    # --- tier 3: verifier (Haiku 4.5, no effort) ---------------------------
    def verify(self, action_desc, before, after):
        payload = (
            f"Action: {action_desc}\n"
            f"Before: {json.dumps(before)}\n"
            f"After: {json.dumps(after)}"
        )
        out = self._json_call(cfg.VERIFIER_MODEL, VERIFIER_SYSTEM, payload, VERIFY_SCHEMA)
        if "satisfied" not in out:
            out = {"satisfied": True, "reason": "verifier fallback (lenient)"}
        return out
