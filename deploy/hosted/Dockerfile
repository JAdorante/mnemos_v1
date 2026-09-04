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
RUN pip install --no-cache-dir -r requirements.txt

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
COPY run_all.py .

# Headless: browser is the only capture surface; bind beyond loopback so the
# reverse proxy can reach us; the LAN gate token is REQUIRED (mint one per
# user or let boot write data/.api_token into the volume).
ENV QUILL_HEADLESS=1 \
    QUILL_HOST=0.0.0.0 \
    QUILL_PORT=8000 \
    QUILL_ASR_WARMUP=1 \
    QUILL_FIRST_RUN_MODE=full \
    PYTHONUNBUFFERED=1
# QUILL_FIRST_RUN_MODE=full is load-bearing: the desktop tester profile's
# meeting-first mode would silently drop ALL web-mic capture outside calendar
# windows (see README "Do NOT ship meeting-first mode to hosted instances").

# Per-user state lives here — one named volume per user container.
VOLUME /srv/sparrow/data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
