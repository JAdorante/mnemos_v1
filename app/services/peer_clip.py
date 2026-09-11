"""Cross-tenant clip playback grants (Phase 4.1/4.3).

Everything else in the peer channel moves TEXT that this instance composed.
This moves a raw recording — the actual moment a claim came from — to another
tenant. That is a categorically larger disclosure, so it gets its own
credential rather than riding on the pairing token:

  * a grant is minted only when a HUMAN approves an answer, never by policy.
    A pack that says "auto-answer my work questions" is consent to answer
    questions, not consent to hand over recordings;
  * it is scoped to the exact source events behind the claims in THAT answer.
    Presenting it for any other event fails, so it is a capability, not a
    standing cross-tenant read grant;
  * it records the verbatim SPAN each claim rests on, and only that span is
    ever sent. A captured moment routinely contains other speakers and
    adjacent conversation; approving a claim is not approving everything that
    happened to share a recording with it. Exact when the capture kept word
    timestamps (`QUILL_ASR_WORD_TIMESTAMPS=1`), estimated from the position in
    the transcript otherwise, and refused outright when neither locates it;
  * it is short-lived, and it dies with the ask, the pairing, or a revoke;
  * it is stored hash-only, the same posture as the peer token it rides
    beside — the plaintext exists once, in the answer payload.

A grant is still only half the check. `/peer/clip` also requires the peer's
normal Bearer token, and the grant must have been issued to that same peer, so
a leaked grant is useless without the pairing credential and vice versa.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from app.config import settings

_lock = threading.Lock()

# Long enough for a teammate to click play on an answer they are reading, short
# enough that a captured payload is not a lasting key to someone's recordings.
DEFAULT_TTL_S = 3600.0

# A grant may cover at most this many events — the claims behind one answer,
# not a session.
MAX_SCOPE = 12


def _path() -> Path:
    return Path(settings.peer.clip_grants_path)


def _hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _load() -> list[dict]:
    p = _path()
    if not p.is_file():
        return []
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except Exception as exc:
        print(f"[peer_clip] grant load failed ({exc}).")
        return []


def _save(rows: list[dict]) -> None:
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception as exc:
        print(f"[peer_clip] grant save failed ({exc}).")


def enabled() -> bool:
    return bool(settings.peer.enabled and settings.peer.clip_playback)


def mint(peer_id: str, ask_id: str, events: list, *,
         ttl_s: float | None = None, now: float | None = None) -> dict | None:
    """Issue one scoped, expiring grant. Returns {token, expires_at, events}.

    `events` is either bare event ids or {"event_id", "span"} records. The span
    is the verbatim quote the claim rests on, and carrying it here is what
    makes the grant "you may hear the part of this recording that backs the
    claim I approved" rather than "you may hear this recording". A captured
    moment routinely contains other speakers and adjacent conversation that
    the approver never meant to disclose.

    Returns None when playback is off or the answer rests on no replayable
    event — an answer with nothing to play must not mint a credential.
    """
    if not enabled():
        return None
    scope: list[int] = []
    spans: dict[str, str] = {}
    for item in events or []:
        if isinstance(item, dict):
            raw_id, span = item.get("event_id"), item.get("span") or ""
        else:
            raw_id, span = item, ""
        try:
            n = int(raw_id)
        except (TypeError, ValueError):
            continue
        if n not in scope:
            scope.append(n)
            if span:
                spans[str(n)] = str(span)[:2000]
    scope = scope[:MAX_SCOPE]
    spans = {k: v for k, v in spans.items() if int(k) in scope}
    if not scope or not peer_id or not ask_id:
        return None

    now = time.time() if now is None else now
    ttl = float(ttl_s if ttl_s is not None else settings.peer.clip_ttl_s
                or DEFAULT_TTL_S)
    token = secrets.token_urlsafe(32)
    row = {
        "token_sha256": _hash(token),
        "peer_id": str(peer_id),
        "ask_id": str(ask_id),
        "event_ids": scope,
        "spans": spans,
        "created_at": now,
        "expires_at": now + ttl,
        "revoked": False,
        "plays": 0,
    }
    with _lock:
        rows = [r for r in _load() if not _is_dead(r, now)]
        rows.append(row)
        _save(rows[-200:])
    print(f"[peer_clip] granted {len(scope)} clip(s) to {peer_id} "
          f"for ask {ask_id} ({int(ttl)}s).")
    return {"token": token, "expires_at": row["expires_at"],
            "events": list(scope)}


def _is_dead(row: dict, now: float) -> bool:
    return bool(row.get("revoked")) or float(row.get("expires_at") or 0) <= now


def check(peer_id: str, token: str, event_id: int,
          now: float | None = None) -> tuple[bool, str]:
    """(allowed, reason). Thin wrapper over `check_scope` for callers that do
    not need the approved span."""
    ok, reason, _span = check_scope(peer_id, token, event_id, now=now)
    return ok, reason


def check_scope(peer_id: str, token: str, event_id: int,
                now: float | None = None) -> tuple[bool, str, str]:
    """(allowed, reason, approved_span). Every failure returns the SAME reason.

    A caller probing this endpoint learns only "no" — never whether the token
    was real but expired, real but for a different event, or never existed.
    That distinction is exactly what turns a rejected request into an oracle
    for which recordings a tenant holds.
    """
    deny = (False, "no such clip grant", "")
    if not enabled():
        return deny
    try:
        eid = int(event_id)
    except (TypeError, ValueError):
        return deny
    digest = _hash(token or "")
    now = time.time() if now is None else now
    with _lock:
        rows = _load()
        for row in rows:
            if not secrets.compare_digest(str(row.get("token_sha256") or ""),
                                          digest):
                continue
            if _is_dead(row, now):
                return deny
            if str(row.get("peer_id") or "") != str(peer_id or ""):
                return deny
            if eid not in (row.get("event_ids") or []):
                return deny
            row["plays"] = int(row.get("plays") or 0) + 1
            row["last_played_at"] = now
            _save(rows)
            return True, "", str((row.get("spans") or {}).get(str(eid), ""))
    return deny


def revoke_for_ask(ask_id: str) -> int:
    """Kill the grants issued for one answer — 4.3's tie to the ask record."""
    return _revoke(lambda r: str(r.get("ask_id") or "") == str(ask_id or ""))


