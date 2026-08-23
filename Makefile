# Mnemos / nexus_v1 — local + CI targets
# Prefer the project venv when present.
PYTHON ?= python

.PHONY: bench-latency bench-latency-cold latency-baseline
.PHONY: eval eval-live eval-people eval-people-live eval-grounding eval-planner eval-noise golden-commitments golden-entity-resolution golden-contact-attribution

# Plan 2.2 + 2.3 + 2.4 + 3.3 + 5.2: golden thresholds (offline, no API key).
eval: golden-commitments golden-entity-resolution golden-contact-attribution
	$(PYTHON) scripts/eval_commitments_ownership.py
	$(PYTHON) scripts/eval_entity_resolution.py
	$(PYTHON) scripts/eval_contact_attribution.py
	$(PYTHON) scripts/eval_grounding.py
	$(PYTHON) scripts/eval_planner_routing.py
	$(PYTHON) scripts/eval_people_noise.py

# People v3 WS-G — noise metrics on the noisy corpus. Report-only in `eval`
# (P0: numbers before knobs); run with --gate once P3/P4 flags are on.
eval-noise:
	$(PYTHON) scripts/eval_people_noise.py --gate

# Optional live LLM pass for commitments/ownership (needs API key).
# Not the People-v2 gate — use eval-people / eval-people-live for that.
eval-live: golden-commitments
	$(PYTHON) scripts/eval_commitments_ownership.py --live

# People goldens (entity resolution + contact attribution).
eval-people: golden-entity-resolution golden-contact-attribution
	$(PYTHON) scripts/eval_entity_resolution.py
	$(PYTHON) scripts/eval_contact_attribution.py

# People v2 live smoke against local quill.db (no LLM; informational).
eval-people-live:
	$(PYTHON) scripts/eval_people_live.py

# Plan 3.3 — query-type grounding routes (Golden #4).
eval-grounding:
	$(PYTHON) scripts/eval_grounding.py

# Plan 5.2 — planner graduation (core gate + global default + multi-step packets).
eval-planner:
	$(PYTHON) scripts/eval_planner_routing.py

golden-commitments:
	$(PYTHON) scripts/gen_commitments_ownership_golden.py

golden-entity-resolution:
	$(PYTHON) scripts/gen_entity_resolution_golden.py

golden-contact-attribution:
	$(PYTHON) scripts/gen_contact_attribution_golden.py

# --- latency program ----------------------------------------------------------
# The acceptance gate for every phase: run it before and after a change and
# compare. Never makes a cloud call.
bench-latency:
	$(PYTHON) scripts/bench_latency.py --rounds 10 \
	  --baseline data/latency_baseline.json

# Cold-start tax: unloads the model between calls. Slow on purpose.
bench-latency-cold:
	$(PYTHON) scripts/bench_latency.py --rounds 5 --cold

# Freeze the current numbers as the comparison point for later phases.
latency-baseline:
	$(PYTHON) scripts/bench_latency.py --rounds 10 \
	  --json data/latency_baseline.json
