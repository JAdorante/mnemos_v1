"""Builders for deterministic ranking golden corpora.

Each corpus is a small Store populated with a fixed seed. Tests snapshot
focus membership + order (+ breakdowns) per scorer.
"""
from __future__ import annotations

import time
from pathlib import Path

from app.storage import Store

# Fixed "now" so age/due math is stable across runs.
CORPUS_NOW = 1_700_000_000.0  # ~2023-11-14


def _open_store(path: Path) -> Store:
    return Store(path)


def build_small(db_path: Path) -> Store:
    """~30 nodes: a few people, entities, mixed open work."""
    store = _open_store(db_path)
    now = CORPUS_NOW
    for name in ("Ada Lovelace", "Bea Kernel", "Cypher Node"):
        store.resolve_person(name)
    for name, kind in (
        ("GitHub", "tool"),
        ("Series A", "project"),
        ("Office", "place"),
        ("Notes app", "tool"),
    ):
        store.resolve_entity(name, kind)
    for i in range(8):
        store.add_task(
            f"Small task {i}",
            confidence=0.85,
            extracted_at=now - i * 86400,
        )
    store.add_task(
        "Due tomorrow",
        confidence=0.9,
        extracted_at=now - 2 * 86400,
        due="2023-11-15",
    )
    try:
        from app.services.graph import rebuild
        rebuild(store)
    except Exception:
        pass
    return store


def build_medium(db_path: Path) -> Store:
    """~200-ish candidate surface: many people/entities/tasks."""
    store = _open_store(db_path)
    now = CORPUS_NOW
    for i in range(40):
        store.resolve_person(f"Person {i:02d}")
    for i in range(30):
        kind = ("tool", "project", "place", "idea")[i % 4]
        store.resolve_entity(f"Entity {i:02d}", kind)
    for i in range(80):
        store.add_task(
            f"Medium task {i}",
            confidence=0.7 + (i % 5) * 0.05,
            extracted_at=now - (i % 30) * 86400,
        )
    try:
        from app.services.graph import rebuild
        rebuild(store)
    except Exception:
        pass
    return store


def build_all_tasks(db_path: Path) -> Store:
    """Adversarial: flood of open tasks, few people/entities."""
    store = _open_store(db_path)
    now = CORPUS_NOW
    store.resolve_person("Ada")
    store.resolve_person("Bea")
    for name in ("GitHub", "AWS", "Cursor"):
        store.resolve_entity(name, "tool")
    for i in range(26):
        store.add_task(
            f"Noise task {i}",
            confidence=0.99,
            extracted_at=now - i * 3600,
        )
    try:
        from app.services.graph import rebuild
        rebuild(store)
    except Exception:
        pass
    return store


def build_one_cluster(db_path: Path) -> Store:
    """Adversarial: many near-duplicate tasks (MMR should collapse)."""
    store = _open_store(db_path)
    now = CORPUS_NOW
    store.resolve_person("Ada")
    store.resolve_person("Bea")
    store.resolve_entity("Project X", "project")
    store.resolve_entity("Tool Y", "tool")
    store.resolve_entity("Place Z", "place")
    for i in range(20):
        store.add_task(
            f"Follow up on the same thread item {i}",
            confidence=0.95,
            extracted_at=now - 1000 - i,
        )
    try:
        from app.services.graph import rebuild
        rebuild(store)
    except Exception:
        pass
    return store


def build_heavy_pins(db_path: Path) -> Store:
    """Adversarial: many pinned nodes must all appear in focus."""
    store = _open_store(db_path)
    now = CORPUS_NOW
    people_ids = []
    for name in ("Ada Lovelace", "Bea Kernel", "Cypher Node", "Dee Rivest", "Eve Cipher"):
        people_ids.append(store.resolve_person(name))
    for name in ("GitHub", "AWS", "Cursor"):
        store.resolve_entity(name, "tool")
    for i in range(10):
        store.add_task(f"Unpinned task {i}", confidence=0.8,
                       extracted_at=now)
    # Pin first three people.
    for pid in people_ids[:3]:
        store.set_constellation_pin("person", int(pid), True)
    try:
        from app.services.graph import rebuild
        rebuild(store)
    except Exception:
        pass
    return store


CORPUS_BUILDERS = {
    "small": build_small,
    "medium": build_medium,
    "all_tasks": build_all_tasks,
    "one_cluster": build_one_cluster,
    "heavy_pins": build_heavy_pins,
}
