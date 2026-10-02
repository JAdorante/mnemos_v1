"""Org Record Service — the org tier of the records layer.

Grown inside org_coordinator so the legacy JSON coordinator (directory,
digests, cascade) keeps working unchanged; the records API mounts only when
QUILL_ORG_DATABASE_URL is set. Business logic lives in `service`, persistence
in org_coordinator/repo (the only SQLAlchemy importer).
"""
