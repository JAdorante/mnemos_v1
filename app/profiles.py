"""Deployment profiles: what runs for this seat, in one file.

There are hundreds of QUILL_* variables, and the learning loop alone used to
run only if someone knew to set six of them. A profile names a deployment
shape and states which *stages* are on; `QUILL_PROFILE=<name>` applies it at
startup, before `settings` is built.

Profiles fill in defaults (`os.environ.setdefault`): a variable set in the
environment or .env still wins, so one flag can be overridden without
leaving the profile. `running()` answers "what is actually running for this
seat" from the live environment, stage by stage, and says where each value
came from (the profile, an explicit override, or the code default).

Standard library only: app/config.py imports this before anything else.
"""
from __future__ import annotations

import os
from typing import Any

_OFF = ("", "0", "false", "False", "off", "no")

# stage -> (what it is, [(flag, code default, on-values or None = "not off")])
# A stage is on when ALL of its flags read on.
STAGES: dict[str, tuple[str, list[tuple[str, str, tuple[str, ...] | None]]]] = {
    "memory.semantic": ("Semantic search over the timeline (LanceDB)",
                        [("QUILL_SEMANTIC", "1", None)]),
    "people.v2": ("People Intelligence: names resolve into person records",
                  [("QUILL_PEOPLE_V2", "1", None)]),
    "capture.vision": ("Webcam frames become memories",
                       [("QUILL_VISION", "0", None)]),
    "capture.desktop": ("Screen and click capture (no keystrokes)",
                        [("QUILL_DESKTOP_CAPTURE", "0", None)]),
    "capture.phone": ("Phone Link notifications and SMS (Windows)",
                      [("QUILL_PHONE_LINK", "1", None)]),
    "agent.planner": ("Goals compile into risk-classified action plans",
                      [("QUILL_PLANNER", "1", None)]),
    "agent.offers": ("Proactive offers in chat (to-dos, tasks, phone)",
                     [("QUILL_AGENT", "1", None)]),
    "agent.anticipate": ("Likely-next offers from activity patterns",
                         [("QUILL_ANTICIPATE", "0", None)]),
    "mail.send": ("Approved mail sends from the user's own account",
                  [("QUILL_MAIL_SEND", "0", None)]),
    "learning.pairs": ("Every human verdict becomes a learning pair",
                       [("QUILL_LEARNING", "1", None)]),
    "learning.local_text": ("Local model first for chat / extract / reflect",
                            [("QUILL_TEXT_LOCAL", "0", None)]),
    "learning.exemplars": ("Confirmed corrections retrieved into prompts",
                           [("QUILL_EXEMPLARS", "0", None)]),
    "learning.shadow_eval": ("Nightly parent re-grade of kept local answers",
                             [("QUILL_SHADOW_EVAL", "0", None)]),
    "learning.router": ("Learned escalation router (shadow or active)",
                        [("QUILL_ROUTER", "off", ("shadow", "active"))]),
    "learning.lora": ("Idle QLoRA training when a task type saturates",
                      [("QUILL_IDLE_TRAIN", "0", None)]),
    "network.peer": ("Teammates' Sparrows can ask this one questions",
                     [("QUILL_PEER_CHANNEL", "1", None)]),
    "network.records": ("Approved claims become org records",
                        [("QUILL_RECORDS", "0", None)]),
    "network.fleet": ("Agent signals through the firm relay",
                      [("QUILL_FLEET", "0", None)]),
    "network.fleet_exemplars": ("Offer and accept teammates' corrections",
                                [("QUILL_FLEET_EXEMPLARS_OFFER", "0", None),
                                 ("QUILL_FLEET_EXEMPLARS_ACCEPT", "0", None)]),
    "network.org": ("Org AI Network: digests, priorities, escalation",
                    [("QUILL_ORG_NETWORK", "0", None)]),
    "assistants.mcp_local": ("Read-only memory tools for local assistants",
                             [("QUILL_MCP", "0", None)]),
    "assistants.mcp_remote": ("ChatGPT / Claude / Grok connect over /mcp",
                              [("QUILL_MCP_REMOTE", "0", None)]),
}

_LEARNING_LOOP = {
    "QUILL_LEARNING": "1",
    "QUILL_TEXT_LOCAL": "1",
    "QUILL_EXEMPLARS": "1",
    "QUILL_ROUTER": "shadow",
}

