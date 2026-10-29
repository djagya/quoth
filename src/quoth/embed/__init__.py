"""Local-only embeddings: backends, model registry and incremental `quoth embed`.

Only local backends exist. `fastembed` (ONNX Runtime on CPU, no torch) is imported
lazily so the rest of the engine works without it; there is no remote/API backend
and none may be added: note text never leaves the machine.
"""

from __future__ import annotations

from .backends import (
    MODELS,
    Embedder,
    EmbedderUnavailable,
    HashEmbedder,
    ModelSpec,
    get_embedder,
)
from .store import EmbedStats, embed_chunks, from_blob, load_matrix, pending_count, to_blob

__all__ = [
    "MODELS", "Embedder", "EmbedderUnavailable", "HashEmbedder", "ModelSpec", "get_embedder",
    "EmbedStats", "embed_chunks", "from_blob", "load_matrix", "pending_count", "to_blob",
]
