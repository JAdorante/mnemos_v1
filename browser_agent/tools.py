"""The semantic action vocabulary (FR-ACT-1) and the JSON schemas used for
structured planner/verifier output.

P0 is read-only, so the irreversible-action tools from the full spec
(`upload`, `request_approval`) are intentionally omitted — they belong to P1/P2.
The model acts by element_id only; each tool maps to one deterministic
Playwright call in browser.py (FR-ACT-2).
"""

try:
    from desktop_agent.config import allowed_app_keys as _desktop_apps
    from desktop_agent.config import describe_apps as _desktop_caps
except Exception:  # pragma: no cover - defensive
    def _desktop_apps() -> str:
        return ("chrome, code, cursor, explorer, flstudio, notepad, phonelink, "
                "terminal")

    def _desktop_caps() -> str:
        return ""

# Neutral-by-default few-shot example names (data-driven when opted in). Guarded
# so browser_agent stays importable without app.* — see prompts.py / vocabulary.py.
try:
    from app.services.vocabulary import example_terms as _example_terms
except Exception:  # pragma: no cover - defensive
    def _example_terms() -> dict:
        return {"person": "<name>", "teammate": "<name>", "company": "Acme",
                "org": "<org>", "project": "<project>"}

_EX = _example_terms()

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
            "form, purchasing, deleting, or changing a saved record. Present a "
            "structured, source-grounded approval packet: WHAT will happen, the "
            "exact content to review, WHY you are doing it, and the SOURCE in "
            "Mnemos's memory that prompted it. Fill `why`/`source` from the "
            "RELEVANT MEMORIES / conversation context when the task came from "
            "something Mnemos heard or a promise the user made — approval should "
            "be grounded, not just 'Can I send this?'. Only perform the action "
            "if it is approved; if declined, do not retry it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string",
                            "description": "one line: what will happen if approved "
                            f"(e.g. \"Send email to {_EX['person']}\")"},
                # structured packet fields — all optional; supply the ones that fit
                # the action. An email uses to/subject/body; a form/CRM change uses
                # action + body (the field values); why/source ground it in memory.
                "action": {"type": "string",
                           "description": "the irreversible action, e.g. 'Send email', "
                           "'Submit form', 'Create contact', 'Purchase'"},
                "to": {"type": "string",
                       "description": "recipient / target, if applicable"},
                "subject": {"type": "string",
                            "description": "subject line, if applicable"},
                "body": {"type": "string",
                         "description": "the full content to review — email body, "
                         "form field values, the record being changed"},
                "why": {"type": "string",
                        "description": "why this is being done, grounded in what Mnemos "
                        "knows, e.g. 'You promised this follow-up after today's meeting.'"},
                "source": {"type": "string",
                           "description": "the memory this came from, e.g. "
                           "'Meeting transcript, 2:14 PM' — cite it verbatim from the "
                           "RELEVANT MEMORIES block when available."},
                "details": {"type": "string",
                            "description": "optional extra context not covered above"},
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

