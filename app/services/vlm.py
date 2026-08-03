"""Vision-language model client — turns a captured frame into structured memory.

Tiered, local-first: every selected webcam frame is described by a local VLM
(Ollama, e.g. llama3.2-vision on the GPU) for free; Claude (Opus 4.8) is the
*paid fallback*, invoked only when the frame is high-stakes (an actionable page —
todo_list / form / code) or the local model reports low confidence. Plain scenes
("a person at a desk") never leave the machine.

Both providers return the same structured shape (`_SCHEMA`) that becomes a VISION
Event, so the router is a drop-in behind the original `vlm.describe(jpeg)` call —
`vision.py` doesn't change. If the local model is unreachable, the router falls
back to Claude automatically, so this is safe to run before the model is pulled.

Auth: the Anthropic() client resolves credentials from the environment
(ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN) or an `ant auth login` profile.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from typing import Any

from app.config import settings


def _log(task, provider, model, latency_s, **kw) -> None:
    """Record a model call; never let telemetry break the vision path."""
    try:
        from app.services.model_log import model_log
        model_log.log_call(task=task, provider=provider, model=model,
                           latency_s=latency_s, **kw)
    except Exception as exc:  # pragma: no cover
        print(f"[vlm] telemetry skipped ({exc}).")

# Structured shape we ask BOTH models to return for each frame.
_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "description": "One or two sentences describing the scene."},
        "ocr_text": {"type": "string", "description": "ALL legible text on the page/board, verbatim, preserving line order. Empty string if none."},
        "people_count": {"type": "integer", "description": "Number of distinct people visible."},
        "objects": {"type": "array", "items": {"type": "string"}, "description": "Salient objects/entities (whiteboard, laptop, notebook, slide, document, ...)."},
        "scene_type": {"type": "string", "description": "Short label: meeting, desk, whiteboard, presentation, document, outdoors, ..."},
        # --- page understanding: when a page/notebook/board of content is shown ---
        "content_type": {
            "type": "string",
            "enum": ["notes", "todo_list", "questions", "diagram", "table",
                     "calculation", "form", "code", "mixed", "none"],
            "description": "What KIND of content the page is. 'none' if no page of "
            "text/content is shown (e.g. just a person or a room). 'mixed' if it "
            "genuinely combines several kinds.",
        },
        "title": {"type": "string", "description": "The page's heading/title if one is written, else empty string."},
        "items": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The discrete entries on the page as clean strings — one per "
            "to-do item, question, table row, or note bullet. Empty for diagrams/images "
            "or when there are no discrete items.",
        },
        "item_confidences": {
            "type": "array",
            "items": {"type": "number"},
            "description": "Per-ITEM transcription confidence 0.0-1.0, SAME ORDER and "
            "SAME LENGTH as `items` — how sure you are of EACH entry individually. "
            "A crisp line is ~0.95; a smudged/half-guessed one is <0.5. Empty if no items.",
        },
        "confidence": {
            "type": "number",
            "description": "0.0-1.0: how sure you are of the text extraction and "
            "content_type classification. Use LOW values (< 0.6) for messy or "
            "ambiguous handwriting, blur, glare, or a partially-visible page.",
        },
    },
    "required": ["description", "ocr_text", "people_count", "objects", "scene_type",
                 "content_type", "title", "items", "confidence"],
    "additionalProperties": False,
}


def align_item_confidences(res: dict) -> list[float]:
    """Return a per-item confidence list aligned 1:1 with res['items'].

    Item-level confidence (#6): a page's items rarely read equally clearly — one
    line is crisp, the next is smudged. The model reports `item_confidences`, but
    weaker local models may omit it or return a wrong-length list. We normalize:
    pad short / truncate long against the overall `confidence`, clamp to 0..1, so
    every item always has a trustworthy per-item number downstream can gate on."""
    items = res.get("items") or []
    overall = res.get("confidence")
    fallback = float(overall) if isinstance(overall, (int, float)) else 0.5
    raw = res.get("item_confidences") or []
    out: list[float] = []
    for i in range(len(items)):
        v = raw[i] if i < len(raw) and isinstance(raw[i], (int, float)) else fallback
        out.append(round(max(0.0, min(1.0, float(v))), 4))
    return out

_SYSTEM = (
    "You are vinceo.ai's vision service. You receive a single still frame from a "
    "laptop webcam during a meeting or work session. Extract what matters for a "
    "personal memory timeline: what's happening, who/what is present, and any "
    "readable text. Be concise and factual; do not speculate about identities.\n\n"
    "When the frame shows a PAGE OF CONTENT (a notebook page, whiteboard, sheet, "
    "or screen of text), also DETERMINE WHAT THE PAGE IS and set content_type:\n"
    "  - todo_list : action items / checkboxes / things to do\n"
    "  - questions : a list of questions to answer or ask\n"
    "  - notes     : general written notes, bullets, or prose\n"
    "  - table     : rows/columns of structured data\n"
    "  - calculation: math/arithmetic/worked equations\n"
    "  - form      : labeled fields to fill in\n"
    "  - code      : source code or pseudocode\n"
    "  - diagram   : a drawing, chart, sketch, or image with little text\n"
    "  - mixed     : genuinely several of the above together\n"
    "  - none      : no page of content (just a person, a room, an object)\n"
    "Transcribe the discrete entries into `items` (one to-do/question/row/bullet "
    "per string), and put any heading in `title`. Read handwriting carefully. "
    "Set `confidence` honestly — low when the text is hard to read."
)

_USER = "Extract the structured summary of this frame as JSON matching the schema."

# Actionable pages: always verified by Claude, because acting on them (dispatching
# a to-do, filling a form) needs accurate transcription, not a cheap first guess.
_HARD_TYPES = {"todo_list", "form", "code"}

# Verbatim handwriting OCR — a different job from scene classification (_SYSTEM).
# Used by the notebook pipeline, where the exact recipient/body matters.
_TRANSCRIBE_SYSTEM = (
    "You are a precise handwriting OCR engine. Transcribe ALL text in the image "
    "exactly as written — preserve line breaks, spelling, capitalization, and "
    "punctuation. Do NOT correct, summarize, translate, interpret, or add "
    "anything. If a word is genuinely unclear, give your single best reading. "
    "Output ONLY the transcribed text."
)


def _parse_json(text: str) -> dict[str, Any]:
    """Best-effort parse of a model's JSON reply (tolerates ```json fences)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    try:
        return json.loads(t)
    except Exception:
        start, end = t.find("{"), t.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(t[start:end + 1])
            except Exception:
                pass
    # Unparseable -> force escalation by reporting zero confidence on an unknown page.
    return {"description": t[:200], "ocr_text": "", "people_count": 0, "objects": [],
            "scene_type": "", "content_type": "none", "title": "", "items": [],
            "item_confidences": [], "confidence": 0.0}


class ClaudeVLM:
    """A paid Claude vision reader. The router keeps two: the accurate model
    (settings.vision.model) for high-stakes reads and the cheap fallback
    (settings.vision.fallback_model) for frames that just need *a* read."""

    def __init__(self, model: str | None = None) -> None:
        self._client = None
        self.model = model or settings.vision.model

    def _ensure(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def describe(self, jpeg_bytes: bytes) -> dict[str, Any]:
        client = self._ensure()
        b64 = base64.standard_b64encode(jpeg_bytes).decode("utf-8")
        t0 = time.time()
        resp = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg", "data": b64,
                    }},
                    {"type": "text", "text": _USER},
                ],
            }],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        u = getattr(resp, "usage", None)
        _log("vision", "claude", self.model, time.time() - t0, ok=True,
             input_tokens=getattr(u, "input_tokens", 0) or 0,
             output_tokens=getattr(u, "output_tokens", 0) or 0,
             input_bytes=len(jpeg_bytes))
        text = next((b.text for b in resp.content if b.type == "text"), "{}")
        return _parse_json(text)

    def transcribe(self, jpeg_bytes: bytes) -> str:
        """Verbatim OCR of the image's text (handwriting). Returns raw text."""
        client = self._ensure()
        b64 = base64.standard_b64encode(jpeg_bytes).decode("utf-8")
        t0 = time.time()
        resp = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=_TRANSCRIBE_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg", "data": b64,
                    }},
                    {"type": "text", "text": "Transcribe all text in this image, verbatim."},
                ],
            }],
        )
        u = getattr(resp, "usage", None)
        _log("vision", "claude", self.model, time.time() - t0, ok=True,
             input_tokens=getattr(u, "input_tokens", 0) or 0,
             output_tokens=getattr(u, "output_tokens", 0) or 0,
             input_bytes=len(jpeg_bytes))
        return next((b.text for b in resp.content if b.type == "text"), "").strip()


