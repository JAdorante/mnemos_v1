"""App capability templates — plain-language presets for desktop app registration.

Non-technical users pick *what kind of app* this is; templates map to vetted
capability contracts. Contributors still edit apps.default.json directly.
"""
from __future__ import annotations

from pathlib import Path

# id -> {capabilities, plain summary fragments}
TEMPLATES: dict[str, dict] = {
    "browser": {
        "label": "Browser",
        "capabilities": {
            "launch": True,
            "open_jailed_files": [".html", ".htm", ".pdf", ".svg", ".txt"],
            "opens_dirs": False,
            "pixel_ui": "approval_required",
            "shell": False,
            "network": True,
            "risk": "medium",
            "demo_safe": True,
            "notes": "Opens web pages and local HTML/PDF files from your Mnemos folder.",
        },
        "summary": (
            "It can open web pages and local HTML/PDF files from your Mnemos folder. "
            "It cannot run commands or control other apps."
        ),
    },
    "text_notes": {
        "label": "Text / notes",
        "capabilities": {
            "launch": True,
            "open_jailed_files": [".txt", ".md", ".log", ".json", ".csv", ".ini"],
            "opens_dirs": False,
            "pixel_ui": "approval_required",
            "shell": False,
            "network": False,
            "risk": "low",
            "demo_safe": True,
            "notes": "Plain-text and notes files inside your Mnemos folder.",
        },
        "summary": (
            "It can open text and notes files (.txt, .md, …) from your Mnemos folder. "
            "It cannot browse the rest of your computer."
        ),
    },
    "documents": {
        "label": "Documents",
        "capabilities": {
            "launch": True,
            "open_jailed_files": [".doc", ".docx", ".odt", ".rtf", ".pdf", ".txt"],
            "opens_dirs": False,
            "pixel_ui": "approval_required",
            "shell": False,
            "network": False,
            "risk": "medium",
            "demo_safe": True,
            "notes": "Word-processor and document files from your Mnemos folder.",
        },
        "summary": (
            "It can open document files from your Mnemos folder. "
            "It cannot run commands or reach outside that folder."
        ),
    },
    "file_manager": {
        "label": "File manager",
        "capabilities": {
            "launch": True,
            "open_jailed_files": [],
            "opens_dirs": True,
            "pixel_ui": "off",
            "shell": False,
            "network": False,
            "risk": "low",
            "demo_safe": True,
            "notes": "Shows a folder inside your Mnemos workspace.",
        },
        "summary": (
            "It can open folders inside your Mnemos workspace. "
            "It cannot edit files unless you also allow a separate editor."
        ),
    },
    "media": {
        "label": "Media",
        "capabilities": {
            "launch": True,
            "open_jailed_files": [".mp3", ".wav", ".flac", ".mp4", ".mkv", ".jpg",
                                  ".png", ".gif", ".flp", ".mid", ".midi"],
            "opens_dirs": False,
            "pixel_ui": "approval_required",
            "shell": False,
            "network": False,
            "risk": "medium",
            "demo_safe": True,
            "notes": "Audio, video, and project files from your Mnemos folder.",
        },
        "summary": (
            "It can open media and project files from your Mnemos folder. "
            "It cannot run commands or control other apps."
        ),
    },
    "communication": {
        "label": "Communication",
        "capabilities": {
            "launch": True,
            "open_jailed_files": [],
            "opens_dirs": False,
            "pixel_ui": "approval_required",
            "shell": False,
            "network": True,
            "risk": "medium",
            "demo_safe": False,
            "notes": "Chat or messaging app; network-capable.",
        },
        "summary": (
            "It can connect to the network for messaging. "
            "It cannot run shell commands or open arbitrary files unless you widen this later."
        ),
    },
}

_TEMPLATE_IDS = tuple(TEMPLATES.keys())

_EXT_HINTS: dict[str, str] = {
    ".html": "browser", ".htm": "browser", ".pdf": "browser",
    ".txt": "text_notes", ".md": "text_notes", ".log": "text_notes",
    ".json": "text_notes", ".csv": "text_notes",
    ".doc": "documents", ".docx": "documents", ".odt": "documents", ".rtf": "documents",
    ".mp3": "media", ".wav": "media", ".mp4": "media", ".flp": "media",
    ".mid": "media", ".midi": "media",
}

_LINUX_CATEGORY_HINTS = {
    "Network": "browser",
    "WebBrowser": "browser",
    "TextEditor": "text_notes",
    "Office": "documents",
    "WordProcessor": "documents",
    "FileManager": "file_manager",
    "AudioVideo": "media",
    "Audio": "media",
    "Video": "media",
    "Chat": "communication",
    "Email": "communication",
}


def template_ids() -> tuple[str, ...]:
    return _TEMPLATE_IDS


def template_label(template_id: str) -> str:
    t = TEMPLATES.get((template_id or "").strip().lower())
    return t["label"] if t else (template_id or "App")


def capabilities_for(template_id: str, *, display_name: str = "") -> dict:
    tid = (template_id or "text_notes").strip().lower()
    base = dict(TEMPLATES.get(tid, TEMPLATES["text_notes"])["capabilities"])
    if display_name:
        base["display_name"] = display_name
    return base


def describe_plain(template_id: str, app_name: str) -> str:
    tid = (template_id or "text_notes").strip().lower()
    t = TEMPLATES.get(tid, TEMPLATES["text_notes"])
    name = app_name or "This app"
    return f"{name} is a {t['label'].lower()}. {t['summary']}"


def infer_template(app_key: str, exe_path: str = "",
                   launch_args: list | None = None,
                   discovery_source: str = "") -> str:
    """Best-effort template guess from extension, .desktop category, or name."""
    args = launch_args or []
    for arg in args:
        ext = Path(str(arg)).suffix.lower()
        if ext in _EXT_HINTS:
            return _EXT_HINTS[ext]

    if discovery_source == "desktop_entry" or exe_path.endswith(".desktop"):
        cat = _read_desktop_category(exe_path)
        for part in cat.split(";"):
            part = part.strip()
            if part in _LINUX_CATEGORY_HINTS:
                return _LINUX_CATEGORY_HINTS[part]

    key = (app_key or "").lower()
    hints = {
        "chrome": "browser", "firefox": "browser", "chromium": "browser",
        "gedit": "text_notes", "notepad": "text_notes",
        "explorer": "file_manager", "nautilus": "file_manager",
        "flstudio": "media", "spotify": "media",
        "claude": "communication", "slack": "communication", "discord": "communication",
    }
    if key in hints:
        return hints[key]

    exe = Path(exe_path or key).stem.lower()
    if exe in hints:
        return hints[exe]
    return "text_notes"


def _read_desktop_category(path: str) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("Categories="):
            return line.split("=", 1)[1].strip()
    return ""


def template_choices() -> list[dict]:
    return [{"id": tid, "label": TEMPLATES[tid]["label"]} for tid in _TEMPLATE_IDS]
