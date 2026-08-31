"""ModelRouter — the one seam every text-model call goes through.

Three jobs: (1) pick the model for a task type from one place (env-overridable),
(2) log every call to `model_log` (latency, tokens, cost) so total spend is
measurable — not just vision, and (3) the local-first tier: with
QUILL_TEXT_LOCAL=1, `complete()`/`complete_json()` run a local Ollama text
model first and escalate to Claude only when the local model errors, its
output doesn't parse, it self-reports low confidence, or the task is
high-stakes (QUILL_TEXT_HIGH_STAKES_TASKS, default `plan`) — the same tiering
`VLMRouter` (vlm.py) uses for frames, feeding the same `escalate_log` distill
trail (modality="text") and the same `model_log`, so `/console/models` shows
the local-first savings. With the flag off (the default), routing is
Claude-only, unchanged.

The local pass also gets retrieval few-shot correction (few_shot.py): similar
past escalations with human-verified parent answers are injected into the
LOCAL prompt as worked examples — the parent prompt and the distill row's
stored training input stay clean.

Vision has its own specialized `VLMRouter` (it juggles image providers +
escalation). The browser agent's executor escalation (Sonnet -> Opus on a
stalled step, browser_agent/config.py) is likewise a separate, Claude-internal
ladder — deliberately NOT routed through this policy.

Add a task by giving it a default model in `MODELS`; call `router.complete(task,
...)` and the logging + local-first policy are automatic.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

from app.config import settings
from app.services.model_log import model_log

# Per-thread: distill row id from the most recent complete()/_local_first call
# (None when no row was written — e.g. local-first off, or a non-verdict task
# kept its local answer). Chat UI reads this immediately after complete() to
# attach 👍/👎/✏️ to the bubble.
_tls = threading.local()

# Tasks whose answers get a verdict surface in the chat UI. Local-KEPT answers
# on these tasks write a distill row too (reason="local_kept") so every bubble
# is labelable — a wrong-but-confident local answer that never escalated is
# exactly the one that must be correctable, and a 👍 promotes the LOCAL text
# to a verified answer (few_shot._row_answer / bench gold fall back to it).
# Other tasks (extract/reflect/activity run constantly, no verdict surface)
# skip this — their kept answers would be pure trail bloat.
_VERDICT_TASKS = frozenset({"chat"})

# task -> default model id. Override per task with QUILL_<TASK>_MODEL.
MODELS: dict[str, str] = {
    # Haiku, matching extractor.EXTRACTOR_MODEL — the two read the SAME env
    # var and used to disagree on the default, so an extract call that reached
    # the router without an explicit model= silently paid Opus prices for a
    # task telemetry showed Haiku handling at ~1/100th the spend.
    "extract": os.environ.get("QUILL_EXTRACT_MODEL", "claude-haiku-4-5"),
    "chat": os.environ.get("QUILL_CHAT_MODEL", "claude-opus-4-8"),
    # Plan 3.3 — local-eligible route classifier (not high-stakes / not ambient).
    "query_route": os.environ.get("QUILL_QUERY_ROUTE_MODEL",
                                  os.environ.get("QUILL_CHAT_MODEL",
                                                 "claude-opus-4-8")),
    # Meeting Layer P3 — session enhance (quality over cost; Sonnet-class).
    "enhance": os.environ.get("QUILL_ENHANCE_MODEL", "claude-sonnet-4-6"),
    # Org AI Network — Anthropic parent for rollups / escalation / cascade.
    "org_digest": os.environ.get("QUILL_ORG_DIGEST_MODEL", "claude-sonnet-4-6"),
    "org_rollup": os.environ.get("QUILL_ORG_ROLLUP_MODEL", "claude-sonnet-4-6"),
    "org_escalate": os.environ.get("QUILL_ORG_ESCALATE_MODEL", "claude-opus-4-8"),
    "org_cascade": os.environ.get("QUILL_ORG_CASCADE_MODEL", "claude-sonnet-4-6"),
}

# Distill rows are a training signal. `prompt_head` and browse fields stay
# truncated for readability, but with full-fidelity ON (QUILL_DISTILL_FULL,
# the default) each TEXT row also carries the untruncated system/messages/
# output — you cannot fine-tune or evaluate on a 500-char prompt head.
_DISTILL_PROMPT_CHARS = 500
_DISTILL_OUTPUT_CHARS = 2000


def _text_cfg():
    """Indirection so tests can patch the policy without mutating frozen settings."""
    return settings.text_local


def _clip(text: str | None, limit: int) -> str:
    t = text or ""
    return t if len(t) <= limit else t[:limit] + "…"


# Retrieval evidence only counts when the local answer AGREES with the
# verified answer it matched (embedding cosine >= this). First calibration
# attempt floored on prompt-similarity alone and kept sim-0.09 answers local —
# a strong prompt match whose answer the model then contradicts is evidence of
# failure, not skill.
_CONSISTENCY_MIN = 0.5


def evidence_examples(local_text: str | None,
                      examples: list[dict]) -> list[dict]:
    """The subset of examples usable as confidence evidence: all of them when
    the local answer is consistent with the top match's verified answer, else
    none. Best-effort — any failure means "no evidence", never a crash."""
    if not local_text or not examples:
        return []
    try:
        import numpy as np
        from app.services.embeddings import embedder
        top = max(examples, key=lambda e: float(e.get("sim") or 0.0))
        v = embedder.encode_many([local_text, str(top.get("answer") or "")])
        if float(np.dot(v[0], v[1])) >= _CONSISTENCY_MIN:
            return examples
    except Exception as exc:  # pragma: no cover
        print(f"[model_router] consistency check skipped ({exc}).")
    return []


def effective_confidence(self_conf: float | None, examples: list[dict],
                         *, weight: float) -> float | None:
    """Calibrated confidence: self-report blended with retrieval evidence.

    Small local models are badly miscalibrated on self-report (bench 2026-07-17:
    replies at sim 0.63-0.71 to the verified answer self-scored 0.0). The
    similarity of the top retrieved HUMAN-VERIFIED example is independent,
    measured evidence that this prompt is in known territory — so it sets a
    floor (top_sim * weight, capped at 0.95: retrieval alone never grants full
    trust) that self-report can raise but not undercut. With no examples the
    self-report passes through untouched — including None (= unsure), so
    out-of-distribution prompts keep today's escalate-by-default behavior.
    The bench (scripts/bench_text.py) uses this same function, so calibration
    changes are measured, not vibes."""
    if not examples or weight <= 0:
        return self_conf
    top = max(float(e.get("sim") or 0.0) for e in examples)
    floor = min(0.95, top * weight)
    return max(float(self_conf or 0.0), floor)


# Suspect-answer heuristics (2026-07-17 live failures): self-reported
# confidence cannot be trusted on two answer shapes a small model produces
# confidently — (a) a REFUSAL when the prompt visibly carried substantive
# context (the model ignored what it was given; "no relevant memories" while
# the activity log sat in the prompt), and (b) an ECHO that restates the
# request instead of performing it ("You want me to summarize your day.").
# Both force escalation regardless of confidence.
_REFUSAL_ANSWER = re.compile(
    r"\b(?:(?:don'?t|do not|doesn'?t|does not) have (?:any|enough|that|a|"
    r"relevant|information|memor)|no (?:information|memor(?:y|ies)|record)|"
    r"i don'?t know|not (?:enough|sufficient) (?:information|context))",
    re.I)


def _has_substantive_context(messages: list | None) -> bool:
    """True when the last user message visibly carries retrieved material —
    two or more bullet lines (the grounding sections render as bullets)."""
    if not messages:
        return False
    from app.services.ollama_text import _flatten
    for m in reversed(messages):
        if m.get("role", "user") == "user":
            text = _flatten(m.get("content"))
            return text.count("\n- ") >= 2
    return False


def _looks_like_echo(answer: str, messages: list | None) -> bool:
    """Answer that merely restates the request (high char-overlap, no new
    content). Cheap sequence-ratio check on the tail of the user message."""
    a = " ".join((answer or "").lower().split())
    if not a or len(a) > 160:
        return False
    if not messages:
        return False
    from app.services.ollama_text import _flatten
    tail = ""
    for m in reversed(messages):
        if m.get("role", "user") == "user":
            tail = " ".join(_flatten(m.get("content")).lower().split())[-400:]
            break
    if not tail:
        return False
    import difflib
    q = tail[-len(a) - 80:] if len(tail) > len(a) + 80 else tail
    return difflib.SequenceMatcher(None, a, q).ratio() > 0.6


def suspect_answer(answer: str, messages: list | None) -> str | None:
    """Return an escalate reason when the local answer can't be trusted at any
    self-reported confidence, else None."""
    if _REFUSAL_ANSWER.search(answer or "") and _has_substantive_context(messages):
        return "refusal_despite_context"
    if _looks_like_echo(answer, messages):
        return "echo_answer"
    return None


# User-initiated tasks — same set spend_cap exempts from the ambient budget.
# They keep their configured (accurate) parent even on infra-driven
# escalations: the user asked, so answer quality is the point.
_USER_TASKS = frozenset({"chat", "plan"})


def _min_conf(cfg, task: str) -> float:
    """Per-task confidence floor: QUILL_TEXT_ESCALATE_MIN_CONF_<TASK> wins
    over the global escalate_min_conf. Env-first at call time (the codebase's
    runtime-knob convention) so chat can run a lower bar than extract without
    a restart."""
    raw = os.environ.get(f"QUILL_TEXT_ESCALATE_MIN_CONF_{task.upper()}")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return cfg.escalate_min_conf


def _answer_sim(a: str | None, b: str | None) -> float | None:
    """Embedding cosine between two answers, or None when either is empty or
    the embedder is unavailable. Best-effort — never raises. Only runs when
    the embeddings module is already imported (it always is once grounding
    has run): scoring one pair is never worth a cold model load."""
    if not (a or "").strip() or not (b or "").strip():
        return None
    import sys
    if "app.services.embeddings" not in sys.modules:
        return None
    try:
        import numpy as np
        from app.services.embeddings import embedder
        v = embedder.encode_many([a, b])
        return float(np.dot(v[0], v[1]))
    except Exception as exc:  # pragma: no cover
        print(f"[model_router] answer-sim skipped ({exc}).")
        return None


def _prompt_chars(system: str | None, messages: list | None) -> int:
    """Total characters the local model would have to hold for this call."""
    from app.services.ollama_text import _flatten
    n = len(system or "")
    for m in (messages or []):
        try:
            n += len(_flatten(m.get("content")))
        except Exception:
            continue
    return n


def _prompt_head(messages: list | None) -> str:
    """First ~500 chars of the last user message — enough to identify the call."""
    if not messages:
        return ""
    from app.services.ollama_text import _flatten
    for m in reversed(messages):
        if m.get("role", "user") == "user":
            return _clip(_flatten(m.get("content")), _DISTILL_PROMPT_CHARS)
    return ""


class ModelRouter:
    def __init__(self) -> None:
        self._client = None
        self._local = None                 # lazy OllamaText
        self._local_ok: bool | None = None  # availability probe (see _use_local)
        self._local_probe_t: float = 0.0    # when the cached probe result landed
        self._warned = False

    def _ensure(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _ensure_local(self):
        if self._local is None:
            from app.services.ollama_text import OllamaText

            self._local = OllamaText()
        return self._local

    def model_for(self, task: str) -> str:
        return MODELS.get(task, "claude-opus-4-8")

    # --- Claude tier (today's path, byte-for-byte when local is off) --------
    def _complete_claude(self, task: str, *, system: str, messages: list,
                         max_tokens: int = 1024, schema: dict | None = None,
                         model: str | None = None) -> str:
        """Run one Claude text completion, logged. Returns the first text block.

        `schema` (a JSON Schema) enforces structured output. `model` overrides
        the task default. Raises on API error (after logging the failure).

        Ambient tasks (extract/reflect/activity/… — see perception.spend_cap)
        draw from the USD/day cloud budget and raise BudgetExhausted when it
        is spent. The local-first path converts that into "keep the local
        answer"; Claude-only ambient callers already treat an extract failure
        as retry-later, which is the required degrade/re-queue behavior.
        User-initiated tasks (chat, plan) are never capped here.

        Plan 6.1: privacy_class gate — never-send refuses; sensitive/personal
        are redacted before Anthropic. Local Ollama path is unaffected.

        Latency program, Phase 3.3: a call inside a speculative scope never
        gets here. The check is on the paid call itself, not on the routing
        logic above it, so a future code path that forgets the policy fails
        loudly instead of quietly spending money on a guess."""
        from app.services.model_log import (
            SpeculativeCloudCall, in_speculative_scope)
        if in_speculative_scope():
            raise SpeculativeCloudCall(
                f"speculative {task!r} call attempted to reach the paid parent; "
                "speculative work is local-only by policy")
        try:
            from app.perception.spend_cap import spend_cap
            spend_cap.check(task)
        except ImportError:
            pass
        # Egress gate (plan 6.1) — must run before any bytes leave the machine.
        privacy_cls = "internal"
        privacy_action = "allow"
        try:
            from app.services.privacy_class import PrivacyRefuse, gate_cloud
        except Exception:
            PrivacyRefuse = None  # type: ignore
            gate_cloud = None  # type: ignore
        if gate_cloud is not None:
            try:
                system, messages, privacy_cls, privacy_action = gate_cloud(
                    system, messages)
            except Exception as exc:
                if PrivacyRefuse is not None and isinstance(exc, PrivacyRefuse):
                    try:
                        model_log.log_call(
                            task=task, provider="claude",
                            model=model or self.model_for(task),
                            latency_s=0.0, ok=False,
                            privacy_max=exc.privacy_class,
                            meta={"privacy_class": exc.privacy_class,
                                  "privacy_action": "refuse",
                                  "privacy_kinds": exc.kinds})
                    except Exception:
                        pass
                    raise
                try:
                    from app.services.redact import redact_text, redact_payload
                    system = redact_text(system or "")
                    messages = redact_payload(messages or [])
                    privacy_action = "redact_fallback"
                except Exception:
                    pass
        else:
            try:
                from app.services.redact import redact_text, redact_payload
                system = redact_text(system or "")
                messages = redact_payload(messages or [])
                privacy_action = "redact_fallback"
            except Exception:
                pass
        model = model or self.model_for(task)
        # The parent is a configured account: with a non-Anthropic provider
        # (OpenAI / Gemini / Grok, parent_model.py) the escalation detours to
        # its OpenAI-compatible dialect — AFTER the privacy gate above, so
        # every parent gets identical egress hygiene. Anthropic stays on this
        # client, byte-for-byte.
        from app.services import parent_model
        pid = parent_model.provider()
        if pid != "anthropic":
            t0 = time.time()
            try:
                resp_p = parent_model.complete(
                    model=model, system=system, messages=messages,
                    max_tokens=max_tokens, schema=schema)
            except Exception:
                model_log.log_call(task=task, provider=pid, model=model,
                                   latency_s=time.time() - t0, ok=False,
                                   privacy_max=privacy_cls,
                                   meta={"privacy_class": privacy_cls,
                                         "privacy_action": privacy_action})
                raise
            model_log.log_call(
                task=task, provider=pid, model=resp_p["model"],
                latency_s=time.time() - t0, ok=True,
                input_tokens=resp_p["input_tokens"],
                output_tokens=resp_p["output_tokens"],
                privacy_max=privacy_cls,
                meta={"privacy_class": privacy_cls,
                      "privacy_action": privacy_action},
            )
            return resp_p["text"]
        kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens,
                                  "system": system, "messages": messages}
        if schema is not None:
            kwargs["output_config"] = {"format": {"type": "json_schema",
                                                  "schema": schema}}
        t0 = time.time()
        try:
            resp = self._ensure().messages.create(**kwargs)
        except Exception:
            model_log.log_call(task=task, provider="claude", model=model,
                               latency_s=time.time() - t0, ok=False,
                               privacy_max=privacy_cls,
                               meta={"privacy_class": privacy_cls,
                                     "privacy_action": privacy_action})
            raise
        u = getattr(resp, "usage", None)
        model_log.log_call(
            task=task, provider="claude", model=model,
            latency_s=time.time() - t0, ok=True,
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            privacy_max=privacy_cls,
            meta={"privacy_class": privacy_cls,
                  "privacy_action": privacy_action},
        )
        return next((b.text for b in resp.content if b.type == "text"), "")

    def local_available(self) -> bool:
        """Availability of the local text tier (probe cached per process) —
        lets the agent bridge decide whether local-only chat is viable when
        no Anthropic credentials are configured."""
        return bool(self._use_local())

    # --- local-first tier ----------------------------------------------------
    def _use_local(self) -> bool:
        """Availability of the local tier. A positive probe is cached for the
        process (a later failure escalates as local_error and re-marks it); a
        NEGATIVE probe is only cached for local_probe_ttl_s — the old
        cache-forever behavior meant Ollama starting a beat after Mnemos
        pinned every call of the session to the paid parent."""
        ttl = float(getattr(_text_cfg(), "local_probe_ttl_s", 60) or 0)
        stale = (self._local_ok is False
                 and time.time() - self._local_probe_t >= ttl)
        if self._local_ok is None or stale:
            local = self._ensure_local()
            try:
                self._local_ok = bool(local.available())
            except Exception:
                self._local_ok = False
            self._local_probe_t = time.time()
            if not self._local_ok and not self._warned:
                print(f"[model_router] local text model '{local.model}' not "
                      f"reachable at {local.url}; using Claude for now. Enable "
                      f"local text with: ollama pull {local.model}")
                self._warned = True
        return self._local_ok

    def _mark_local_down(self) -> None:
        """Half-open breaker: after a confirmed-dead local error, skip the
        local attempt (and its full timeout wait) until the probe TTL expires."""
        self._local_ok = False
        self._local_probe_t = time.time()

    def _distill(self, *, task: str, reason: str, parent: dict,
                 local: dict | None = None, parent_model: str | None = None,
                 local_error: str | None = None, messages: list | None = None,
                 system: str | None = None, fewshot_n: int = 0,
                 schema: dict | None = None, fewshot_top_sim: float | None = None,
                 conf_effective: float | None = None,
                 retrieval: dict | None = None,
                 reasons: list[str] | None = None,
                 parent_sim: float | None = None) -> str | None:
        """Persist a local→parent pair for later idle distillation (best-effort).

        `prompt_head` stays truncated for browsing; with full-fidelity on, the
        row also stores the untruncated system + messages (the exact training
        input — always the CLEAN prompt, never the few-shot-augmented one) and
        `parent` arrives uncapped. `fewshot_n` records how many retrieved
        examples the failed local attempt had, so few-shot lift is analyzable.
        Returns the new row's `id` (for chat verdict wiring) or None on skip."""
        try:
            from app.services.escalate_log import escalate_log

            meta: dict[str, Any] = {}
            head = _prompt_head(messages)
            if head:
                meta["prompt_head"] = head
            if fewshot_n:
                meta["fewshot_n"] = fewshot_n
            if fewshot_top_sim is not None:
                meta["fewshot_top_sim"] = round(fewshot_top_sim, 4)
            if conf_effective is not None:
                meta["conf_effective"] = round(conf_effective, 4)
            if reasons and len(reasons) > 1:
                # The primary `reason` is first-match by precedence; a call
                # can trip several gates at once, and the old single label
                # undercounted everything below the first match in the
                # /console/escalate histogram.
                meta["reasons"] = list(reasons)
            if parent_sim is not None:
                # Embedding cosine local-answer vs parent-answer: the direct
                # "did escalating actually change the answer?" signal nothing
                # else records. High sim = the escalation bought little.
                meta["parent_sim"] = round(parent_sim, 4)
            if retrieval:
                # D.2b router features, measured at CALL time. Replaying them
                # later would give different numbers as the memory store grows,
                # so this is the only place they can honestly be recorded.
                meta["retrieval"] = retrieval
            if settings.escalate_log.full_fidelity:
                from app.services.ollama_text import _flatten
                if system:
                    meta["system"] = system
                if messages:
                    meta["messages"] = [
                        {"role": m.get("role", "user"),
                         "text": _flatten(m.get("content"))}
                        for m in messages
                    ]
                if schema is not None:
                    meta["schema"] = schema   # lets the eval harness replay the call
            row = escalate_log.record(
                task=task,
                reason=reason,
                local=local,
                parent=parent,
                local_model=_text_cfg().local_model,
                parent_model=parent_model,
                source="model_router",
                modality="text",
                local_error=local_error,
                meta=meta or None,
            )
            return (row or {}).get("id")
        except Exception as exc:  # pragma: no cover
            print(f"[model_router] escalate distill skipped ({exc}).")
            return None

    def _local_first(self, task: str, *, system: str, messages: list,
                     max_tokens: int, schema: dict | None,
                     model: str | None,
                     speculative: bool = False,
                     retrieval: dict | None = None
                     ) -> tuple[str, dict | None, str | None]:
        """Local pass -> confidence/stakes gate -> Claude parent when needed.
        Returns (text, parsed_json_or_None, distill_row_id_or_None). Mirrors
        VLMRouter.describe: fail open to Claude when local is down; keep the
        local answer when the parent call itself fails."""
        cfg = _text_cfg()
        parent_model = model or self.model_for(task)
        full = settings.escalate_log.full_fidelity
        out_cap = 10 ** 9 if full else _DISTILL_OUTPUT_CHARS
        fewshot_n = 0

        def _parent(reason: str, local: dict | None = None,
                    local_error: str | None = None,
                    fewshot_top_sim: float | None = None,
                    conf_effective: float | None = None,
                    reasons: list[str] | None = None
                    ) -> tuple[str, dict | None, str | None]:
            # Tier by reason (the same split vlm.py applies to frames): an
            # infra-driven escalation on an ambient task was work the LOCAL
            # model was supposed to do — it needs *a* decent answer, not the
            # accurate tier. User-initiated tasks, high-stakes tasks and
            # explicit model= overrides keep their configured parent.
            use_model = parent_model
            if (model is None and task not in _USER_TASKS
                    and task not in cfg.high_stakes_tasks
                    and reason in getattr(cfg, "cheap_parent_reasons", ())):
                use_model = (getattr(cfg, "cheap_parent_model", "")
                             or parent_model)
            text = self._complete_claude(task, system=system, messages=messages,
                                         max_tokens=max_tokens, schema=schema,
                                         model=use_model)
            parsed = None
            if schema is not None:
                try:
                    parsed = json.loads(text or "{}")
                except Exception:
                    parsed = {}
            rid = self._distill(task=task, reason=reason, local=local,
                                parent={"text": _clip(text, out_cap)},
                                parent_model=use_model,
                                retrieval=retrieval,
                                local_error=local_error,
                                messages=messages, system=system,
                                fewshot_n=fewshot_n, schema=schema,
                                fewshot_top_sim=fewshot_top_sim,
                                conf_effective=conf_effective,
                                reasons=reasons,
                                parent_sim=_answer_sim(
                                    (local or {}).get("text"), text))
            return text, parsed, rid

        if not self._use_local():
            if speculative:
                # Falling back to Claude is the correct behavior for demand
                # traffic and exactly the wrong one here: nobody asked for
                # this answer. Return nothing and let the cache stay cold.
                return "", None, None
            return _parent("local_unavailable")

        # Too much context for the small model to actually read. It would still
        # answer — fluently, from whatever it held — and self-report high
        # confidence, so the post-hoc gates below never fire. An attached
        # document is the common case: the file plus the grounding block runs
        # far past what a 7B holds, and the answer comes back about the
        # memories instead of the file.
        max_prompt = int(getattr(cfg, "local_max_prompt_chars", 0) or 0)
        if max_prompt > 0:
            n_chars = _prompt_chars(system, messages)
            if n_chars > max_prompt:
                if speculative:
                    return "", None, None
                print(f"[model_router] prompt {n_chars} chars > "
                      f"{max_prompt} — skipping local, asking Claude.")
                return _parent("prompt_too_long_for_local")

        # Retrieval few-shot: show the local model similar past prompts it
        # needed rescuing on, with the human-verified answer. Local-only —
        # the parent prompt and the stored training input stay clean.
        # Workstream C: the exemplar store (curated learning_pairs, LanceDB)
        # is tried first; the legacy distill-trail recall remains the fallback
        # so behavior is byte-identical until QUILL_EXEMPLARS=1.
        examples: list[dict] = []
        try:
            from app.services import exemplar_store
            examples = exemplar_store.router_examples(task, messages, cfg)
        except Exception as exc:
            examples = []
            print(f"[model_router] exemplar recall skipped ({exc}).")
        if not examples:
            try:
                from app.services.few_shot import few_shot
                examples = few_shot.examples(task, messages,
                                             k=cfg.fewshot_k,
                                             min_sim=cfg.fewshot_min_sim)
            except Exception as exc:
                examples = []
                print(f"[model_router] few-shot recall skipped ({exc}).")
        exemplar_block = ""
        if examples:
            from app.services.few_shot import few_shot
            fewshot_n = len(examples)
            exemplar_block = few_shot.render(
                examples, confidence_line=schema is None)

        def _local_call():
            # Phase 1.2: the exemplar block goes in as its own argument, not
            # concatenated onto `system`, so ollama_text can put the static
            # prefix ahead of it and the prefix cache can actually hit.
            return self._ensure_local().complete(
                task, system=system, messages=messages,
                max_tokens=max_tokens, schema=schema,
                exemplars=exemplar_block)

        res = None
        try:
            res = _local_call()
        except Exception as exc:
            # Half-open breaker: a 3s probe decides between a transient fault
            # (retry once, free) and a dead daemon (mark local down so the
            # next calls skip straight to the parent instead of each paying
            # the full local timeout, and escalate this one now).
            err = exc
            alive = False
            try:
                alive = bool(self._ensure_local().available())
            except Exception:
                alive = False
            if alive:
                try:
                    res = _local_call()
                except Exception as exc2:
                    err = exc2
            else:
                self._mark_local_down()
            if res is None:
                if speculative:
                    print(f"[model_router] speculative local error ({err}); "
                          "dropped.")
                    return "", None, None
                print(f"[model_router] local text error ({err}); "
                      "falling back to Claude.")
                return _parent("local_error", local_error=str(err))

        conf = res.get("confidence")
        # Calibration (#6): retrieval evidence floors the miscalibrated
        # self-report — but only when the answer agrees with the verified
        # answer it matched. With no (consistent) examples this is exactly
        # the old gate: missing confidence still reads as "unsure".
        top_sim = (max(float(e.get("sim") or 0.0) for e in examples)
                   if examples else None)
        eff = effective_confidence(conf,
                                   evidence_examples(res.get("text"), examples),
                                   weight=cfg.fewshot_conf_weight)
        hard = task in cfg.high_stakes_tasks
        parse_fail = schema is not None and not res.get("parse_ok")
        unsure = eff is None or float(eff) < _min_conf(cfg, task)
        suspect = (None if schema is not None else
                   suspect_answer(res.get("text") or "", messages))
        heuristic_escalates = bool(hard or parse_fail or unsure or suspect)
        # A generation that hit num_predict (even after ollama_text's wider
        # retry) is a token-budget problem, not a judgement problem — label it
        # honestly instead of burying it under low_confidence/parse_failure.
        truncated = bool(res.get("truncated"))
        empty = not (res.get("text") or "").strip()
        reason = ("high_stakes_task" if hard else
                  ("local_truncated" if truncated else "parse_failure")
                  if parse_fail else
                  suspect if (suspect and not unsure) else
                  ("local_truncated" if (truncated and empty)
                   else "low_confidence") if unsure else None)
        # Every gate that fired, not just the first match — the single-label
        # histogram undercounted everything below the winning branch.
        reasons_all: list[str] = []
        if hard:
            reasons_all.append("high_stakes_task")
        if parse_fail:
            reasons_all.append(
                "local_truncated" if truncated else "parse_failure")
        if suspect:
            reasons_all.append(suspect)
        if unsure:
            lbl = ("local_truncated" if (truncated and empty)
                   else "low_confidence")
            if lbl not in reasons_all:
                reasons_all.append(lbl)
        if reason in reasons_all:
            reasons_all.remove(reason)
            reasons_all.insert(0, reason)
        # Workstream D: consult the trained escalation router. In shadow mode
        # this only LOGS its decision next to the heuristic's; in active mode
        # it augments the CONFIDENCE gate only — the hard safety gates
        # (high-stakes task, parse failure, suspect answer) always escalate.
        should_escalate = heuristic_escalates
        shadow_priority = False
        try:
            from app.services.escalation_router import escalation_router
            from app.services.escalation_router import mode as _router_mode
            if _router_mode() != "off":
                d = escalation_router.decide(
                    task, messages,
                    (float(eff) if eff is not None else conf),
                    heuristic_escalates=heuristic_escalates,
                    heuristic_reason=reason,
                    retrieval=retrieval)
                if _router_mode() == "active":
                    soft_only = (not heuristic_escalates
                                 or reason == "low_confidence")
                    if soft_only:
                        should_escalate = bool(d["escalate"])
                        shadow_priority = bool(d.get("shadow_priority"))
                        if should_escalate and not heuristic_escalates:
                            reason = "router_predicted_failure"
        except Exception as exc:  # pragma: no cover
            print(f"[model_router] router consult skipped ({exc}).")
        keep_reason = "local_kept"
        if speculative:
            # The ladder stops here. A low-confidence speculative answer is
            # simply not cached; it is never worth a paid call, because the
            # user may never ask the question.
            should_escalate = False
            keep_reason = "speculative_local_only"
        if should_escalate:
            local_payload = {
                "text": _clip(res.get("text"), out_cap),
                "json": res.get("json"),
                "confidence": conf,
            }
            try:
                return _parent(reason or "low_confidence", local=local_payload,
                               fewshot_top_sim=top_sim, conf_effective=eff,
                               reasons=reasons_all)
            except Exception as exc:
                print(f"[model_router] escalation to Claude failed ({exc}); "
                      "keeping local.")
                keep_reason = "parent_failed"
        # The local answer stands. Workstream B: log the kept output to the
        # shadow-eval sample pool — the confident-but-wrong quadrant never
        # escalates, so this log is the only place it can be re-graded from.
        try:
            from app.services import shadow_eval
            shadow_eval.log_local_output(
                task, messages=messages, text=res.get("text"),
                confidence=(float(eff) if eff is not None else conf),
                model_tag=cfg.local_model, shadow_priority=shadow_priority,
                retrieval=retrieval)
        except Exception as exc:  # pragma: no cover
            print(f"[model_router] shadow log skipped ({exc}).")
        # Verdict-able tasks still get a distill row (no parent side) so the
        # UI can put 👍/👎/✏️ on EVERY answer, not just escalated ones.
        # Full-fidelity like any other row — replayable by the bench and
        # trainable once a human verdict lands.
        rid = None
        if task in _VERDICT_TASKS and schema is None:
            rid = self._distill(
                task=task, reason=keep_reason,
                local={"text": _clip(res.get("text"), out_cap),
                       "json": res.get("json"), "confidence": conf},
                parent={}, parent_model="(not called)",
                messages=messages, system=system, fewshot_n=fewshot_n,
                schema=schema, fewshot_top_sim=top_sim, conf_effective=eff,
                retrieval=retrieval)
        return res.get("text") or "", res.get("json"), rid

    @staticmethod
    def _maybe_speculative(on: bool):
        """Enter the speculative scope only when asked. Returns a context
        manager either way so the call sites stay one shape."""
        from contextlib import nullcontext
        from app.services.model_log import speculative_scope
        return speculative_scope() if on else nullcontext()

    @property
    def last_distill_id(self) -> str | None:
        """Distill row id from the most recent `complete`/`complete_json` on
        this thread, or None when no row was written (local-first off, or a
        kept answer on a task outside _VERDICT_TASKS)."""
        return getattr(_tls, "distill_id", None)

    # --- public API ----------------------------------------------------------
    def complete(self, task: str, *, system: str, messages: list,
                 max_tokens: int = 1024, schema: dict | None = None,
                 model: str | None = None, speculative: bool = False,
                 retrieval: dict | None = None) -> str:
        """One text completion, local-first when QUILL_TEXT_LOCAL=1 (else
        Claude-only, unchanged). Returns the reply text. When an escalation
        distill row is written, its id is also available as
        `router.last_distill_id` on this thread (for chat verdict wiring).

        `speculative=True` (latency program, Phase 3.3) marks work the user did
        not ask for — pre-generated answers to predicted questions. Such calls
        are local-only: no escalation, no fallback to Claude when the local
        model is down or unsure, and the paid seam raises if one ever gets
        that far. Returns "" when no local answer could be produced.

        `retrieval` (D.2b) is router_train.retrieval_stats() for the grounding
        this call was built on — STATS only, never retrieved content. Callers
        that ground a prompt should pass it; the router reads it as a feature
        and it is recorded on the distill/shadow rows so training sees the
        same numbers production saw. Omitting it is always safe.
        """
        with self._maybe_speculative(speculative):
            if not _text_cfg().enabled:
                _tls.distill_id = None
                if speculative:
                    return ""       # Claude-only mode has no free tier to use
                return self._complete_claude(
                    task, system=system, messages=messages,
                    max_tokens=max_tokens, schema=schema, model=model)
            text, _, distill_id = self._local_first(
                task, system=system, messages=messages,
                max_tokens=max_tokens, schema=schema, model=model,
                speculative=speculative, retrieval=retrieval)
            _tls.distill_id = distill_id
            return text

    def complete_json(self, task: str, *, system: str, messages: list,
                      schema: dict, max_tokens: int = 1024,
                      model: str | None = None,
                      speculative: bool = False,
                      retrieval: dict | None = None) -> dict:
        """`complete` + parse the JSON result (schema-enforced). A local parse
        failure is an escalate trigger; parse failure at the parent degrades to
        {} exactly as before."""
        with self._maybe_speculative(speculative):
            if not _text_cfg().enabled:
                _tls.distill_id = None
                if speculative:
                    return {}
                text = self._complete_claude(
                    task, system=system, messages=messages,
                    max_tokens=max_tokens, schema=schema, model=model)
                try:
                    return json.loads(text or "{}")
                except Exception:
                    return {}
            _, parsed, distill_id = self._local_first(
                task, system=system, messages=messages,
                max_tokens=max_tokens, schema=schema, model=model,
                speculative=speculative, retrieval=retrieval)
            _tls.distill_id = distill_id
            return parsed or {}


router = ModelRouter()
