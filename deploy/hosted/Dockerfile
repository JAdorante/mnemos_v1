# Sparrow hosted (Boost Run) instance — Web Perceive Phase 3.
#
# One container per user. QUILL_HEADLESS=1 means no local devices ever open;
# capture arrives from the user's browser over WS /ingest/audio (see
# app/api/web_ingest.py). Build from the repo root:
#
#   docker build -f deploy/hosted/Dockerfile -t sparrow-hosted .
#
# GPU ASR: run with the NVIDIA runtime and set QUILL_WHISPER_DEVICE=cuda,
# QUILL_WHISPER_COMPUTE=float16, QUILL_WHISPER_MODEL=large-v3 (or
# distil-large-v3). CTranslate2 needs the CUDA libs — swap the base image for
# nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 + python3.12 when doing that.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/sparrow

COPY requirements.txt .
# Device-capture libs (sounddevice/soundcard/opencv/pyautogui...) stay listed —
# they import lazily and never load under QUILL_HEADLESS=1 (verified: zero
# device libs at routes import). Trimming them is an image-size optimization,
# not a correctness need.
# pynput drags in evdev, which compiles against kernel headers absent from the
# slim image — and no input observer ever starts under QUILL_HEADLESS=1.
RUN grep -v '^pynput' requirements.txt > /tmp/requirements.hosted.txt \
    && pip install --no-cache-dir -r /tmp/requirements.hosted.txt

# Chromium for web goals (playwright itself comes from requirements.txt).
# --with-deps pulls the shared libs the slim image lacks; one browser only.
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Bake onnxruntime-web so the /capture page's client-side VAD (Phase 4) is
# self-contained — no CDN dependency at runtime. Version must match ORT_BASES
# in app/api/capture_page.py.
ARG ORT_WEB_VERSION=1.19.2
RUN mkdir -p app/static/ort && cd app/static/ort && \
    base="https://cdn.jsdelivr.net/npm/onnxruntime-web@${ORT_WEB_VERSION}/dist" && \
    for f in ort.min.js ort-wasm-simd-threaded.wasm ort-wasm-simd-threaded.mjs \
             ort-wasm-simd-threaded.jsep.wasm ort-wasm-simd-threaded.jsep.mjs; do \
        curl -fsSL -o "$f" "$base/$f" || true; \
    done && ls -la

COPY app ./app
# The chat lane constructs browser_agent.orchestrator.Agent even when no web
# goal ever runs (fast-lane answers come from it too) — without the module the
# worker dies at boot and every chat turn is rejected. The browser stays lazy:
# Chromium (installed above) only launches on the first routed web goal.
COPY browser_agent ./browser_agent
COPY run_all.py .
# The source-policy table ships with the repo but /srv/sparrow/data is a
# per-user volume that starts empty; without the file the app runs on the
# restrictive fallback (no people/commitment minting, no contacts). Stage it
# in the image and seed the volume at boot (cp -n: never clobber a live copy).
COPY data/source_policies.json ./seed/source_policies.json

# Headless: browser is the only capture surface; bind beyond loopback so the
# reverse proxy can reach us; the LAN gate token is REQUIRED (mint one per
# user or let boot write data/.api_token into the volume).
ENV QUILL_HEADLESS=1 \
    QUILL_HOST=0.0.0.0 \
    QUILL_PORT=8000 \
    QUILL_ASR_WARMUP=1 \
    QUILL_FIRST_RUN_MODE=full \
    QUILL_CREDENTIALS_FILE=/srv/sparrow/data/.credentials.env \
    QUILL_AGENT_HEADLESS=1 \
    QUILL_GHOST_BROWSER=headless \
    QUILL_BROWSER_NO_SANDBOX=1 \
    AGENT_DATA_DIR=/srv/sparrow/data/agent_sessions \
    PYTHONUNBUFFERED=1
# Web-goal posture: no display in the container, so the agent browser runs
# headless with its view streamed to the chat ghost pane; --no-sandbox because
# the container runs as root (isolation comes from the container boundary);
# agent sessions + login profiles persist in the per-user volume. Web goals
# additionally need cloud credentials (ANTHROPIC_API_KEY, or a key pasted in
# onboarding) — without one the router keeps every request in local-only chat.
# QUILL_CREDENTIALS_FILE must live in the volume: the guided iCloud connect and
# pasted LLM keys both persist there, and the default (/srv/sparrow) is wiped
# on every image rebuild — testers would silently lose their credentials.
# QUILL_FIRST_RUN_MODE=full is load-bearing: the desktop tester profile's
# meeting-first mode would silently drop ALL web-mic capture outside calendar
# windows (see README "Do NOT ship meeting-first mode to hosted instances").

# Per-user state lives here — one named volume per user container.
VOLUME /srv/sparrow/data

EXPOSE 8000
# cp -f, not -n: the policy table is SHIPPED CONFIGURATION (wipe.py KEEP_NAMES
# rationale — byte-identical on every install, no personal data), so every
# boot syncs the volume to the image's version; -n would pin old policy
# forever on existing volumes.
CMD ["sh", "-c", "mkdir -p data && cp -f seed/source_policies.json data/source_policies.json; exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
