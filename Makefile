# Mnemos / nexus_v1 — local + CI targets
# Prefer the project venv when present.
PYTHON ?= python

.PHONY: bench-latency bench-latency-cold latency-baseline
.PHONY: eval-asr eval-asr-check eval-asr-smoke eval-asr-bootstrap eval-asr-baseline
.PHONY: listen-idle listen-idle-baseline
.PHONY: asr-calibrate asr-calibration
.PHONY: eval eval-live eval-people eval-people-live eval-grounding eval-planner eval-noise eval-context eval-ideas golden-commitments golden-entity-resolution golden-contact-attribution

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

# Ambient-context attribution flip-on gates (live LLM; needs an API key).
# eval-context gates QUILL_EXTRACT_CONTEXT=1 (no precision/faithfulness
# regression + >=20% attribution gain); eval-ideas gates QUILL_EXTRACT_IDEAS=1
# (idea precision >= 0.8, zero task/commitment double-emission).
CTX_GOLDEN := tests/fixtures/goldens/extraction_context_ideas.jsonl
eval-context:
	$(PYTHON) scripts/eval_extraction.py --data $(CTX_GOLDEN) --context-gate
eval-ideas:
	$(PYTHON) scripts/eval_extraction.py --data $(CTX_GOLDEN) --ideas-gate

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

# --- perception: idle footprint -----------------------------------------------
# What listening costs while nobody speaks — the baseline Phase B's "≤ half of
# baseline" is measured against. Paced to real time on purpose; --fast reports
# a CPU share several times too low. Freeze it on the reference machine:
#   make listen-idle-baseline
listen-idle:
	$(PYTHON) scripts/bench_listen_idle.py --seconds 30 \
	  --baseline data/listen_idle_baseline.json

listen-idle-baseline:
	$(PYTHON) scripts/bench_listen_idle.py --seconds 30 \
	  --json data/listen_idle_baseline.json

# --- perception: ASR engine acceptance ----------------------------------------
# The gate for every engine swap (Whisper -> Parakeet and anything after it).
# Real fixtures live in tests/fixtures/asr_eval/ — see the README there; the
# clips are not in git, so `eval-asr-bootstrap` first on a fresh checkout.
eval-asr-bootstrap:
	$(PYTHON) scripts/eval_asr.py bootstrap

# Cheap and fast — run it before a long scoring run, and in CI on any PR
# touching app/services/audio*.py or the engine seam.
eval-asr-check:
	$(PYTHON) scripts/eval_asr.py check

# The CI gate (.github/workflows/perception.yml): audio with no speech in it
# must not become a memory. Loads a real engine; needs no recorded fixtures.
eval-asr-smoke:
	$(PYTHON) scripts/eval_asr.py smoke

# Score one engine: `make eval-asr ASR_ENGINE=parakeet-onnx`.
ASR_ENGINE ?= whisper
eval-asr: eval-asr-check
	$(PYTHON) scripts/eval_asr.py run --engine $(ASR_ENGINE) --speakers

# Freeze the incumbent as the comparison point. Every later engine is scored
# against this file, on the same machine, or the numbers mean nothing.
eval-asr-baseline:
	$(PYTHON) scripts/eval_asr.py run --tag whisper-baseline --speakers \
	  -o data/eval/asr/report_whisper-baseline.json

# Fit a new engine's ingest thresholds against the incumbent's, so a different
# confidence scale cannot quietly change what becomes a memory (§3.3). Dry-run
# by default: read the operating-point comparison, then re-run with --write.
#   make asr-calibrate REF=data/eval/asr/report_whisper-baseline.json \
#                      CAND=data/eval/asr/report_parakeet.json
REF ?= data/eval/asr/report_whisper-baseline.json
CAND ?=
asr-calibrate:
	@test -n "$(CAND)" || (echo "set CAND=<candidate report.json>"; exit 2)
	$(PYTHON) scripts/calibrate_asr_confidence.py fit $(REF) $(CAND)

# What every engine is currently judged on.
asr-calibration:
	$(PYTHON) scripts/calibrate_asr_confidence.py show