# --- pixel fallback vocabulary ---------------------------------------------
# Offered ONLY on turns where the page is dominated by a graphics surface the
# DOM cannot describe (a <canvas> game/map/editor, a video player, a plugin
# embed). Coordinates are in the attached screenshot's pixel space and are
# refused outside that surface — page chrome around it keeps using element_ids,
# so the model can't blind-click a Send/Buy button it merely guessed at.
PIXEL_TOOLS = [
    {
        "name": "click_at",
        "description": (
            "PIXEL FALLBACK: click inside the graphics surface at (x, y) — "
            "pixels from the top-left of the ATTACHED SCREENSHOT. Use for "
            "things drawn on a canvas (a playing card, a map pin, a toolbar "
            "icon) that have no element_id. Set clicks=2 to double-click. "
            "Coordinates outside the surface are refused — use element_id for "
            "the page's real buttons and links."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string",
                           "description": "left, right, or middle (default left)"},
                "clicks": {"type": "integer",
                           "description": "1 (default) or 2 for a double-click"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "drag",
        "description": (
            "PIXEL FALLBACK: press the mouse at (from_x, from_y), move to "
            "(to_x, to_y), and release — the drag-and-drop many canvas UIs "
            "need (moving a card, panning a map, drawing, a custom slider). "
            "Coordinates are pixels in the ATTACHED SCREENSHOT and both ends "
            "must lie inside the graphics surface."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_x": {"type": "integer"},
                "from_y": {"type": "integer"},
                "to_x": {"type": "integer"},
                "to_y": {"type": "integer"},
            },
            "required": ["from_x", "from_y", "to_x", "to_y"],
        },
    },
    {
        "name": "press_key",
        "description": (
            "Send a key or chord (enter, escape, space, arrowleft, ctrl+z, …) "
            "to the page. Canvas apps are often driven by the keyboard — "
            "undo a move, deal, dismiss an overlay. It goes to whatever the "
            "page has focused, so click the surface first if unsure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
]

PIXEL_ACTIONS = frozenset({t["name"] for t in PIXEL_TOOLS})


# --- desktop/OS action vocabulary ------------------------------------------
# Used when the router picks surface='desktop'. Each maps to one guarded method
# on desktop_agent.DesktopDriver — every action is jailed, allowlisted, and
# (if mutating) passes the human approval gate before it runs. There is no raw
# shell here: run_command takes argv as a LIST and only allowlisted verbs run.
DESKTOP_TOOLS = [
    {
        "name": "make_dir",
        "description": "Create a project folder inside the sandbox (jail). "
        "`name` is a relative folder name — never an absolute path or one with "
        "'..'. Use this to start a new project before opening it in an app.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a text file inside the sandbox — the "
        "ONLY way to author source files (index.html, app.js, style.css, README). "
        "Do NOT try to write files with run_command/echo/python -c: quotes and "
        "redirects are refused as shell metacharacters. `path` is the file's "
        "relative path (may include subfolders, e.g. \"src/main.js\"); `content` "
        "is the full file text; optional `project` names the sandbox folder to "
        "write inside. Parent folders are created automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "project": {"type": "string",
                            "description": "sandbox folder to write inside, optional"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "launch_app",
        "description": "Open a desktop app, optionally on a jailed target. "
        f"`app` is preferably one of the registry keys ({_desktop_apps()}); any "
        "other installed app may be requested by bare name and is discovered "
        "and vetted at launch time (launch-only, human-approved on first use). "
        "Each app may only be opened on the file types (or folder) it supports "
        "— pointing it at anything else is refused. Capabilities:\n"
        f"{_desktop_caps()}\n"
        "Set `project` to a make_dir folder (or a jailed file path) to open the "
        "app on it — e.g. open Cursor on a new project, or flstudio on a .flp "
        "inside the jail. (terminal and phonelink take no target.)",
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "project": {"type": "string",
                            "description": "sandbox folder name to open, optional"},
            },
            "required": ["app"],
        },
    },
    {
        "name": "run_command",
        "description": "Run one allowlisted shell command inside the sandbox. "
        "`argv` is the command split into a list (e.g. [\"git\",\"init\"]). Only "
        "allowlisted verbs run (git, npm, npx, pip, python, node, ls, echo, …); "
        "destructive or elevated commands are refused. `project` optionally names "
        "the sandbox folder to run in.",
        "input_schema": {
            "type": "object",
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}},
                "project": {"type": "string",
                            "description": "sandbox folder to run in, optional"},
            },
            "required": ["argv"],
        },
    },
    {
        "name": "list_dir",
        "description": "List the contents of the sandbox, or a folder within it "
        "(`name`). Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "ui_scan",
        "description": "PREFERRED way to interact with an open app: list an "
        "allowlisted app window's controls (buttons, menus, edits, tabs) as an "
        "indexed list via UI Automation — no mouse or focus is taken and the "
        "user can keep working. Act on a control with ui_invoke / ui_set_text "
        "using its [id]. Re-scan after the window changes (ids are per-scan). "
        "`title` narrows to a window whose title contains it. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string",
                        "description": "allowlisted app key (chrome, notepad, …)"},
                "title": {"type": "string",
                          "description": "optional window-title substring"},
            },
            "required": ["app"],
        },
    },
    {
        "name": "ui_invoke",
        "description": "Activate a control from the last ui_scan by its [id] "
        "(press a button, open a menu item, select a tab). Uses UI Automation — "
        "no mouse movement, works without stealing focus. Requires approval.",
        "input_schema": {
            "type": "object",
            "properties": {"control_id": {"type": "integer"}},
            "required": ["control_id"],
        },
    },
    {
        "name": "ui_set_text",
        "description": "REPLACE the text of an editable control from the last "
        "ui_scan (marked [set_text]) by its [id]. Overwrites the control's "
        "whole content — check its current value in the scan first. No mouse "
        "or focus taken. Requires approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "control_id": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["control_id", "text"],
        },
    },
    {
        "name": "click_at",
        "description": "PIXEL FALLBACK: click at screen coordinates (x, y) in "
        "pixels from the top-left of the primary display — this moves the "
        "user's real mouse. Use only when ui_scan can't see the control "
        "(canvas UIs like FL Studio). Coordinates must match the attached "
        "screenshot. Click a text field before type_text. Requires approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string",
                            "description": "left, right, or middle (default left)"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text at the current keyboard focus. Click the "
        "target field with click_at first. Requires approval.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "press_key",
        "description": "Press a key or chord (enter, tab, ctrl+s, alt+f, …). "
        "Use after typing to confirm dialogs. Requires approval.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "ask_human",
        "description": "Pause and ask the human operator a question when blocked "
        "or when only a human can decide.",
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "done",
        "description": "The task is complete. Provide a short result describing "
        "what was done.",
        "input_schema": {
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
        },
    },
]

