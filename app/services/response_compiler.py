"""Deterministic response compiler — semantic AST for chat replies.

Separates reasoning (plain model text) from presentation. The frontend
renders `compiled.sections` into editorial UI; `text` remains the fallback.

No extra LLM calls. Cacheable. Idempotent for the same input.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

VERSION = 1

# Soft cap for body paragraphs (~3–4 visual lines at ~70 chars).
_MAX_PARA_CHARS = 280

_TRANSITION = re.compile(
    r"(?<=[.!?])\s+(?=(?:Therefore|However|For example|In summary|"
    r"In other words|Meanwhile|Consequently|By contrast|Next|"
    r"The key idea|Think of|Importantly|Note that|Finally)\b)",
    re.I,
)

_HEADING = re.compile(r"^(#{1,3})\s+(.+)$")
_BULLET = re.compile(r"^(\s*)([-*•]|\d+[.)])\s+(.+)$")
_CALLOUT = re.compile(
    r"^(?:\*\*)?(Key idea|Key concept|Definition|Example|Common mistake|"
    r"Warning|Caution|Note|Summary|Takeaway|Next steps?)(?:\*\*)?\s*[:—–-]\s*(.*)$",
    re.I,
)
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_MD_CODE_FENCE = re.compile(r"```(\w*)\n(.*?)```", re.S)
_DISPLAY_TEX = re.compile(
    r"(?:\\\[([\s\S]*?)\\\])|(?:\$\$([\s\S]*?)\$\$)",
)
_INLINE_TEX = re.compile(r"(?:\\\((.+?)\\\))|(?:(?<!\$)\$(?!\$)(.+?)\$(?!\$))")
_FENCE = re.compile(
    r":::(\w+)\s*\n([\s\S]*?)(?:\n:::\s*|$)",
    re.I,
)

_EDU_HINT = re.compile(
    r"\b(integrat|derivativ|equation|formula|theorem|definition|velocity|"
    r"acceleration|proof|homework|lecture|quiz|concept|example|"
    r"step[- ]by[- ]step|solve|calculate)\b",
    re.I,
)

_ACTION_CATALOG = (
    ("step_by_step", "Walk through step-by-step",
     "Walk me through this step-by-step."),
    ("visual", "Show visual explanation",
     "Show a visual or intuitive explanation of this."),
    ("another_example", "Try another example",
     "Give me another example of this."),
    ("practice", "Generate practice problem",
     "Generate a practice problem so I can try this."),
    ("intuitive", "Explain intuitively",
     "Explain this more intuitively, without jargon."),
)

# Terms we may emphasize when they appear as whole words (educational).
_EMPHASIS_CANDIDATES = re.compile(
    r"\b("
    r"integrat(?:e|ion|ing)|velocity|acceleration|derivative|integral|"
    r"constant of integration|theorem|hypothesis|definition|"
    r"momentum|force|energy|entropy|gradient|matrix|vector|"
    r"probability|distribution|mean|variance|"
    r"thesis|rubric|syllabus|deadline"
    r")\b",
    re.I,
)


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _strip_md_inline(s: str) -> str:
    s = _MD_BOLD.sub(r"\1", s)
    s = _MD_ITALIC.sub(r"\1", s)
    return s.strip()


def _emphasis_in(text: str, budget: int = 6) -> list[str]:
    """Pick a small set of educational terms to emphasize (~5–10% of words)."""
    words = re.findall(r"[A-Za-z][A-Za-z\-']+", text)
    if not words:
        return []
    max_n = max(1, min(budget, max(1, len(words) // 12)))
    seen: list[str] = []
    for m in _EMPHASIS_CANDIDATES.finditer(text):
        term = m.group(0)
        # Prefer title-cased canonical form for display consistency.
        key = term.lower()
        if any(s.lower() == key for s in seen):
            continue
        seen.append(term)
        if len(seen) >= max_n:
            break
    return seen


def _split_paragraphs(block: str) -> list[str]:
    """Split on blank lines, transitions, and length."""
    raw = [p.strip() for p in re.split(r"\n\s*\n", block) if p.strip()]
    out: list[str] = []
    for p in raw:
        # Transition splits
        parts = _TRANSITION.split(p)
        # re.split with capturing keeps separators; our pattern is lookaround so no.
        chunks: list[str] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if len(part) <= _MAX_PARA_CHARS:
                chunks.append(part)
                continue
            # Sentence-pack into ~_MAX_PARA_CHARS
            sentences = re.split(r"(?<=[.!?])\s+", part)
            buf = ""
            for sent in sentences:
                if not sent:
                    continue
                if buf and len(buf) + 1 + len(sent) > _MAX_PARA_CHARS:
                    chunks.append(buf.strip())
                    buf = sent
                else:
                    buf = (buf + " " + sent).strip() if buf else sent
            if buf:
                chunks.append(buf.strip())
        out.extend(chunks)
    return out


def _callout_type(label: str) -> str:
    key = label.lower().strip()
    return {
        "key idea": "key_idea",
        "key concept": "key_idea",
        "definition": "definition",
        "example": "example",
        "common mistake": "mistake",
        "warning": "warning",
        "caution": "warning",
        "note": "note",
        "summary": "summary",
        "takeaway": "takeaway",
        "next step": "next_actions",
        "next steps": "next_actions",
    }.get(key, "note")


def _parse_fences(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract :::type ... ::: blocks; return residual text + sections."""
    sections: list[dict[str, Any]] = []
    residual = text

    def _repl(m: re.Match) -> str:
        stype = m.group(1).lower().strip()
        body = m.group(2).strip()
        if stype in ("formula", "equation", "math"):
            sections.append({"type": "formula", "tex": body, "display": True})
        elif stype in ("list", "steps", "next_actions", "actions"):
            items = []
            for line in body.splitlines():
                bm = _BULLET.match(line.strip()) or re.match(r"^(.+)$", line.strip())
                if bm and line.strip():
                    items.append(_strip_md_inline(
                        bm.group(3) if bm.lastindex and bm.lastindex >= 3 else bm.group(1)
                    ))
            sections.append({
                "type": "next_actions" if stype in ("next_actions", "actions") else "list",
                "items": [i for i in items if i],
            })
        elif stype == "title":
            sections.append({"type": "title", "text": _strip_md_inline(body)})
        else:
            mapped = {
                "takeaway": "takeaway",
                "explanation": "explanation",
                "example": "example",
                "warning": "warning",
                "definition": "definition",
                "key_idea": "key_idea",
                "concept": "key_idea",
                "summary": "summary",
                "note": "note",
                "mistake": "mistake",
            }.get(stype, "explanation")
            sec: dict[str, Any] = {"type": mapped, "text": _strip_md_inline(body)}
            if mapped == "example":
                sec["title"] = "Example"
            sections.append(sec)
        return "\n"

    residual = _FENCE.sub(_repl, residual)
    return residual, sections


