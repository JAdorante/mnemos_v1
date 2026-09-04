/* Web Perceive Phase 4 — client-side Silero VAD worker.
 *
 * Mirrors the server pipeline exactly so segmentation cannot drift:
 *  - model I/O matches silero_vad's OnnxWrapper: input = [64-sample context ||
 *    512-sample frame] -> [1,576] float32, state [2,1,128], sr int64 16000;
 *    context = last 64 samples of each input after the run.
 *  - hysteresis matches VADIterator: trigger at prob >= threshold, end after
 *    min_silence_ms of prob < threshold - 0.15; thresholds come from
 *    GET /capture/config so client and server never disagree.
 *  - utterance shape matches AudioPipeline.feed(): collect from the trigger
 *    frame through the end-event frame (trailing hysteresis silence included),
 *    force-cut at max_utterance_s staying in-speech.
 *
 * Result: only detected speech leaves the device. Frames arrive from the
 * capture page as 512-sample Int16 buffers; utterances go back as one Int16
 * buffer + timestamps for the WS "utterance" header.
 *
 * onnxruntime-web is loaded from /static/ort/ when the deployment bakes it in
 * (see deploy/hosted/), else from the pinned CDN. If neither loads, the page
 * falls back to server-side VAD — capture never breaks, only the privacy rung
 * steps down.
 */
"use strict";

const FRAME = 512;
const CTX = 64;
const SR = 16000;

let session = null;
let cfg = null;
let thr = 0.5, minSilenceSamples = 8000, maxUtterSamples = 0;

// OnnxWrapper state
let state = new Float32Array(2 * 1 * 128);
let context = new Float32Array(CTX);

// VADIterator state
let triggered = false;
let tempEnd = 0;
let currentSample = 0;

// Collection state (mirrors AudioPipeline._buffer/_in_speech)
let inSpeech = false;
let collected = [];        // Int16Array frames
let collectedSamples = 0;
let speechStartTs = 0;

// Frames must be inferred in order; ort runs are async, so serialize.
const queue = [];
let processing = false;

function resetVad() {
  state = new Float32Array(2 * 1 * 128);
  context = new Float32Array(CTX);
  triggered = false;
  tempEnd = 0;
  currentSample = 0;
  inSpeech = false;
  collected = [];
  collectedSamples = 0;
}

async function init(msg) {
  cfg = msg.cfg;
  thr = cfg.vad_threshold;
  minSilenceSamples = SR * cfg.min_silence_ms / 1000;
  maxUtterSamples = cfg.max_utterance_s > 0
    ? Math.floor(cfg.max_utterance_s * SR) : 0;
  let loadedFrom = null;
  for (const base of msg.ortBases) {
    try {
      importScripts(base + "ort.min.js");
      loadedFrom = base;
      break;
    } catch (e) { /* next source */ }
  }
  if (loadedFrom === null || typeof self.ort === "undefined") {
    postMessage({ type: "unavailable", reason: "onnxruntime-web not loadable" });
    return;
  }
  ort.env.wasm.wasmPaths = loadedFrom;
  ort.env.wasm.numThreads = 1;   // no COOP/COEP; single-thread wasm works everywhere
  try {
    const model = await (await fetch(msg.modelUrl)).arrayBuffer();
    session = await ort.InferenceSession.create(model, {
      executionProviders: ["wasm"],
    });
    // One dry run so the first real frame isn't the compile hit.
    await infer(new Float32Array(FRAME));
    resetVad();
    postMessage({ type: "ready" });
  } catch (e) {
    postMessage({ type: "unavailable", reason: String(e && e.message || e) });
  }
}

async function infer(frameF32) {
  const input = new Float32Array(CTX + FRAME);
  input.set(context, 0);
  input.set(frameF32, CTX);
  const feeds = {
    input: new ort.Tensor("float32", input, [1, CTX + FRAME]),
    state: new ort.Tensor("float32", state, [2, 1, 128]),
    sr: new ort.Tensor("int64", BigInt64Array.from([BigInt(SR)]), []),
  };
  const out = await session.run(feeds);
  state = out.stateN.data instanceof Float32Array
    ? out.stateN.data : Float32Array.from(out.stateN.data);
  context = input.subarray(input.length - CTX);
  return out.output.data[0];
}

// VADIterator.__call__, minus the model call (prob comes in).
function vadStep(prob) {
  currentSample += FRAME;
  if (prob >= thr && tempEnd) tempEnd = 0;
  if (prob >= thr && !triggered) {
    triggered = true;
    return "start";
  }
  if (prob < thr - 0.15 && triggered) {
    if (!tempEnd) tempEnd = currentSample;
    if (currentSample - tempEnd < minSilenceSamples) return null;
    tempEnd = 0;
    triggered = false;
    return "end";
  }
  return null;
}

function emitUtterance(endTs) {
  const pcm = new Int16Array(collectedSamples);
  let o = 0;
  for (const f of collected) { pcm.set(f, o); o += f.length; }
  collected = [];
  collectedSamples = 0;
  postMessage({
    type: "utterance",
    start_ts: speechStartTs,
    end_ts: endTs,
    pcm: pcm.buffer,
  }, [pcm.buffer]);
}

async function processFrame(frame) {           // frame: Int16Array(512)
  const f32 = new Float32Array(FRAME);
  for (let i = 0; i < FRAME; i++) f32[i] = frame[i] / 32768;
  const prob = await infer(f32);
  const evt = vadStep(prob);

  // Mirror feed(): append while in-speech BEFORE handling start/end, so the
  // end-event frame is included and the start frame becomes the first buffer.
  if (inSpeech) {
    collected.push(frame);
    collectedSamples += frame.length;
    if (maxUtterSamples && collectedSamples >= maxUtterSamples) {
      emitUtterance(Date.now() / 1000);       // force-cut; stay in-speech
      speechStartTs = Date.now() / 1000;
    }
  }
  if (evt === "start") {
    inSpeech = true;
    speechStartTs = Date.now() / 1000;
    collected = [frame];
    collectedSamples = frame.length;
  } else if (evt === "end" && inSpeech) {
    inSpeech = false;
    emitUtterance(Date.now() / 1000);
  }
}

async function pump() {
  if (processing) return;
  processing = true;
  while (queue.length) {
    try {
      await processFrame(queue.shift());
    } catch (e) {
      postMessage({ type: "unavailable", reason: String(e && e.message || e) });
      queue.length = 0;
    }
  }
  processing = false;
}

self.onmessage = (e) => {
  const m = e.data;
  if (m.type === "init") { init(m); return; }
  if (m.type === "frame") {
    if (session === null) return;
    queue.push(new Int16Array(m.pcm));
    pump();
    return;
  }
  if (m.type === "flush") {
    // Source stopped mid-speech: ship what we hold (server flush() twin).
    if (inSpeech && collectedSamples) {
      inSpeech = false;
      emitUtterance(Date.now() / 1000);
    }
    resetVad();
    postMessage({ type: "flushed" });
  }
};