def revoke_for_peer(peer_id: str) -> int:
    """Unpairing must not leave a live key to this tenant's recordings."""
    return _revoke(lambda r: str(r.get("peer_id") or "") == str(peer_id or ""))


def _revoke(match) -> int:
    n = 0
    with _lock:
        rows = _load()
        for row in rows:
            if not row.get("revoked") and match(row):
                row["revoked"] = True
                n += 1
        if n:
            _save(rows)
    if n:
        print(f"[peer_clip] revoked {n} clip grant(s).")
    return n


def active_grants(now: float | None = None) -> list[dict]:
    """Live grants, metadata only — for the UI's "what can they still play"."""
    now = time.time() if now is None else now
    out: list[dict] = []
    for row in _load():
        if _is_dead(row, now):
            continue
        out.append({k: row.get(k) for k in
                    ("peer_id", "ask_id", "event_ids", "created_at",
                     "expires_at", "plays", "last_played_at")})
    return out


def prune(now: float | None = None) -> int:
    """Drop dead rows. Expiry is enforced at check(); this is housekeeping."""
    now = time.time() if now is None else now
    with _lock:
        rows = _load()
        live = [r for r in rows if not _is_dead(r, now)]
        if len(live) != len(rows):
            _save(live)
    return len(rows) - len(live)


