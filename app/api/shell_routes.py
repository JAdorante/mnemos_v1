"""Settings and Inspect — the two rail items below the five destinations.

Settings consolidates the configuration surfaces behind one page with tabs
(capture, connectors, privacy, peers, team). Each tab lists the surface that
owns the control today; the controls themselves move in later steps.
Inspect is the developer index: every old console view behind one link.
"""
from __future__ import annotations

import os

from fastapi import APIRouter

from app.api import ui

router = APIRouter()

TABS = (("capture", "Capture"), ("connectors", "Connectors"),
        ("privacy", "Privacy"), ("peers", "Peers"), ("team", "Team"))


def _tester() -> bool:
    return os.environ.get("QUILL_PROFILE", "").strip().lower() == "tester"


def _sections(tab: str) -> list[dict]:
    tester = _tester()
    if tab == "capture":
        return [
            {"title": "Capture sources", "lead": "What Sparrow may listen to and watch, and the consent behind each source.",
             "links": [{"href": "/capture", "label": "Microphone, system audio, screen, camera", "note": "consent and pause"},
                       {"href": "/meetings", "label": "Meeting capture", "note": "sessions, retention, notes"}]},
            {"title": "Speakers and voice", "lead": "Who Sparrow recognises, and whether it speaks replies aloud.",
             "links": [{"href": "/capture#speakers", "label": "Enrolled speakers"},
                       {"href": "/capture#voice", "label": "Spoken replies"}]},
        ]
    if tab == "connectors":
        links = [{"href": "/onboarding", "label": "Mail, calendar and documents", "note": "sign in, background sync, message bodies"},
                 {"href": "/assistants", "label": "Outside assistants", "note": "ChatGPT, Claude, Grok over MCP"}]
        if not tester:
            links += [{"href": "/phone", "label": "Phone", "note": "pairing and sync"},
                      {"href": "/desktop-access", "label": "Desktop access", "note": "apps the agent may drive"}]
        return [{"title": "Connected sources", "lead": "Each connector names what it reads and what it may send.", "links": links}]
    if tab == "privacy":
        return [
            {"title": "What leaves this device", "lead": "Nothing crosses without a rule you set or a decision you made.",
             "links": [{"href": "/privacy/egress", "label": "Egress ledger", "note": "every send, who approved it"},
                       {"href": "/memory/changes", "label": "Facts Sparrow updated on its own", "note": "reversible"},
                       {"href": "/capture#privacy", "label": "Privacy class and blocklist"}]},
            {"title": "Forgetting", "lead": "Remove a person or an entity and everything drawn from them.",
             "links": [{"href": "/profile?tab=people", "label": "People", "note": "forget, merge, rename"},
                       {"href": "/records", "label": "Team records", "note": "retract what was shared"}]},
        ]
    if tab == "peers":
        return [{"title": "Teammates", "lead": "Pairing, disclosure packs and the questions teammates may ask your memory.",
                 "links": [{"href": "/peer", "label": "Peers and policy"},
                           {"href": "/peer#asks", "label": "Questions waiting on you", "note": "also in Inbox"}]}]
    links = [{"href": "/records", "label": "Shared records", "note": "what the team holds"},
             {"href": "/projects", "label": "Projects", "note": "shared project notes"}]
    if not tester:
        links.append({"href": "/org-network", "label": "Organisation network", "note": "digests, priorities, escalations"})
    return [{"title": "Team", "lead": "The org tier: records, projects and the organisation network.", "links": links}]


@router.get("/settings")
def settings_page(tab: str = "capture"):
    if tab not in dict(TABS):
        tab = "capture"
    from app.api.routes import _agent_worker
    return ui.render("settings.html", path="/settings", query=f"tab={tab}",
                     agent_worker=_agent_worker(), tabs=TABS, tab=tab,
                     sections=_sections(tab))


INSPECT_GROUPS = (
    {"title": "Memory console", "links": [
        {"href": "/memory", "label": "Feed and search"},
        {"href": "/memory?mode=constellation", "label": "Constellation"},
        {"href": "/memory/changes", "label": "Changes", "note": "supersessions, revert"},
        {"href": "/meetings", "label": "Meetings"},
    ]},
    {"title": "Pipeline", "links": [
        {"href": "/console/jobs", "label": "Jobs", "note": "JSON"},
        {"href": "/console/queue_slo", "label": "Review queue depth", "note": "JSON"},
        {"href": "/console/readiness", "label": "Readiness", "note": "JSON"},
        {"href": "/console/economy", "label": "Memory economy", "note": "JSON"},
        {"href": "/health", "label": "Health", "note": "JSON"},
    ]},
    {"title": "Knowledge", "links": [
        {"href": "/kg/parity", "label": "Parity", "note": "JSON"},
        {"href": "/kg/adjudications", "label": "Adjudications", "note": "JSON"},
        {"href": "/field/state", "label": "Field state", "note": "JSON"},
        {"href": "/graph/stats", "label": "Graph stats", "note": "JSON"},
    ]},
    {"title": "Agent", "links": [
        {"href": "/console/agent-runs", "label": "Agent runs", "note": "JSON"},
        {"href": "/decisions/summary?days=7", "label": "Decisions this week", "note": "JSON"},
        {"href": "/console/egress", "label": "Egress", "note": "JSON"},
        {"href": "/triggers", "label": "Triggers"},
        {"href": "/selfreport", "label": "Self report"},
    ]},
    {"title": "Team and devices", "links": [
        {"href": "/records", "label": "Records"},
        {"href": "/org-network", "label": "Org network"},
        {"href": "/desktop-access", "label": "Desktop access"},
        {"href": "/phone", "label": "Phone"},
    ]},
)


@router.get("/inspect")
def inspect_page():
    from app.api.routes import _agent_worker
    return ui.render("inspect.html", path="/inspect", agent_worker=_agent_worker(),
                     groups=INSPECT_GROUPS)
