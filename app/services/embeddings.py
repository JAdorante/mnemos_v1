"""Text embeddings for semantic memory search.

Uses a small local sentence-transformers model (all-MiniLM-L6-v2, 384-dim) —
CPU-friendly, no API, works offline. Every memory event's text is embedded so
the timeline is searchable by meaning ("what did I see on the whiteboard?")
rather than exact keywords.
"""
from __future__ import annotations

import threading

import numpy as np

from app.config import settings


class Embedder:
    def __init__(self) -> None:
        self.model_name = settings.memory.embedding_model
        self._model = None
        self._lock = threading.Lock()
        self._dim: int | None = None

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    print(f"[embed] loading '{self.model_name}' ...")
                    self._model = SentenceTransformer(self.model_name, device="cpu")
                    self._dim = self._model.get_sentence_embedding_dimension()
                    print(f"[embed] ready ({self._dim}-d).")
        return self._model

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._load()
        return int(self._dim)

    def encode(self, text: str) -> np.ndarray:
        model = self._load()
        vec = model.encode(text or "", normalize_embeddings=True)
        return np.asarray(vec, dtype=np.float32)

    def warmup(self) -> None:
        """Force the model — and its heavy transformers/torch imports — to load
        NOW, on the calling thread. Call once on the main thread at startup so
        the first real encode can't race other first-time imports.

        Specifically: SpeechBrain (loaded lazily by speaker ID on the audio
        thread) plants a `speechbrain.integrations.k2_fsa` lazy module that
        *raises* when touched because `k2` isn't installed. transformers' import
        walks sys.modules via inspect.getmodule() and trips that landmine,
        which corrupts a concurrent first import of sentence-transformers
        ('Could not import module PreTrainedModel'). Importing the embedder
        first, single-threaded, sidesteps the whole race. Best-effort."""
        try:
            self.encode("")
        except Exception as exc:
            print(f"[embed] warmup skipped ({exc})")

    def encode_many(self, texts: list[str]) -> np.ndarray:
        """Batch-encode many texts at once. Far faster than calling encode()
        per item — used by the startup backfill, which would otherwise embed
        the whole timeline one string at a time."""
        model = self._load()
        vecs = model.encode(
            [t or "" for t in texts],
            normalize_embeddings=True, batch_size=64, show_progress_bar=False,
        )
        return np.asarray(vecs, dtype=np.float32)


embedder = Embedder()
