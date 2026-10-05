"""WritableConnector — the write-back contract (spec, "Write-back connectors").

Runs in the Org Record Service, never on a node. The external system stays
the system of record: Sparrow writes what a human approved, reads it back to
prove it landed, and later reads it again to notice when someone changed it.

Plans, previews and results are plain JSON-able dicts, because a preview is
embedded in the promotion payload and therefore under its hash:

  plan     {"connector_id", "kind", "target", "op", "fields": {...} | None,
            "text": str | None, "marker": str | None}
  preview  {"connector_id", "target", "op", "fields"/"text"/"marker",
            "diff": [{"field", "before", "after"}]}

`map_record` is deterministic — same version + same mapping row, same plan,
byte for byte — and there is no LLM anywhere in the write path.

The spec puts this protocol in app/services/connectors/base.py; it lives here
because the service image ships without the node's app package.
"""
from __future__ import annotations

from typing import Any, Protocol


class ConnectorError(Exception):
    """Base: something went wrong talking to the external system."""


class TransientError(ConnectorError):
    """Retry later (429, 5xx, network)."""


class PermanentError(ConnectorError):
    """Retrying will not help (bad token, missing object, invalid field)."""


class ConflictError(ConnectorError):
    """The external value moved since the approver saw the preview."""

    def __init__(self, field: str, expected: Any, actual: Any) -> None:
        super().__init__(f"{field}: expected {expected!r}, found {actual!r}")
        self.field, self.expected, self.actual = field, expected, actual


OPS = ("set_property", "create_note", "create_task", "append_entry", "post")
# Ops whose fields can be read back and compared later (drift detection).
DRIFTABLE_OPS = ("set_property",)


def plan_core(plan: dict[str, Any]) -> dict[str, Any]:
    """The part of a plan the approver binds to, minus what the external
    system currently holds (`diff.before` moves; the intended write does not)."""
    return {k: plan.get(k) for k in ("connector_id", "kind", "target", "op",
                                     "fields", "text", "marker")}


class WritableConnector(Protocol):
    kind: str

    def resolve_target(self, scope: dict, subject_ref: str,
                       target_hint: str | None) -> str | None: ...

    def map_record(self, version: dict, target: str,
                   mapping: dict) -> dict: ...

    def preview(self, plan: dict) -> list[dict]: ...

    def write(self, plan: dict, idempotency_key: str) -> dict: ...

    def read_back(self, plan: dict) -> dict: ...

    def supports(self, kind: str, predicate: str, op: str) -> bool: ...