# The Mnemos intent/action-router envelope (PRD "Planner LLM Service" output).
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
        "surface": {
            "type": "string",
            "enum": ["browser", "desktop", "phone_link", "none"],
            "description": "where the task runs: 'browser' for web, 'desktop' for "
            "local apps/files, 'phone_link' for iPhone SMS via Windows Phone Link, "
            "'none' if answerable from memory alone",
        },
        "requires_browser": {
            "type": "boolean",
            "description": "true if completing this needs a live web action "
            "(kept for compatibility; equals surface=='browser')",
        },
        "requires_user_approval": {
            "type": "boolean",
            "description": "true if the action would send, buy, delete, submit, "
            "change records, schedule, or share private info",
        },
        "tool": {
            "type": "string",
            "enum": ["browser_agent", "desktop_agent", "phone_link", "direct_answer"],
        },
        "site": {
            "type": "string",
            "description": "target site/app if known (gmail, calendar, crm, web), "
            "else empty string",
        },
        "rationale": {"type": "string", "description": "one sentence, why this route"},
    },
    "required": [
        "intent", "surface", "requires_browser", "requires_user_approval",
        "tool", "site", "rationale",
    ],
    "additionalProperties": False,
}

PHONE_GOAL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["send_sms", "read_messages", "open", "reply"],
            "description": "send_sms/text/reply to message someone; read_messages to list/read SMS",
        },
        "recipient": {
            "type": "string",
            "description": "contact name or phone number for SMS; empty if not applicable",
        },
        "message": {
            "type": "string",
            "description": "the exact SMS body the user dictated to send; leave EMPTY "
                           "if they named a recipient but did not say what to send "
                           "(never invent or infer a body), and empty for read/open",
        },
    },
    "required": ["action", "recipient", "message"],
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
