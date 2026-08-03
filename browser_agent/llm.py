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

from anthropic import Anthropic, BadRequestError

# The fallback `except`s below exist ONLY to degrade when the installed SDK is
# too old for `output_config` (raises TypeError: unexpected kwarg) or the API
# rejects the structured-output schema (400 BadRequestError). They must NOT
# catch transient errors — 429/5xx/529 are already retried by the SDK; letting
# them propagate to the friendly handler beats firing a wasteful second request.
_PARAM_FALLBACK = (TypeError, BadRequestError)

from . import config as cfg
from .prompts import (ROUTER_SYSTEM, PLANNER_SYSTEM, EXECUTOR_SYSTEM,
                      DESKTOP_EXECUTOR_SYSTEM, VERIFIER_SYSTEM)
from .tools import (ACTION_TOOLS, DESKTOP_TOOLS, ROUTE_SCHEMA, PLAN_SCHEMA,
                    PHONE_GOAL_SCHEMA, VERIFY_SCHEMA)

# Neutral-by-default few-shot example names (data-driven when opted in). Guarded
# so browser_agent stays importable without app.* — see prompts.py / vocabulary.py.
try:
    from app.services.vocabulary import example_terms as _example_terms
except Exception:  # pragma: no cover - defensive
    def _example_terms() -> dict:
        return {"person": "<name>", "teammate": "<name>", "company": "Acme",
                "org": "<org>", "project": "<project>"}


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
        # max_retries lifts the SDK's default (2) so a transient 529/overload is
        # ridden out with exponential backoff instead of raising in the user's face.
        self.client = Anthropic(max_retries=cfg.LLM_MAX_RETRIES)
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
        except _PARAM_FALLBACK:
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
            except _PARAM_FALLBACK:
                r = self.client.messages.create(**base)
            self._track(model, r.usage)
            return _extract_json(_first_text(r))

    # --- tier 0: intent/action router (Sonnet 4.6, cheap) ------------------
    def route(self, user_request, context=""):
        user = (context + "\n\n" if context else "") + "User request: " + user_request
        out = self._json_call(cfg.ROUTER_MODEL, ROUTER_SYSTEM, user, ROUTE_SCHEMA,
                              effort=cfg.ROUTER_EFFORT)
        out.setdefault("intent", "unknown")
        # Reconcile surface <-> requires_browser (surface is authoritative; older
        # callers/prompts may still only set requires_browser).
        surface = out.get("surface")
        if surface not in ("browser", "desktop", "phone_link", "none"):
            surface = "browser" if out.get("requires_browser", True) else "none"
        out["surface"] = surface
        out["requires_browser"] = (surface == "browser")
        out.setdefault("requires_user_approval", False)
        out.setdefault("tool", {
            "browser": "browser_agent",
            "desktop": "desktop_agent",
            "phone_link": "phone_link",
            "none": "direct_answer",
        }[surface])
        out.setdefault("site", "")
        out.setdefault("rationale", "")
        return out

    def direct_answer(self, user_request, context="", mode_guidance=""):
        """Answer a no-browser request (a memory/conversational question).

        This is plain chat, not agentic work — so when vinceo.ai's ModelRouter is
        importable and its local-first text tier is ON, the answer routes
        through it (task="chat"): free local model first, few-shot corrected
        from past verdicts, escalations distilled for the learning loop.
        Standalone agent / flag off / any router failure -> the original
        direct Claude call, byte-for-byte. Router-served calls are costed in
        model_log rather than this client's per-run tracker (local ones are
        $0 anyway). The planner/executor/route ladder stays Claude-internal
        on purpose — only the ANSWER path is chat-shaped.

        When the router writes an escalate distill row, its id is left on
        `self.last_distill_id` so the chat UI can attach a one-tap verdict
        (👍/👎/✏️ → set_user_outcome). Cleared / None when no row was written.

        `mode_guidance` is optional study-mode instruction text (homework,
        lecture notes, …) appended to the system prompt when non-empty.
        """
        self.last_distill_id = None
        system = (
            "You are vinceo.ai's assistant. Answer the user's request directly and "
            "concisely from the conversation context. If the context does not "
            "contain the answer, say so plainly in one line — do not invent it. "
            "General knowledge (world facts, definitions, conversions) may be "
            "answered from your own knowledge; never invent PERSONAL facts. "
            "When the context includes a PEOPLE YOU KNOW (contacts) section, "
            "answer people-list / contacts questions from THAT list only — do not "
            "add ambient names from working set, screen, news, or timeline as "
            "people the user knows. "
            "SESSION CONVERSATION / LAST_ASSISTANT_REPLY: when the user asks to "
            "recall, repeat, summarize, or refer to what was just said in this "
            "chat (\"what you just told me\", \"how you work\" after you explained "
            "it, \"that message\"), answer from those session turns FIRST — do not "
            "replace them with unrelated personal memories (tasks, CRM notes, "
            "desktop activity). Personal memories are for the user's life/facts; "
            "session turns are for this chat. Answer ONLY the final 'User:' line; "
            "never continue or re-answer a previous turn unless they asked you to "
            "recall it.\n\n"
            "STRUCTURE (optional, improves the UI): when explaining concepts, "
            "prefer short paragraphs; lead with one takeaway sentence; put "
            "displayed equations on their own line in \\[ ... \\]; label callouts "
            "as 'Key idea:', 'Definition:', 'Example:', or 'Warning:'. "
            "Do not ask vague follow-ups like 'Would you like me to explain more?' "
            "— the interface already offers next actions.\n\n"
            "TIME: when context includes RIGHT NOW (user's local time) and "
            "task/commitment due dates, use that clock for overdue / due today / "
            "this week — never invent today's date."
        )
        try:
            from app.services.clock import clock_instruction
            system = system + "\n\n" + clock_instruction()
        except Exception:
            pass
        guide = (mode_guidance or "").strip()
        if guide:
            system = system + "\n\n" + guide
        msg = (context + "\n\n" if context else "") + "User: " + user_request
        try:
            from app.config import settings as _app_settings
            if _app_settings.text_local.enabled:
                from app.services.model_router import router as _model_router
                reply = _model_router.complete(
                    "chat", system=system,
                    messages=[{"role": "user", "content": msg}],
                    max_tokens=1024).strip()
                if reply:
                    self.last_distill_id = _model_router.last_distill_id
                    return reply
        except ImportError:
            pass                       # standalone agent — no app package
        except Exception as exc:
            print(f"[llm] router answer failed ({exc}); using direct call.")
        r = self.client.messages.create(
            model=cfg.ROUTER_MODEL, max_tokens=1024, system=_sys(system),
            messages=[{"role": "user", "content": msg}],
        )
        self._track(cfg.ROUTER_MODEL, r.usage)
        return _first_text(r).strip()

    def parse_phone_goal(self, goal, context=""):
        """Extract Phone Link action parameters from a natural-language goal."""
        ex = _example_terms()
        system = (
            "Parse the user's request into a Phone Link (iPhone SMS on Windows) "
            "action. Use send_sms when they want to text/message someone. Use "
            "read_messages to list or read texts. Use open only to launch Phone "
            "Link with no other action. Use reply when responding to a specific "
            "person/thread mentioned in context.\n"
            "RECIPIENT: take the contact name/number from what the user said. The "
            "RELEVANT MEMORIES block may help identify WHO is meant, but the name "
            "still comes from the user's words.\n"
            "MESSAGE: set it to (1) the words the user dictated to send, or (2) "
            "when they refer to prior chat content (\"the message you just told "
            "me\", \"that summary\", \"what you just said\"), the text of "
            "LAST_ASSISTANT_REPLY / the matching SESSION CONVERSATION result — "
            "copy that assistant reply as the message body. If they only named a "
            f"recipient with no message and no session reference (e.g. 'text {ex['person']}'), "
            "leave message EMPTY. NEVER invent a body. NEVER lift unrelated open "
            "tasks or other personal memories as the SMS body — only session "
            "assistant replies when the user points at them."
        )
        user = (context + "\n\n" if context else "") + "Goal: " + goal
        return self._json_call(cfg.ROUTER_MODEL, system, user, PHONE_GOAL_SCHEMA,
                               effort=cfg.ROUTER_EFFORT)

    # --- voice-typo correction (recipient grounding + body cleanup) --------
    def resolve_recipient(self, spoken, contacts, context=""):
        """Snap a voice-transcribed recipient to a real contact.

        Fuzzy match first (deterministic, free); only ask the model to break a
        tie when the match is ambiguous or weak. Returns a dict:
        {name, original, changed, confidence, alternatives, reason}. Falls back
        to the spoken name on any error, so a bad correction never blocks a send.
        """
        from .voice_correct import rank, _norm, safe_to_remap

        spoken = (spoken or "").strip()
        out = {"name": spoken, "original": spoken, "changed": False,
               "confidence": "none", "alternatives": [], "reason": ""}
        if not spoken or not contacts:
            return out
        ranked = rank(spoken, contacts)
        if not ranked:
            return out
        # Similarity floors. A WRONG correction is worse than none — keep what
        # the user said when the real contact isn't clearly in the list.
        # Multi-token names need a higher bar (full name → different person is
        # the failure mode we most need to prevent).
        spoken_tokens = _norm(spoken).split()
        AUTO_ACCEPT = 0.85 if len(spoken_tokens) >= 2 else 0.72
        TIEBREAK_FLOOR = 0.55 if len(spoken_tokens) >= 2 else 0.5

        top_name, top_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0

        # Spoken value already IS a real contact — nothing to fix.
        if _norm(spoken) == _norm(top_name):
            out.update(name=top_name, confidence="high",
                       reason="already an exact contact match")
            return out

        # Confident, unambiguous winner — and must be the same person.
        if (top_score >= AUTO_ACCEPT and (top_score - second) >= 0.1
                and safe_to_remap(spoken, top_name)):
            out.update(name=top_name, changed=True, confidence="high",
                       reason=f"closest contact (similarity {top_score})",
                       alternatives=[c for c, _ in ranked[1:4]
                                     if _ > 0.5 and safe_to_remap(spoken, c)])
            return out

        # Ambiguous — model tiebreak ONLY among same-person-safe candidates.
        cands = [c for c, s in ranked[:6]
                 if s >= TIEBREAK_FLOOR and safe_to_remap(spoken, c)]
        if not cands:
            return out
        schema = {"type": "object", "properties": {
            "name": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"}},
            "required": ["name"], "additionalProperties": False}
        system = (
            "The user dictated a contact name by voice; speech-to-text may have "
            "garbled it. Choose the single most likely intended contact from the "
            "candidate list — consider phonetic similarity and the memory context "
            "of who the user talks to. Return the chosen name EXACTLY as it appears "
            "in the list. If none plausibly match, return the original spoken name. "
            "Never pick a different person (different given name) just because they "
            "appear in the list."
        )
        user = ((context + "\n\n" if context else "")
                + f"Spoken (possibly mis-heard): {spoken}\n"
                + "Candidate contacts:\n- " + "\n- ".join(cands))
        try:
            r = self._json_call(cfg.ROUTER_MODEL, system, user, schema,
                                 effort=cfg.ROUTER_EFFORT)
        except Exception:
            return out
        chosen = (r.get("name") or "").strip()
        # Only trust a choice that is actually one of the safe candidates.
        match = next((c for c in cands if _norm(c) == _norm(chosen)), None)
        if (match and _norm(match) != _norm(spoken)
                and safe_to_remap(spoken, match)):
            out.update(name=match, changed=True,
                       confidence=r.get("confidence") or "medium",
                       reason=r.get("reason") or "model tiebreak",
                       alternatives=[c for c in cands if _norm(c) != _norm(match)][:3])
        return out

    def clean_message(self, text, context=""):
        """Fix speech-to-text errors in a dictated message body, preserving the
        user's exact wording/tone. Returns {text, original, changed, note}; on any
        error returns the text unchanged."""
        text = (text or "").strip()
        out = {"text": text, "original": text, "changed": False, "note": ""}
        if not text:
            return out
        try:
            from .phone_parse import message_looks_clean
            if message_looks_clean(text):
                out["note"] = "skipped_clean"
                return out
        except Exception:
            pass
        schema = {"type": "object", "properties": {
            "cleaned": {"type": "string"},
            "changed": {"type": "boolean"},
            "note": {"type": "string"}},
            "required": ["cleaned", "changed"], "additionalProperties": False}
        system = (
            "You fix speech-to-text transcription errors in a SHORT text message "
            "the user dictated out loud. Correct ONLY clear STT mistakes: "
            "homophones (their/there), run-together words, spoken numbers, times "
            "and dates ('at too'->'at 2', 'meet at for thirty'->'meet at 4:30'), "
            "and obvious missing punctuation or capitalization. PRESERVE the user's "
            "wording, tone, slang, and meaning exactly — never add, drop, or rephrase "
            "content, and never make it more formal. If nothing needs fixing, return "
            "it unchanged with changed=false. 'note' is a brief phrase naming the fix."
        )
        try:
            r = self._json_call(cfg.ROUTER_MODEL, system, "Message: " + text, schema,
                                 effort=cfg.ROUTER_EFFORT)
        except Exception:
            return out
        cleaned = (r.get("cleaned") or "").strip()
        if cleaned and r.get("changed") and cleaned != text:
            out.update(text=cleaned, changed=True, note=(r.get("note") or "").strip())
        return out

    # --- tier 1: planner (Opus 4.8) ----------------------------------------
    def plan(self, goal, start_url):
        user = f"Task: {goal}\nStarting URL: {start_url or '(none)'}"
        out = self._json_call(cfg.PLANNER_MODEL, PLANNER_SYSTEM, user, PLAN_SCHEMA,
                              effort=cfg.PLANNER_EFFORT)
        if "steps" not in out or not isinstance(out.get("steps"), list):
            out = {"steps": [{"description": goal, "success_criteria": "goal achieved"}]}
        return out

    # --- tier 2: executor (Sonnet 4.6, escalates to Opus) ------------------
    def choose_action(self, content, escalate=False, image=None):
        model = cfg.ESCALATION_MODEL if escalate else cfg.EXECUTOR_MODEL
        effort = cfg.ESCALATION_EFFORT if escalate else cfg.EXECUTOR_EFFORT
        # When a screenshot is provided (DOM view is thin, or we're stuck), send
        # it alongside the text so Claude can read the pixels — its own OCR.
        if image:
            import base64

            b64 = base64.standard_b64encode(image).decode("utf-8")
            user_content = [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": content},
            ]
        else:
            user_content = content
        base = dict(
            model=model,
            max_tokens=1024,
            system=_sys(EXECUTOR_SYSTEM),
            tools=ACTION_TOOLS,
            tool_choice={"type": "any"},  # force exactly one action per turn
            messages=[{"role": "user", "content": user_content}],
        )
        try:
            r = self.client.messages.create(output_config={"effort": effort}, **base)
        except _PARAM_FALLBACK:
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

    # --- desktop executor (Sonnet, forced tool call) -----------------------
    def choose_desktop_action(self, content, escalate=False, image=None):
        """Pick one desktop action (make_dir/launch_app/run_command/...). Mirrors
        choose_action but over the desktop tool vocabulary."""
        model = cfg.ESCALATION_MODEL if escalate else cfg.EXECUTOR_MODEL
        effort = cfg.ESCALATION_EFFORT if escalate else cfg.EXECUTOR_EFFORT
        if image:
            import base64

            b64 = base64.standard_b64encode(image).decode("utf-8")
            user_content = [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": content},
            ]
        else:
            user_content = content
        base = dict(
            model=model,
            # Roomy: a write_file action carries the full file text as an argument.
            max_tokens=8192,
            system=_sys(DESKTOP_EXECUTOR_SYSTEM),
            tools=DESKTOP_TOOLS,
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": user_content}],
        )
        try:
            r = self.client.messages.create(output_config={"effort": effort}, **base)
        except _PARAM_FALLBACK:
            r = self.client.messages.create(**base)
        self._track(model, r.usage)
        tool, text = None, ""
        for b in r.content:
            if b.type == "tool_use":
                tool = b
            elif b.type == "text":
                text += b.text
        if tool is None:
            return {"name": "ask_human",
                    "input": {"question": "I couldn't decide on a desktop action. "
                              "How should I proceed?"}}
        return {"name": tool.name, "input": dict(tool.input or {})}

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