PROFILES: dict[str, dict[str, Any]] = {
    "desktop-solo": {
        "about": "One person on their own laptop. Capture, memory and the "
                 "learning loop; nothing networked.",
        "env": {
            **_LEARNING_LOOP,
            "QUILL_SHADOW_EVAL": "1",
            "QUILL_IDLE_TRAIN": "0",
            "QUILL_MCP": "1",
            "QUILL_RECORDS": "0",
            "QUILL_FLEET": "0",
            "QUILL_ORG_NETWORK": "0",
        },
    },
    "hosted-pilot": {
        "about": "A headless seat on the hosted box: meeting capture, the "
                 "learning loop, records, fleet, remote assistants.",
        "env": {
            **_LEARNING_LOOP,
            "QUILL_DESKTOP_CAPTURE": "0",
            "QUILL_PHONE_LINK": "0",
            "QUILL_PHONE_WATCH": "0",
            "QUILL_PEER_DEFAULT_PACK": "pilot",
            "QUILL_MAIL_SEND": "1",
            "QUILL_MCP_REMOTE": "1",
            "QUILL_RECORDS": "1",
            "QUILL_FLEET": "1",
            # The browser agent drafts but never sends on a hosted seat.
            "AGENT_DRY_RUN": "draft",
        },
    },
    "org": {
        "about": "A seat in an organization: everything in hosted-pilot, "
                 "plus the org network, nightly grading and shared "
                 "corrections.",
        "env": {
            **_LEARNING_LOOP,
            "QUILL_DESKTOP_CAPTURE": "0",
            "QUILL_PHONE_LINK": "0",
            "QUILL_PHONE_WATCH": "0",
            "QUILL_PEER_DEFAULT_PACK": "pilot",
            "QUILL_MAIL_SEND": "1",
            "QUILL_MCP_REMOTE": "1",
            "QUILL_RECORDS": "1",
            "QUILL_FLEET": "1",
            "QUILL_ORG_NETWORK": "1",
            "QUILL_SHADOW_EVAL": "1",
            "QUILL_FLEET_EXEMPLARS_OFFER": "1",
            "QUILL_FLEET_EXEMPLARS_ACCEPT": "1",
            "AGENT_DRY_RUN": "draft",
        },
    },
    "tester": {
        "about": "The September desktop tester cohort: meeting-first, the "
                 "reliable 20%. Local text is off, so the learning loop "
                 "runs only on exemplars from cloud answers.",
        "env": {
            "QUILL_FIRST_RUN_MODE": "meeting",
            "QUILL_PHONE_LINK": "0",
            "QUILL_ANTICIPATE": "0",
            "QUILL_DESKTOP_CAPTURE": "0",
            "QUILL_PHONE_WATCH": "0",
            "QUILL_AUTOSTART_NOTIFICATIONS": "0",
            "QUILL_TEXT_LOCAL": "0",
            # Research → memory writeback (sync) so memory-only follow-ups
            # work in the same test loop without a Helpful click.
            "QUILL_RESEARCH_INGEST": "1",
            "QUILL_RESEARCH_INGEST_SYNC": "1",
        },
    },
}

# Which keys the applied profile actually set (vs. found already set).
_applied: dict[str, str] = {}
_profile_name: str = ""


def current() -> str:
    """The profile named by QUILL_PROFILE, or "" when none (or unknown)."""
    name = os.environ.get("QUILL_PROFILE", "").strip().lower()
    return name if name in PROFILES else ""


def apply(name: str | None = None, *, extra: dict[str, str] | None = None
          ) -> dict[str, str]:
    """Fill the environment with the profile's defaults. Returns the keys it
    set; keys already present are left alone. `extra` adds pins decided at
    startup (the tester profile's Ollama check)."""
    global _profile_name
    name = (name if name is not None else current()).strip().lower()
    if name not in PROFILES:
        if name:
            print(f"[profiles] unknown QUILL_PROFILE {name!r}; known: "
                  f"{', '.join(PROFILES)}.")
        return {}
    _profile_name = name
    pins = {**PROFILES[name]["env"], **(extra or {})}
    for key, val in pins.items():
        if key not in os.environ:
            os.environ[key] = val
            _applied[key] = val
    return dict(_applied)


def _flag_on(value: str, on_values: tuple[str, ...] | None) -> bool:
    v = (value or "").strip()
    if on_values is not None:
        return v.lower() in on_values
    return v not in _OFF


def running() -> dict[str, Any]:
    """Stage-by-stage view of this seat, from the live environment."""
    name = _profile_name or current()
    prof = PROFILES.get(name, {})
    stages: dict[str, Any] = {}
    for stage, (about, flags) in STAGES.items():
        rows = []
        for flag, default, on_values in flags:
            if flag in _applied and os.environ.get(flag) == _applied[flag]:
                source = "profile"
            elif flag in os.environ:
                source = "override" if flag in prof.get("env", {}) else "env"
            else:
                source = "default"
            value = os.environ.get(flag, default)
            rows.append({"flag": flag, "value": value, "source": source,
                         "on": _flag_on(value, on_values)})
        stages[stage] = {"on": all(r["on"] for r in rows), "about": about,
                         "flags": rows}
    return {"profile": name or None,
            "about": prof.get("about"),
            "profiles": {k: v["about"] for k, v in PROFILES.items()},
            "on": sorted(s for s, v in stages.items() if v["on"]),
            "stages": stages}