def _extract_code_fences(text: str) -> tuple[str, list[dict[str, Any]]]:
    sections: list[dict[str, Any]] = []

    def _repl(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        code = m.group(2).rstrip("\n")
        sections.append({"type": "code", "lang": lang, "text": code})
        return "\n"

    return _MD_CODE_FENCE.sub(_repl, text), sections


def _extract_display_math(text: str) -> tuple[str, list[dict[str, Any]]]:
    sections: list[dict[str, Any]] = []

    def _repl(m: re.Match) -> str:
        tex = (m.group(1) or m.group(2) or "").strip()
        if tex:
            sections.append({"type": "formula", "tex": tex, "display": True})
        return "\n"

    return _DISPLAY_TEX.sub(_repl, text), sections


def _inline_math_to_markers(text: str) -> str:
    """Leave inline math as $...$ markers for the frontend KaTeX pass."""

    def _repl(m: re.Match) -> str:
        tex = (m.group(1) or m.group(2) or "").strip()
        return f"${tex}$" if tex else ""

    return _INLINE_TEX.sub(_repl, text)


def _looks_short(text: str) -> bool:
    t = text.strip()
    if len(t) < 48:
        return True
    # Single short sentence
    if t.count("\n") == 0 and len(t) < 120 and t.count(". ") < 1:
        return True
    return False


def _is_educational(text: str, sections: list[dict]) -> bool:
    if any(s.get("type") in ("formula", "definition", "key_idea", "example")
           for s in sections):
        return True
    if _EDU_HINT.search(text):
        return True
    return False


def _default_actions() -> list[dict[str, str]]:
    return [
        {"id": a[0], "label": a[1], "prompt": a[2]}
        for a in _ACTION_CATALOG
    ]


def _looks_like_title(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 80:
        return False
    if s.endswith((".", "?", "!")):
        return False
    if "→" in s or "->" in s or "—" in s:
        return True
    # Short noun-phrase heading (few words, no clause verbs)
    words = s.split()
    if 1 <= len(words) <= 8 and not re.search(
        r"\b(is|are|was|were|be|been|being|have|has|had|do|does|did|"
        r"will|would|can|could|should|may|might|integrate|find|use)\b",
        s, re.I,
    ):
        return True
    return False


def _parse_linear(text: str) -> list[dict[str, Any]]:
    """Walk residual prose into typed sections."""
    sections: list[dict[str, Any]] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    list_buf: list[str] = []
    para_buf: list[str] = []
    saw_content = False

    def flush_list() -> None:
        nonlocal list_buf
        if list_buf:
            sections.append({"type": "list", "items": list_buf[:]})
            list_buf = []

    def flush_para() -> None:
        nonlocal para_buf, saw_content
        if not para_buf:
            return
        block = "\n".join(para_buf).strip()
        para_buf = []
        if not block:
            return
        # Leading title-like line alone
        if (not saw_content and "\n" not in block and _looks_like_title(block)
                and not sections):
            sections.append({"type": "title", "text": _strip_md_inline(block)})
            saw_content = True
            return
        for p in _split_paragraphs(block):
            p = _strip_md_inline(_inline_math_to_markers(p))
            if not p:
                continue
            if (not saw_content and not sections and _looks_like_title(p)):
                sections.append({"type": "title", "text": p})
                saw_content = True
                continue
            em = _emphasis_in(p)
            sec: dict[str, Any] = {"type": "explanation", "text": p}
            if em:
                sec["emphasis"] = em
            sections.append(sec)
            saw_content = True

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_list()
            flush_para()
            i += 1
            continue

        hm = _HEADING.match(stripped)
        if hm:
            flush_list()
            flush_para()
            level = len(hm.group(1))
            title = _strip_md_inline(hm.group(2))
            sections.append({
                "type": "title" if level <= 2 else "heading",
                "text": title,
                "level": level,
            })
            saw_content = True
            i += 1
            continue

        cm = _CALLOUT.match(stripped)
        if cm:
            flush_list()
            flush_para()
            stype = _callout_type(cm.group(1))
            rest = _strip_md_inline(cm.group(2) or "")
            body_lines = [rest] if rest else []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if not nxt.strip():
                    break
                if _CALLOUT.match(nxt.strip()) or _HEADING.match(nxt.strip()):
                    break
                if _BULLET.match(nxt) and stype == "next_actions":
                    break
                body_lines.append(nxt.strip())
                j += 1
            body = _strip_md_inline(" ".join(body_lines).strip())
            if stype == "next_actions":
                items = [body] if body else []
                while j < len(lines) and _BULLET.match(lines[j].strip() or ""):
                    bm = _BULLET.match(lines[j].strip())
                    if bm:
                        items.append(_strip_md_inline(bm.group(3)))
                    j += 1
                sections.append({"type": "next_actions", "items": items})
            elif stype == "example":
                sections.append({"type": "example", "title": "Example", "text": body})
            elif stype == "formula":
                sections.append({"type": "formula", "tex": body, "display": True})
            else:
                sections.append({"type": stype, "text": body})
            saw_content = True
            i = j
            continue

        bm = _BULLET.match(line)
        if bm:
            flush_para()
            list_buf.append(_strip_md_inline(bm.group(3)))
            saw_content = True
            i += 1
            continue

        flush_list()
        para_buf.append(stripped)
        i += 1

    flush_list()
    flush_para()
    return sections


def _promote_takeaway(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure a takeaway leads when the reply is multi-section and educational."""
    if not sections:
        return sections
    if any(s.get("type") == "takeaway" for s in sections):
        take = next(s for s in sections if s.get("type") == "takeaway")
        rest = [s for s in sections if s is not take]
        titles = [s for s in rest if s.get("type") == "title"]
        other = [s for s in rest if s.get("type") != "title"]
        return titles[:1] + [take] + titles[1:] + other

    explanations = [s for s in sections if s.get("type") == "explanation"]
    if len(sections) < 2 or not explanations:
        return sections
    # Prefer a full instructional sentence over a title fragment
    first = None
    for s in explanations:
        text = (s.get("text") or "").strip()
        if not text or len(text) > 180:
            continue
        if text.count(". ") > 1:
            continue
        if _looks_like_title(text):
            continue
        first = s
        break
    if first is None:
        return sections
    promoted = dict(first)
    promoted["type"] = "takeaway"
    out: list[dict[str, Any]] = []
    for s in sections:
        if s is first:
            out.append(promoted)
        else:
            out.append(s)
    titles = [s for s in out if s.get("type") == "title"]
    if titles:
        take = next(s for s in out if s.get("type") == "takeaway")
        rest = [s for s in out if s is not take and s.get("type") != "title"]
        return [titles[0]] + [take] + titles[1:] + rest
    return out


def _sources_summary(sources: list | None) -> dict[str, Any] | None:
    if not sources:
        return None
    total = 0
    for s in sources:
        try:
            total += int(s.get("n") or len(s.get("items") or []) or 0)
        except (TypeError, ValueError):
            total += len(s.get("items") or [])
    if total <= 0:
        total = sum(len(s.get("items") or []) for s in sources)
    return {
        "total": total,
        "groups": [
            {
                "label": s.get("label") or "Source",
                "n": s.get("n") or len(s.get("items") or []),
                "items": list(s.get("items") or []),
            }
            for s in sources
        ],
    }


def _insert_after_anchor(
    base: list[dict[str, Any]],
    extra: list[dict[str, Any]],
    anchors: tuple[str, ...] = ("takeaway", "key_idea", "explanation"),
) -> list[dict[str, Any]]:
    if not extra:
        return list(base)
    if not base:
        return list(extra)
    out: list[dict[str, Any]] = []
    placed = False
    for s in base:
        out.append(s)
        if not placed and s.get("type") in anchors:
            out.extend(extra)
            placed = True
    if not placed:
        out.extend(extra)
    return out


def _merge_sections(
    fence_secs: list[dict[str, Any]],
    linear: list[dict[str, Any]],
    math_secs: list[dict[str, Any]],
    code_secs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if fence_secs and not linear:
        return fence_secs + math_secs + code_secs
    if fence_secs:
        return fence_secs + linear + math_secs + code_secs
    return _insert_after_anchor(linear, math_secs) + code_secs


def compile_response(
    text: str,
    *,
    sources: list | None = None,
    kind: str = "result",
) -> dict[str, Any] | None:
    """Compile plain assistant text into a semantic document.

    Returns None when the reply should stay as a plain bubble (very short,
    non-result, or empty).
    """
    raw = (text or "").strip()
    if not raw or kind not in ("result", "ask", "system"):
        return None
    if kind == "ask" and "APPROVAL NEEDED" in raw:
        return None
    if kind == "system":
        return None
    if _looks_short(raw) and ":::" not in raw and "```" not in raw:
        if not _DISPLAY_TEX.search(raw) and not _FENCE.search(raw):
            return None

    residual, fence_secs = _parse_fences(raw)
    residual, code_secs = _extract_code_fences(residual)
    residual, math_secs = _extract_display_math(residual)
    linear = _parse_linear(residual.strip()) if residual.strip() else []

    sections = _merge_sections(fence_secs, linear, math_secs, code_secs)
    sections = _promote_takeaway(sections)
    # Drop empty
    cleaned: list[dict[str, Any]] = []
    for s in sections:
        if s.get("type") in ("list", "next_actions"):
            if s.get("items"):
                cleaned.append(s)
        elif s.get("type") == "formula":
            if s.get("tex"):
                cleaned.append(s)
        elif s.get("type") == "code":
            if s.get("text"):
                cleaned.append(s)
        elif (s.get("text") or "").strip():
            cleaned.append(s)
    sections = cleaned

    if not sections:
        return None

    educational = _is_educational(raw, sections)
    actions: list[dict[str, str]] = []
    if educational and kind == "result":
        # Don't duplicate if next_actions section already present with content
        has_na = any(s.get("type") == "next_actions" for s in sections)
        if not has_na:
            actions = _default_actions()

    grounding = _sources_summary(sources)

    return {
        "version": VERSION,
        "id": _cache_key(raw),
        "educational": educational,
        "sections": sections,
        "actions": actions,
        "grounding": grounding,
    }
