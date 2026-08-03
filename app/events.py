"""The common event shape that every modality feeds into the Memory Engine.

Everything vinceo.ai perceives — a spoken utterance, a captured frame, an OCR
result — is normalized into an `Event` and pushed onto the in-process
`EventBus`. Later milestones (memory, knowledge graph, agents) subscribe to
this bus instead of talking to each modality directly.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Awaitable, Callable


class Modality(str, Enum):
    AUDIO = "audio"
    VISION = "vision"
    NOTIFICATION = "notification"  # Windows toasts (Phone Link / iPhone mirror)
    INPUT = "input"          # keyboard / mouse activity
    SYSTEM = "system"
    DOCUMENT = "document"    # file text read off disk (PDF / Word / notes)
    TEXT = "text"            # typed chat — statements the user tells vinceo directly


@dataclass
class Event:
    time: float                       # epoch seconds
    modality: Modality
    raw: str                          # raw payload (transcript text, ocr, ...)
    summary: str = ""
    source: str = ""                  # which service produced it
    confidence: float | None = None
    people: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["modality"] = self.modality.value
        return d

    # --- confidence contract (#3) -----------------------------------------
    # The epistemic tag + confidence facets ride in `meta` (so they persist as
    # JSON with the event). These read-only views surface them ergonomically;
    # `app.services.confidence.attach` is what writes them.
    @property
    def epistemic(self) -> str:
        """How this event's content was obtained: observed | extracted |
        inferred | accepted (or '' if never stamped)."""
        return (self.meta or {}).get("epistemic", "")

    @property
    def confidence_facets(self) -> dict[str, float]:
        """The separated 0..1 facets (capture_quality / model_confidence /
        semantic_confidence / action_confidence) that were known."""
        return (self.meta or {}).get("confidence", {}) or {}

    @property
    def action_readiness(self) -> float | None:
        """Combined 0..1 readiness-to-act, or None if the contract wasn't set."""
        return (self.meta or {}).get("action_readiness")


Subscriber = Callable[[Event], Awaitable[None] | None]


class EventBus:
    """Minimal async pub/sub. Synchronous producers can use `publish_nowait`."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    async def publish(self, event: Event) -> None:
        for fn in list(self._subscribers):
            result = fn(event)
            if asyncio.iscoroutine(result):
                await result

    def publish_nowait(self, event: Event) -> None:
        """Publish from a non-async thread (e.g. the audio callback thread)."""
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self.publish(event), self._loop)
        else:
            # No loop bound (e.g. standalone CLI): call sync subscribers directly.
            for fn in list(self._subscribers):
                res = fn(event)
                if asyncio.iscoroutine(res):
                    res.close()


bus = EventBus()