def playable_path(event_id: int, store=None) -> str | None:
    """The audio file behind one event, or None when there is nothing to play.

    Confined to this tenant's data dir by the caller; resolving the path here
    keeps `evidence_playback` the single source of truth for which of the raw
    and enhanced captures is the one a human should hear.
    """
    from app.services import evidence_playback
    if store is None:
        from app.storage import get_store
        store = get_store()
    try:
        row = store.get_event(int(event_id))
    except Exception:
        return None
    if not row:
        return None
    meta = row.get("meta") if isinstance(row, dict) else None
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = None
    clip: dict[str, Any] = evidence_playback.clip_from_meta(
        meta if isinstance(meta, dict) else {})
    if not clip.get("play_path") and isinstance(row, dict):
        # Older events kept the path as a column rather than in meta.
        clip["play_path"] = row.get("audio_path") or None
    return clip.get("play_path") or None


# Breathing room either side of the approved words, so a trimmed clip does not
# start mid-syllable. Small enough that it cannot pull in a separate sentence.
SPAN_PAD_S = 0.6


def span_window(event_id: int, span: str, store=None) -> tuple[float, float] | None:
    """(start_s, end_s) covering the approved words inside the recording.

    Exact when the capture kept word timestamps (`QUILL_ASR_WORD_TIMESTAMPS`).
    Otherwise estimated from where the span sits in the transcript, which is
    approximate but still far narrower than the whole moment. None when the
    span cannot be located at all — the caller then decides whether sending
    the untrimmed clip is acceptable, rather than doing it silently.
    """
    span = (span or "").strip()
    if not span:
        return None
    if store is None:
        from app.storage import get_store
        store = get_store()
    try:
        row = store.get_event(int(event_id))
    except Exception:
        return None
    if not isinstance(row, dict):
        return None
    meta = row.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = None
    meta = meta if isinstance(meta, dict) else {}

    words = meta.get("word_timestamps")
    if isinstance(words, list) and words:
        hit = _window_from_words(words, span)
        if hit:
            return hit

    # Fall back to position-in-transcript. Speech rate is roughly constant, so
    # a proportional estimate lands close; the padding absorbs the error.
    from app.services import evidence_playback
    transcript = (evidence_playback.clip_from_meta(meta).get("transcript")
                  or row.get("raw") or "")
    located = evidence_playback.find_span(transcript, span)
    duration = _wav_duration(playable_path(event_id, store))
    if not located or not duration:
        return None
    full = len(located.get("transcript") or transcript) or 1
    start = duration * (located["start"] / full)
    end = duration * (located["end"] / full)
    return _padded(start, end, duration)


def _window_from_words(words: list, span: str) -> tuple[float, float] | None:
    """Match the span against the word stream and take its time extent."""
    import re as _re

    def norm(s: str) -> str:
        return _re.sub(r"[^a-z0-9]+", " ", (s or "").casefold()).strip()

    target = norm(span).split()
    if not target:
        return None
    toks = [(norm(str(w.get("word") or "")), w) for w in words
            if isinstance(w, dict)]
    toks = [(t, w) for t, w in toks if t]
    if not toks:
        return None
    flat = [t for t, _ in toks]
    n = len(target)
    for i in range(len(flat) - n + 1):
        if flat[i:i + n] == target:
            try:
                return _padded(float(toks[i][1]["start"]),
                               float(toks[i + n - 1][1]["end"]), None)
            except (TypeError, ValueError, KeyError):
                return None
    return None


def _padded(start: float, end: float,
            duration: float | None) -> tuple[float, float]:
    lo = max(0.0, float(start) - SPAN_PAD_S)
    hi = float(end) + SPAN_PAD_S
    if duration:
        hi = min(hi, float(duration))
    if hi <= lo:
        hi = lo + 0.5
    return lo, hi


def _wav_duration(path: str | None) -> float | None:
    if not path:
        return None
    import wave
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            return (w.getnframes() / rate) if rate else None
    except Exception:
        return None


