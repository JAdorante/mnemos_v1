"""Speaker awareness — staged, and adaptive to the acoustic environment (#4).

Stage 1 (anonymous diarization): every utterance is embedded with SpeechBrain's
ECAPA-TDNN model and online-clustered by cosine similarity into "Speaker 1",
"Speaker 2", ... — no enrollment needed.

Stage 2 (named voiceprints): enroll a known person from a sample; at label time
we first check enrolled voiceprints and, if one is close enough, use that name.

Stage 3 (#4 — adaptive, not one global threshold): a single cosine threshold is
demo-fragile because ECAPA embeddings degrade differently per environment. So we
    (a) infer an ENVIRONMENT PROFILE from the pre-ASR audio_quality signals
        (close_mic / quiet_room / laptop_fan / far_field / noisy_room / clipping),
    (b) adapt the accept / cluster / margin thresholds per profile, and learn a
        bounded per-profile cluster offset online (label-free), and
    (c) return a three-tier DECISION with the evidence to judge it:
        {label, name, is_known, confidence, decision, second_best, margin,
         candidate, environment_profile, similarity}
      decision: accepted (named, strong margin) | clustered (anonymous, maybe with
      a candidate name hint) | new (a fresh speaker) | unknown.

The decision logic lives in `identify_embedding(emb, aq)` — pure w.r.t. the model,
so it's unit-testable with synthetic embeddings (no ECAPA load).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np

from app.config import settings


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))  # inputs are L2-normalized


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# Acoustic environment inferred from the audio_quality dict (#1). Different
# environments weaken ECAPA differently, so thresholds key off this. The cutoffs
# live in settings.speaker_env (env-overridable / #B4 auto-calibratable) so the
# same code adapts to any machine's mic/rooms rather than this developer's.
def classify_environment(aq: dict | None) -> str:
    if not aq:
        return "unknown_env"
    snr = aq.get("snr_est")
    rms = aq.get("rms", 0.0) or 0.0
    clip = aq.get("clipping_pct", 0.0) or 0.0
    if snr is None:
        return "unknown_env"
    env = settings.speaker_env
    if clip > env.clip_pct:
        return "clipping"                 # distorted — embeddings unreliable
    if snr < env.noisy_snr:
        return "noisy_room"               # café / background chatter
    if snr < env.farfield_snr:
        return "far_field" if rms < env.farfield_rms else "laptop_fan"
    return "close_mic" if rms >= env.close_rms else "quiet_room"


# Per-profile threshold deltas (d_id, d_cluster, d_margin) on the base config.
# Degraded acoustics -> lower the accept bar (same-speaker similarity drops) BUT
# demand a bigger margin to guard against false-accepts; clean -> a touch stricter.
# These defaults were tuned to one developer's rooms; an optional JSON overlay
# (QUILL_SPK_PROFILE_ADJUST, a {profile: [d_id, d_cluster, d_margin]} map) lets any
# machine retune them without a code edit. Unknown/missing profiles keep the default.
_DEFAULT_PROFILE_ADJUST = {
    "close_mic":   (+0.04, +0.03, +0.00),
    "quiet_room":  (+0.00, +0.00, +0.00),
    "laptop_fan":  (-0.03, -0.02, +0.03),
    "far_field":   (-0.06, -0.04, +0.05),
    "noisy_room":  (-0.06, -0.04, +0.06),
    "clipping":    (-0.05, -0.03, +0.05),
    "unknown_env": (+0.00, +0.00, +0.00),
}


def _load_profile_adjust() -> dict:
    """Default deltas, overlaid by QUILL_SPK_PROFILE_ADJUST if set. Fails safe to
    the defaults on any parse problem (a bad overlay never breaks speaker ID)."""
    import json
    import os
    table = {k: tuple(v) for k, v in _DEFAULT_PROFILE_ADJUST.items()}
    raw = os.environ.get("QUILL_SPK_PROFILE_ADJUST")
    if not raw:
        return table
    try:
        path = Path(raw)
        data = json.loads(path.read_text(encoding="utf-8") if path.is_file() else raw)
        for prof, deltas in (data or {}).items():
            if isinstance(deltas, (list, tuple)) and len(deltas) == 3:
                table[prof] = (float(deltas[0]), float(deltas[1]), float(deltas[2]))
    except Exception as exc:
        print(f"[speakers] profile-adjust overlay ignored ({exc}).")
    return table


_PROFILE_ADJUST = _load_profile_adjust()
_ALL_PROFILES = tuple(_PROFILE_ADJUST)


class SpeakerIdentifier:
    def __init__(
        self,
        cluster_threshold: float | None = None,
        id_threshold: float | None = None,
        voiceprint_dir: Path | None = None,
    ) -> None:
        cfg = settings.speakers
        self.cluster_threshold = (
            cluster_threshold if cluster_threshold is not None else cfg.cluster_threshold)
        self.id_threshold = id_threshold if id_threshold is not None else cfg.id_threshold
        self.hint_threshold = cfg.hint_threshold
        self.min_margin = cfg.min_margin
        self.adaptive = cfg.adaptive
        self.model_id = cfg.model
        # Env-adapted-threshold clamps + online-adaptation bounds (all overridable).
        self._id_clamp = (cfg.id_clamp_lo, cfg.id_clamp_hi)
        self._cluster_clamp = (cfg.cluster_clamp_lo, cfg.cluster_clamp_hi)
        self._adapt = dict(min_n=cfg.adapt_min_n, bound=cfg.adapt_bound,
                           step=cfg.adapt_step, hi=cfg.adapt_hi, lo=cfg.adapt_lo)
        self.dir = Path(voiceprint_dir or cfg.voiceprint_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._model = None
        # anonymous clusters: label -> {"centroid": vec, "count": n}
        self._clusters: dict[str, dict] = {}
        self._next_id = 1
        # Isolated remote-channel clusters (MeetingSession loopback).
        self._remote_clusters: dict[str, dict] = {}
        self._remote_next_id = 1
        # named voiceprints: name -> centroid vec
        self._voiceprints: dict[str, np.ndarray] = {}
        # per-profile learned state + running stats (observability + calibration)
        self._profiles: dict[str, dict] = {}
        self._stats_dirty = 0
        self._load_voiceprints()
        self._load_profiles()

    # ------------------------------ model --------------------------------
    def _load(self):
        if self._model is None:
            from speechbrain.inference.speaker import EncoderClassifier

            # Copy model files instead of symlinking — Windows blocks symlinks
            # without admin / developer mode.
            try:
                from speechbrain.utils.fetching import LocalStrategy

                local_strategy = LocalStrategy.COPY
            except Exception:
                local_strategy = None

            print(f"[speakers] loading speaker embedder ({self.model_id}) ...")
            kwargs = dict(
                source=self.model_id,
                savedir=str(self.dir / "_ecapa_model"),
            )
            if local_strategy is not None:
                kwargs["local_strategy"] = local_strategy
            self._model = EncoderClassifier.from_hparams(**kwargs)
            print("[speakers] embedder ready.")
        return self._model

    def embed(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """float32 mono @ sample_rate -> L2-normalized 192-d embedding."""
        import torch

        model = self._load()
        if sample_rate != 16_000:
            raise ValueError("ECAPA expects 16 kHz audio")
        wav = torch.from_numpy(np.ascontiguousarray(audio)).float().unsqueeze(0)
        with torch.no_grad():
            emb = model.encode_batch(wav).squeeze().cpu().numpy()
        norm = np.linalg.norm(emb)
        return emb / norm if norm > 0 else emb

    # ------------------------------ voiceprints --------------------------
    def _load_voiceprints(self) -> None:
        index = self.dir / "voiceprints.json"
        if not index.exists():
            return
        try:
            names = json.loads(index.read_text())
        except Exception:
            return
        for name in names:
            vec = self.dir / f"{name}.npy"
            if vec.exists():
                self._voiceprints[name] = np.load(vec)
        if self._voiceprints:
            print(f"[speakers] loaded {len(self._voiceprints)} voiceprint(s): "
                  f"{', '.join(self._voiceprints)}")

    def _save_voiceprints(self) -> None:
        (self.dir / "voiceprints.json").write_text(json.dumps(list(self._voiceprints)))

    def enroll(self, name: str, audio: np.ndarray, sample_rate: int) -> None:
        """Register (or update) a named person's voiceprint from a sample.

        Averaging with any existing voiceprint makes it more robust over time.
        """
        emb = self.embed(audio, sample_rate)
        with self._lock:
            if name in self._voiceprints:
                merged = self._voiceprints[name] + emb
                emb = merged / np.linalg.norm(merged)
            self._voiceprints[name] = emb
            np.save(self.dir / f"{name}.npy", emb)
            self._save_voiceprints()
        print(f"[speakers] enrolled '{name}'.")

    def enrolled_names(self) -> list[str]:
        with self._lock:
            return list(self._voiceprints)

    # ------------------------------ per-profile calibration --------------
    def _load_profiles(self) -> None:
        path = self.dir / "profile_stats.json"
        if path.exists():
            try:
                self._profiles = json.loads(path.read_text())
            except Exception:
                self._profiles = {}
        for p in _ALL_PROFILES:
            self._profiles.setdefault(p, {
                "n": 0, "offset": 0.0, "sim_ema": None, "margin_ema": None,
                "headroom_ema": None, "accepted": 0, "clustered": 0,
                "new": 0, "unknown": 0})

    def _save_profiles(self) -> None:
        try:
            (self.dir / "profile_stats.json").write_text(json.dumps(self._profiles))
        except Exception as exc:
            print(f"[speakers] profile stats save error: {exc}")

    def _eff_thresholds(self, profile: str):
        d_id, d_cl, d_mg = _PROFILE_ADJUST.get(profile, (0.0, 0.0, 0.0))
        learned = self._profiles.get(profile, {}).get("offset", 0.0) if self.adaptive else 0.0
        eff_id = _clamp(self.id_threshold + d_id, *self._id_clamp)
        eff_cluster = _clamp(self.cluster_threshold + d_cl + learned, *self._cluster_clamp)
        eff_margin = max(0.0, self.min_margin + d_mg)
        return eff_id, eff_cluster, eff_margin

    @staticmethod
    def _ema(prev, x, a=0.1):
        return x if prev is None else (1 - a) * prev + a * x

    def _update_profile(self, profile: str, decision: str, confidence: float,
                        margin: float, headroom: float | None):
        st = self._profiles.setdefault(profile, {
            "n": 0, "offset": 0.0, "sim_ema": None, "margin_ema": None,
            "headroom_ema": None, "accepted": 0, "clustered": 0,
            "new": 0, "unknown": 0})
        st["n"] += 1
        st[decision] = st.get(decision, 0) + 1
        st["sim_ema"] = round(self._ema(st["sim_ema"], confidence), 4)
        if margin is not None:
            st["margin_ema"] = round(self._ema(st["margin_ema"], margin), 4)
        # Learn the cluster offset from re-match headroom (label-free): if repeat
        # matches sit comfortably above threshold, tighten a hair; if they barely
        # clear it (we're over-splitting speakers), loosen. Bounds/steps are config.
        if self.adaptive and headroom is not None:
            st["headroom_ema"] = round(self._ema(st["headroom_ema"], headroom), 4)
            h = st["headroom_ema"]
            a = self._adapt
            if st["n"] >= a["min_n"]:
                if h > a["hi"]:
                    st["offset"] = round(_clamp(st["offset"] + a["step"], -a["bound"], a["bound"]), 4)
                elif h < a["lo"]:
                    st["offset"] = round(_clamp(st["offset"] - a["step"], -a["bound"], a["bound"]), 4)
        self._stats_dirty += 1
        if self._stats_dirty >= 10:
            self._save_profiles()
            self._stats_dirty = 0

    def profile_report(self) -> dict:
        with self._lock:
            return {p: dict(self._profiles.get(p, {})) for p in _ALL_PROFILES}

    # ------------------------------ identify -----------------------------
    def identify(self, audio: np.ndarray, sample_rate: int, aq: dict | None = None,
                 space: str = "default") -> dict:
        """Embed an utterance and classify the speaker with environment-adaptive
        thresholds. `aq` is the audio_quality dict (#1) used to pick the profile.

        ``space``: default (ambient mix), ``self`` (mic during a MeetingSession —
        skip clustering), ``remote`` (loopback — cluster only among remotes).
        """
        if space == "self":
            return self._self_channel_result()
        emb = self.embed(audio, sample_rate)
        return self.identify_embedding(emb, aq, space=space)

    def _self_channel_result(self) -> dict:
        name = "You"
        try:
            from app.services.identity import user_identity
            n = (user_identity().get("name") or "").strip()
            if n:
                name = n
        except Exception:
            pass
        return {
            "label": name, "name": name, "is_known": True,
            "similarity": 1.0, "confidence": 1.0,
            "decision": "self_channel", "margin": 1.0,
            "second_best": None, "candidate": None,
            "environment_profile": "close_mic",
            "thresholds": {"id": 1.0, "cluster": 1.0, "margin": 0.0},
        }

    def identify_embedding(self, emb: np.ndarray, aq: dict | None = None,
                           space: str = "default") -> dict:
        """Pure decision logic over a precomputed embedding (model-free -> testable).
        Mutates cluster state / profile stats; returns the rich result dict."""
        if space == "self":
            return self._self_channel_result()
        profile = classify_environment(aq)
        eff_id, eff_cluster, eff_margin = self._eff_thresholds(profile)

        with self._lock:
            # rank enrolled voiceprints
            names = sorted(((n, _cos(emb, vp)) for n, vp in self._voiceprints.items()),
                           key=lambda t: t[1], reverse=True)
            best_name, best_name_sim = (names[0] if names else (None, -1.0))
            second_name = (names[1] if len(names) > 1 else (None, -1.0))

            clusters_map = (self._remote_clusters if space == "remote"
                            else self._clusters)
            next_attr = "_remote_next_id" if space == "remote" else "_next_id"

            # rank anonymous clusters
            clusters = sorted(((lb, _cos(emb, c["centroid"]))
                               for lb, c in clusters_map.items()),
                              key=lambda t: t[1], reverse=True)
            best_cl, best_cl_sim = (clusters[0] if clusters else (None, -1.0))

            # the strongest competing identity (for the margin)
            competitor_sim = max(second_name[1], best_cl_sim)
            margin = round(best_name_sim - competitor_sim, 3) if best_name else 0.0
            confidence = round(max(best_name_sim, best_cl_sim, 0.0), 3)

            second_best = None
            if second_name[0] is not None and second_name[1] >= best_cl_sim:
                second_best = {"label": second_name[0], "similarity": round(second_name[1], 3)}
            elif best_cl is not None:
                second_best = {"label": best_cl, "similarity": round(best_cl_sim, 3)}

            candidate = None
            headroom = None

            # Tier 1: confident, well-separated name -> accept.
            if best_name is not None and best_name_sim >= eff_id and margin >= eff_margin:
                decision, label, name, is_known = "accepted", best_name, best_name, True
                confidence = round(best_name_sim, 3)
            else:
                # Not confidently named. Attach a candidate hint if we're near a
                # known voice, then cluster anonymously (tier 2 / 3).
                if best_name is not None and best_name_sim >= self.hint_threshold:
                    candidate = {"name": best_name, "similarity": round(best_name_sim, 3)}
                if best_cl is not None and best_cl_sim >= eff_cluster:
                    # re-match an existing cluster
                    c = clusters_map[best_cl]
                    n = c["count"]
                    merged = c["centroid"] * n + emb
                    c["centroid"] = merged / np.linalg.norm(merged)
                    c["count"] = n + 1
                    decision, label, name, is_known = "clustered", best_cl, None, False
                    confidence = round(best_cl_sim, 3)
                    headroom = best_cl_sim - eff_cluster
                else:
                    # brand-new anonymous speaker
                    nid = getattr(self, next_attr)
                    label = (f"Remote {nid}" if space == "remote"
                             else f"Speaker {nid}")
                    setattr(self, next_attr, nid + 1)
                    clusters_map[label] = {"centroid": emb, "count": 1}
                    decision, name, is_known = "new", None, False
                    confidence = 1.0 if not clusters else round(max(best_cl_sim, 0.0), 3)

            self._update_profile(profile, decision, confidence, margin, headroom)

            return {
                "label": label, "name": name, "is_known": is_known,
                "similarity": confidence,          # back-compat alias
                "confidence": confidence,
                "decision": decision,
                "margin": margin,
                "second_best": second_best,
                "candidate": candidate,
                "environment_profile": profile,
                "thresholds": {"id": round(eff_id, 3), "cluster": round(eff_cluster, 3),
                               "margin": round(eff_margin, 3)},
            }


speakers = SpeakerIdentifier()