class OllamaVLM:
    """The free local first-pass — a vision model served by Ollama on the GPU."""

    def __init__(self, model: str | None = None) -> None:
        self.url = settings.vision.ollama_url.rstrip("/")
        self.model = model or settings.vision.local_model
        self.timeout = settings.vision.local_timeout_s

    def available(self) -> bool:
        """True if Ollama is up and the configured model is present."""
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=3) as r:
                tags = json.load(r)
        except Exception:
            return False
        names = {m.get("name", "") for m in tags.get("models", [])}
        base = self.model.split(":")[0]
        return any(n == self.model or n.split(":")[0] == base for n in names)

    def describe(self, jpeg_bytes: bytes) -> dict[str, Any]:
        b64 = base64.standard_b64encode(jpeg_bytes).decode("utf-8")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _USER, "images": [b64]},
            ],
            "stream": False,
            "format": _SCHEMA,          # Ollama structured output — enforce the schema
            "options": {"temperature": 0},
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url + "/api/chat", data=data,
                                     headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            out = json.load(r)
        # Local model — free; still logged for latency + token throughput.
        _log("vision", "ollama", self.model, time.time() - t0, ok=True,
             input_tokens=out.get("prompt_eval_count", 0) or 0,
             output_tokens=out.get("eval_count", 0) or 0,
             input_bytes=len(jpeg_bytes), cost_usd=0.0)
        content = (out.get("message") or {}).get("content", "{}")
        return _parse_json(content)