def trim_wav(path: Path, start_s: float, end_s: float) -> bytes | None:
    """A new in-memory WAV holding only [start_s, end_s).

    stdlib `wave` only — no ffmpeg dependency — so this works for the PCM WAV
    the capture pipeline writes and returns None for anything else, which the
    caller treats as "cannot trim".
    """
    import io
    import wave
    try:
        with wave.open(str(path), "rb") as src:
            rate = src.getframerate()
            if not rate:
                return None
            total = src.getnframes()
            first = max(0, min(total, int(start_s * rate)))
            last = max(first, min(total, int(end_s * rate)))
            if last <= first:
                return None
            src.setpos(first)
            frames = src.readframes(last - first)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as out:
                out.setnchannels(src.getnchannels())
                out.setsampwidth(src.getsampwidth())
                out.setframerate(rate)
                out.writeframes(frames)
            return buf.getvalue()
    except Exception as exc:
        print(f"[peer_clip] trim failed ({exc}).")
        return None


def clip_bytes_for_send(event_id: int, span: str,
                        store=None) -> tuple[bytes, str] | None:
    """The audio to actually send for one granted event, trimmed to the span.

    Returns (bytes, content_type), or None when nothing may be sent. With
    `QUILL_PEER_CLIP_SPAN_ONLY` on (the default) an untrimmable clip is
    refused rather than sent whole: the approver said yes to a claim, and
    shipping the surrounding conversation because trimming was inconvenient is
    not what they agreed to.
    """
    target = resolve_for_send(event_id, store)
    if target is None:
        return None
    window = span_window(event_id, span, store) if span else None
    if window is not None:
        data = trim_wav(target, window[0], window[1])
        if data:
            return data, "audio/wav"
    if settings.peer.clip_span_only:
        print(f"[peer_clip] refused untrimmable clip for event {event_id} "
              f"(span-only mode).")
        return None
    media = {".wav": "audio/wav", ".mp3": "audio/mpeg",
             ".m4a": "audio/mp4"}.get(target.suffix.lower(),
                                      "application/octet-stream")
    try:
        return target.read_bytes(), media
    except Exception as exc:
        print(f"[peer_clip] clip read failed ({exc}).")
        return None


def resolve_for_send(event_id: int, store=None) -> Path | None:
    """The file to stream for one granted event, confined to OUR data dir.

    The path comes from our own store rather than anything the peer sent, but
    it is still re-confined here: `playable_path` reads a meta field that was
    written by a capture pipeline, and a path that escaped the data dir would
    turn a clip grant into an arbitrary file read.
    """
    raw = playable_path(event_id, store)
    if not raw:
        return None
    root = Path(settings.storage.data_dir).resolve()
    try:
        target = Path(raw).resolve()
    except Exception:
        return None
    if root not in target.parents and target != root:
        print(f"[peer_clip] refused a clip path outside the data dir: {raw!r}")
        return None
    if not target.is_file():
        return None
    return target


def fetch_from_peer(peer_rec: dict, grant: dict, event_id: int,
                    timeout: float = 30.0) -> tuple[bytes, str] | None:
    """Asker side: pull one granted clip over the peer channel.

    Returns (bytes, content_type). The grant token authorises the read; the
    pairing token still authenticates the connection, so both are required.
    """
    import urllib.request

    from app.services import peer_channel
    token = (grant or {}).get("token")
    if not token:
        return None
    body = json.dumps({"token": token, "event_id": int(event_id)}).encode()
    cap = int(settings.peer.clip_max_bytes)
    for base in peer_channel.peer_urls(peer_rec):
        req = urllib.request.Request(
            f"{base}/peer/clip", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {peer_rec.get('outbound_token')}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                # Read one byte past the cap so a too-large body is detected
                # rather than silently truncated into a corrupt clip.
                data = r.read(cap + 1)
                if len(data) > cap:
                    print("[peer_clip] refused an oversized clip from a peer.")
                    return None
                ctype = r.headers.get("Content-Type") or "application/octet-stream"
                return data, ctype
        except Exception as exc:
            print(f"[peer_clip] clip fetch from {base} failed ({exc}).")
            continue
    return None
