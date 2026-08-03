"""The semantic action vocabulary (FR-ACT-1) and the JSON schemas used for
structured planner/verifier output.

P0 is read-only, so the irreversible-action tools from the full spec
(`upload`, `request_approval`) are intentionally omitted — they belong to P1/P2.
The model acts by element_id only; each tool maps to one deterministic
Playwright call in browser.py (FR-ACT-2).
"""

ACTION_TOOLS = [
    {
        "name": "click",
        "description": "Click an element by its element_id.",
        "input_schema": {
            "type": "object",
            "properties": {"element_id": {"type": "integer"}},
            "required": ["element_id"],
        },
    },
    {
        "name": "type",
        "description": (
            "Type text into an editable element (input/textarea/contenteditable) "
            "by element_id — a search box, a form field, or an email draft's "
            "recipient/subject/body. Never type passwords, card numbers, or "
            "other secrets; a human enters those."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["element_id", "text"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option in a dropdown (select) by element_id. "
        "`option` is the visible label or the underlying value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {"type": "integer"},
                "option": {"type": "string"},
            },
            "required": ["element_id", "option"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the page up or down.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"]},
                "amount": {"type": "integer", "description": "pixels, default 600"},
            },
            "required": ["direction"],
        },
    },
    {
        "name": "navigate",
        "description": "Go directly to a URL.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "go_back",
        "description": "Navigate back to the previous page.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "wait_for",
        "description": "Wait for the page to settle. `condition` is "
        "'network-idle' or a text string to wait for.",
        "input_schema": {
            "type": "object",
            "properties": {"condition": {"type": "string"}},
            "required": ["condition"],
        },
    },
    {
        "name": "read",
        "description": "Return the text content of an element (by element_id) "
        "or, if element_id is omitted, the page's main content. Use this to "
        "gather information to reason over or summarize.",
        "input_schema": {
            "type": "object",
            "properties": {"element_id": {"type": "integer"}},
            "required": [],
        },
    },
    {
        "name": "ask_human",
        "description": "Pause and ask the human operator a question. Use when "
        "blocked, when a login or CAPTCHA wall appears, or when only a human "
        "can decide.",
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "request_approval",
        "description": (
            "Pause and ask the human to approve an irreversible or sensitive "
            "action BEFORE you take it — sending an email/message, submitting a "
            "form, purchasing, deleting, or changing a saved record. Give a "
            "clear summary of exactly what will happen (e.g. the recipient and "
            "subject of an email you are about to send). Only perform the action "
            "if it is approved; if it is declined, do not retry it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string",
                            "description": "one line: what will happen if approved"},
                "details": {"type": "string",
                            "description": "optional: the full content to review"},
            },
            "required": ["summary"],
        },
    },
    {
        "name": "done",
        "description": "The goal is complete. Provide the final result — e.g. "
        "the requested summary or answer.",
        "input_schema": {
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
        },
    },
]

# The QUILL intent/action-router envelope (PRD "Planner LLM Service" output).
# Produced once per user request, before step planning, to decide whether a web
# action is even needed and whether it would require user approval.
ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "description": "short verb_noun label, e.g. draft_email, research_company, "
            "summarize_page, update_crm, memory_question",
        },
        "requires_browser": {
            "type": "boolean",
            "description": "true if completing this needs a live web action",
        },
        "requires_user_approval": {
            "type": "boolean",
            "description": "true if the action would send, buy, delete, submit, "
            "change records, schedule, or share private info",
        },
        "tool": {
            "type": "string",
            "enum": ["browser_agent", "direct_answer"],
        },
        "site": {
            "type": "string",
            "description": "target site/app if known (gmail, calendar, crm, web), "
            "else empty string",
        },
        "rationale": {"type": "string", "description": "one sentence, why this route"},
    },
    "required": [
        "intent", "requires_browser", "requires_user_approval",
        "tool", "site", "rationale",
    ],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "success_criteria": {"type": "string"},
                },
                "required": ["description", "success_criteria"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["steps"],
    "additionalProperties": False,
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "satisfied": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["satisfied", "reason"],
    "additionalProperties": False,
}
