"""64-bit difference hash for L1 perceptual-change detection.

Downscale to grayscale 9x8, compare adjacent pixels horizontally → 64 bits.
Trigger when Hamming distance from the previous hash exceeds the configured
threshold (prompt default: 10). Pure numpy — no OpenCV dependency.
"""
from __future__ import annotations

import numpy as np


def dhash64(rgb: np.ndarray, size: int = 8) -> int:
    """Compute a 64-bit dHash of an RGB (or gray) image array."""
    if rgb is None or getattr(rgb, "size", 0) == 0:
        return 0
    if rgb.ndim == 3:
        gray = np.mean(rgb, axis=2)
    else:
        gray = rgb.astype(np.float64)
    # 9 columns × 8 rows so we get 8 horizontal comparisons per row.
    from PIL import Image
    img = Image.fromarray(gray.astype(np.uint8))
    small = np.asarray(img.resize((size + 1, size), Image.BILINEAR),
                       dtype=np.float64)
    diff = small[:, 1:] > small[:, :-1]
    bits = 0
    for i, v in enumerate(diff.flatten()):
        if v:
            bits |= (1 << i)
    return int(bits)


def hamming64(a: int, b: int) -> int:
    """Popcount of XOR — bits that differ between two 64-bit hashes."""
    x = (int(a) ^ int(b)) & ((1 << 64) - 1)
    try:
        return x.bit_count()
    except AttributeError:  # pragma: no cover — py<3.10
        return bin(x).count("1")
