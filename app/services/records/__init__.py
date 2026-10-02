"""Records layer — Tier 2 claims and their promotion into org records.

  capture (events, Tier 1)  ->  claims (Tier 2, this node)  ->  records (Tier 3,
  Org Record Service)

Nothing here moves Tier 1 content off the node. A claim carries evidence
POINTERS (event id, verbatim span, quote hash); a promotion packet carries the
exact record body that will be written, hash-bound the same way action_packets
are. `canonical` is stdlib-only and is imported by the Org Record Service too,
so both sides compute the same hash from the same rules.
"""