class VLMRouter:
    """Local-first with a Claude fallback. `describe()` is the drop-in used by
    the vision pipeline; it decides per-frame whether Claude is worth a call."""

    def __init__(self) -> None:
        self.local = OllamaVLM()
        self.claude = ClaudeVLM()                                   # accurate tier
        self.claude_lite = ClaudeVLM(settings.vision.fallback_model)  # cheap tier
        self._local_ok: bool | None = None
        self._warned = False
        self._local_cool_until: float = 0.0

    def _use_local(self) -> bool:
        if not settings.vision.local_vlm:
            return False
        if time.time() < self._local_cool_until:
            return False
        if self._local_ok is None:               # probe once, cache
            self._local_ok = self.local.available()
            if not self._local_ok and not self._warned:
                print(f"[vlm] local model '{self.local.model}' not reachable at "
                      f"{self.local.url}; using Claude for now. Enable local vision "
                      f"with: ollama pull {self.local.model}")
                self._warned = True
        return bool(self._local_ok)

    def _trip_local_cooldown(self, exc: Exception) -> None:
        cool = float(getattr(settings.vision, "local_cooldown_s", 120) or 0)
        if cool <= 0:
            return
        self._local_cool_until = time.time() + cool
        print(f"[vlm] local VLM cooling down {cool:.0f}s after error ({exc}).")

    @staticmethod
    def _budget_ok() -> bool:
        """Hard USD/day cap on ambient cloud vision (SECURITY #2). Checked
        BEFORE every cloud call; fails CLOSED — an unmetered cloud call is
        worse than a skipped frame (the stream retries soon anyway)."""
        try:
            from app.perception.spend_cap import spend_cap
            return spend_cap.allow("vision")
        except Exception as exc:
            print(f"[vlm] spend cap unavailable ({exc}); skipping cloud call.")
            return False

    @staticmethod
    def _tag(res: dict, provider: str, route: dict | None) -> dict:
        # Chokepoint: every result this router hands out — local or parent —
        # is redacted here, so downstream storage (events, facts, screen
        # extract) never sees a raw credential even when the frame reached
        # the cloud before we could know what was on it.
        from app.services import redact

        res = redact.redact_payload(dict(res or {}))
        res["_provider"] = provider          # 'ollama' or 'claude' — shown in the timeline
        if route:
            res["_route"] = route
        return res

    def _distill(self, *, reason: str, parent: dict, local: dict | None = None,
                 capture_quality: float | None = None,
                 local_error: str | None = None,
                 context: dict | None = None,
                 parent_model: str | None = None) -> None:
        """Persist a local→parent pair for later idle distillation (best-effort)."""
        try:
            from app.services.escalate_log import escalate_log

            ctx = context or {}
            escalate_log.record(
                task="vision.describe",
                reason=reason,
                local=local,
                parent=parent,
                local_model=self.local.model,
                parent_model=parent_model or self.claude.model,
                capture_quality=capture_quality,
                frame_path=ctx.get("frame_path"),
                source=ctx.get("source"),
                modality=ctx.get("modality") or "vision",
                local_error=local_error,
            )
        except Exception as exc:  # pragma: no cover
            print(f"[vlm] escalate distill skipped ({exc}).")

    def describe(self, jpeg_bytes: bytes, *,
                 capture_quality: float | None = None,
                 escalate: bool = True,
                 context: dict | None = None) -> dict[str, Any]:
        """Two-pass, capture-aware. Pass 1 is the free local VLM; pass 2 (Claude)
        fires when the page is high-stakes (todo_list/form/code), the local model
        is unsure, OR the frame's own capture is marginal on a content page — a
        soft/dim capture makes local OCR untrustworthy no matter how confident the
        model sounds, so it's exactly when the accurate reader earns its cost.
        `capture_quality` is the #6 frame_quality facet (0..1); None = skip that test.

        `escalate=False` keeps the call local-only (or empty on local failure) —
        used for cheap optional paths like click crops that must never burn Claude.

        `context` is optional distill metadata (frame_path, source, modality) —
        never includes image bytes.
        """
        cooling = time.time() < self._local_cool_until
        if cooling or not self._use_local():
            reason = "local_cooldown" if cooling else (
                "local_unreachable" if settings.vision.local_vlm
                else "local_disabled")
            # Local was *supposed* to handle this frame; if the user opted out
            # of paying for local outages, skip it — the stream retries soon.
            # (local_disabled = user chose Claude-only; that still escalates.)
            if reason != "local_disabled" and \
                    not settings.vision.cloud_when_local_down:
                return self._tag(
                    {"description": "", "ocr_text": "", "people_count": 0,
                     "objects": [], "scene_type": "", "content_type": "none",
                     "title": "", "items": [], "item_confidences": [],
                     "confidence": 0.0},
                    "none", {"reason": reason + "_cloud_off"})
            if not escalate:
                return self._tag(
                    {"description": "", "ocr_text": "", "people_count": 0,
                     "objects": [], "scene_type": "", "content_type": "none",
                     "title": "", "items": [], "item_confidences": [],
                     "confidence": 0.0},
                    "none", {"reason": reason + "_no_escalate"})
            # Local is down/cooling — these frames just need a decent read, not
            # the accurate reader. The cheap tier covers them.
            if not self._budget_ok():
                return self._tag(
                    {"description": "", "ocr_text": "", "people_count": 0,
                     "objects": [], "scene_type": "", "content_type": "none",
                     "title": "", "items": [], "item_confidences": [],
                     "confidence": 0.0},
                    "none", {"reason": reason + "_budget_exhausted"})
            parent = self.claude_lite.describe(jpeg_bytes)
            self._distill(reason=reason, parent=parent, local=None,
                          capture_quality=capture_quality, context=context,
                          parent_model=self.claude_lite.model)
            return self._tag(parent, "claude", {"reason": reason})

        try:
            local = self.local.describe(jpeg_bytes)
        except Exception as exc:
            self._trip_local_cooldown(exc)
            if not escalate:
                print(f"[vlm] local VLM error ({exc}); no escalate — skipping.")
                return self._tag(
                    {"description": "", "ocr_text": "", "people_count": 0,
                     "objects": [], "scene_type": "", "content_type": "none",
                     "title": "", "items": [], "item_confidences": [],
                     "confidence": 0.0},
                    "none", {"reason": "local_error_no_escalate"})
            if not settings.vision.cloud_when_local_down:
                print(f"[vlm] local VLM error ({exc}); cloud-on-outage off — "
                      "skipping frame.")
                return self._tag(
                    {"description": "", "ocr_text": "", "people_count": 0,
                     "objects": [], "scene_type": "", "content_type": "none",
                     "title": "", "items": [], "item_confidences": [],
                     "confidence": 0.0},
                    "none", {"reason": "local_error_cloud_off"})
            if not self._budget_ok():
                print(f"[vlm] local VLM error ({exc}); cloud budget exhausted "
                      "— skipping frame.")
                return self._tag(
                    {"description": "", "ocr_text": "", "people_count": 0,
                     "objects": [], "scene_type": "", "content_type": "none",
                     "title": "", "items": [], "item_confidences": [],
                     "confidence": 0.0},
                    "none", {"reason": "local_error_budget_exhausted"})
            print(f"[vlm] local VLM error ({exc}); falling back to Claude.")
            parent = self.claude_lite.describe(jpeg_bytes)
            self._distill(reason="local_error", parent=parent, local=None,
                          capture_quality=capture_quality, local_error=str(exc),
                          context=context, parent_model=self.claude_lite.model)
            return self._tag(parent, "claude", {"reason": "local_error"})

        conf = float(local.get("confidence", 1.0) or 0.0)
        ctype = local.get("content_type", "none")

        # Secret-shaped local read: the frame is showing credentials/PII, so
        # the image must NOT go to the cloud no matter how high-stakes the
        # page looks. No escalation, no distill row — return the local read
        # (redacted by _tag) and nothing leaves the machine for this frame.
        from app.services import redact
        secret_kinds = redact.scan_payload(local)
        if secret_kinds:
            print(f"[vlm] secret-shaped OCR ({', '.join(secret_kinds)}); "
                  "cloud escalation skipped.")
            return self._tag(local, "ollama", {
                "reason": "secret_detected", "secret_kinds": secret_kinds,
                "confidence": conf})

        hard = ctype in _HARD_TYPES
        unsure = ctype != "none" and conf < settings.vision.escalate_min_conf
        weak_capture = (ctype != "none" and capture_quality is not None
                        and capture_quality < settings.vision.escalate_min_capture)
        if escalate and (hard or unsure or weak_capture) \
                and not self._budget_ok():
            # Budget spent: the local read stands. Degraded, never unmetered.
            return self._tag(local, "ollama", {
                "reason": "budget_exhausted", "local_content_type": ctype,
                "confidence": conf})
        if escalate and (hard or unsure or weak_capture):
            try:
                # High-stakes pages (todo/form/code) and weak captures earn the
                # accurate reader; a merely-unsure local read only needs the
                # cheap tier's second opinion.
                reader = self.claude if (hard or weak_capture) else self.claude_lite
                parent = reader.describe(jpeg_bytes)
                reason = ("hard_type" if hard else
                          "low_confidence" if unsure else "weak_capture")
                self._distill(reason=reason, parent=parent, local=local,
                              capture_quality=capture_quality, context=context,
                              parent_model=reader.model)
                return self._tag(parent, "claude", {
                    "reason": reason, "local_content_type": ctype,
                    "local_confidence": conf, "capture_quality": capture_quality})
            except Exception as exc:
                print(f"[vlm] escalation to Claude failed ({exc}); keeping local.")
        return self._tag(local, "ollama", {"confidence": conf})

    def transcribe(self, jpeg_bytes: bytes) -> str:
        """Verbatim OCR — always Claude. Only called on a deliberate scan (a
        notebook page), where recipient/body accuracy matters more than cost."""
        return self.claude.transcribe(jpeg_bytes)


vlm = VLMRouter()
