"""Optional local embeddings.

Semantic retrieval is the difference between "What attracts you to this
position?" finding your stored "Why do you want to work here?" answer and
missing it — those two share almost no words, so lexical scoring alone cannot
connect them.

OpenRouter serves chat completions only, free or paid, so embeddings have to be
local. `model2vec` is used when present: static embeddings, numpy only, tens of
megabytes, no torch, no ONNX runtime, and fast enough to embed the whole
knowledge base at boot.

When it is not installed the service still works — retrieval falls back to BM25
plus the LLM reranking pass, which is where most of the semantic power sits
anyway. Nothing here is required.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("jobkb.embed")

DEFAULT_MODEL = os.environ.get("JOBKB_EMBED_MODEL", "minishlab/potion-base-8M")


class Embedder:
    """Wraps whichever backend is available. `.available` says which happened."""

    def __init__(self) -> None:
        self.backend = "none"
        self._model = None
        self._np = None
        try:
            import numpy as np  # noqa: PLC0415
            from model2vec import StaticModel  # noqa: PLC0415

            self._np = np
            self._model = StaticModel.from_pretrained(DEFAULT_MODEL)
            self.backend = f"model2vec:{DEFAULT_MODEL}"
            log.info("embeddings enabled (%s)", self.backend)
        except Exception as exc:  # noqa: BLE001
            log.info("embeddings unavailable, using lexical retrieval only (%s)", exc)

    @property
    def available(self) -> bool:
        return self._model is not None

    def encode(self, texts: list[str]):
        """L2-normalised vectors, or None when no backend is installed."""
        if not self.available or not texts:
            return None
        vecs = self._model.encode(texts)
        norms = self._np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms

    def similarity(self, query_vec, matrix) -> list[float]:
        """Cosine similarity against a pre-normalised matrix."""
        if query_vec is None or matrix is None:
            return []
        return (matrix @ query_vec).tolist()


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder
