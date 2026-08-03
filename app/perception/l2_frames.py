"""L2 — content-addressed frame store (WebP full + thumbnail).

Layout: ``data/frames/<sha256[:2]>/<sha256>.webp``. Full and thumb are
separate digests (different encodings) sharing the same CAS tree. Writes are
idempotent: if the digest file already exists, skip rewrite.

H.264 promotion packing is deferred; this module only handles still CAS I/O.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

import numpy as np


def frames_root(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    from app.config import settings
    return Path(settings.perception.frames_dir)


def path_for(sha256: str, root: str | Path | None = None) -> Path:
    sha = (sha256 or "").strip().lower()
    if len(sha) < 4:
        raise ValueError(f"invalid frame sha: {sha256!r}")
    return frames_root(root) / sha[:2] / f"{sha}.webp"


def _encode_webp(rgb: np.ndarray, *, quality: int, max_px: int | None) -> bytes:
    from PIL import Image

    if rgb is None or getattr(rgb, "size", 0) == 0:
        raise ValueError("empty rgb")
    img = Image.fromarray(rgb)
    if max_px and max(img.size) > max_px:
        w, h = img.size
        if w >= h:
            nh = max(1, int(h * (max_px / w)))
            img = img.resize((max_px, nh), Image.BILINEAR)
        else:
            nw = max(1, int(w * (max_px / h)))
            img = img.resize((nw, max_px), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=max(1, min(100, int(quality))),
             method=4)
    return buf.getvalue()


def _put_bytes(data: bytes, root: Path) -> str:
    sha = hashlib.sha256(data).hexdigest()
    dest = path_for(sha, root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.is_file():
        tmp = dest.with_suffix(".webp.tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
    return sha


def put_rgb(rgb: np.ndarray, *, root: str | Path | None = None,
            thumb_max_px: int | None = None,
            thumb_quality: int | None = None,
            full_quality: int | None = None) -> dict:
    """Encode full + thumb WebP into the CAS. Returns sha/path dict."""
    from app.config import settings
    cfg = settings.perception
    base = frames_root(root)
    base.mkdir(parents=True, exist_ok=True)
    t_px = cfg.thumb_max_px if thumb_max_px is None else thumb_max_px
    t_q = cfg.thumb_quality if thumb_quality is None else thumb_quality
    f_q = cfg.full_quality if full_quality is None else full_quality

    full_bytes = _encode_webp(rgb, quality=f_q, max_px=None)
    thumb_bytes = _encode_webp(rgb, quality=t_q, max_px=t_px)
    frame_sha = _put_bytes(full_bytes, base)
    thumb_sha = _put_bytes(thumb_bytes, base)
    return {
        "frame_sha256": frame_sha,
        "thumb_sha256": thumb_sha,
        "frame_path": str(path_for(frame_sha, base)),
        "thumb_path": str(path_for(thumb_sha, base)),
        "frame_bytes": len(full_bytes),
        "thumb_bytes": len(thumb_bytes),
    }


def unlink_sha(sha256: str, root: str | Path | None = None) -> bool:
    try:
        p = path_for(sha256, root)
    except ValueError:
        return False
    try:
        if p.is_file():
            p.unlink()
            # Best-effort remove empty shard dir.
            try:
                p.parent.rmdir()
            except OSError:
                pass
            return True
    except Exception:
        return False
    return False


def dir_size_bytes(root: str | Path | None = None) -> int:
    base = frames_root(root)
    if not base.is_dir():
        return 0
    total = 0
    for f in base.rglob("*.webp"):
        try:
            total += f.stat().st_size
        except OSError:
            continue
    return total
