"""Local embedding backends: local only, CPU, no torch, no external API.

Model files are read from ``quoth.paths.model_cache()`` (``QUOTH_MODEL_CACHE``) unless a
caller passes ``cache_dir``.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .. import paths


class EmbedderUnavailable(RuntimeError):
    """The requested local backend or its model files are not installed on this machine."""



@dataclass(frozen=True)
class ModelSpec:
    name: str            # value stored in embeddings.model and passed to --model
    backend: str         # 'fastembed' | 'onnx' | 'hash'
    source_id: str       # fastembed model id (HF repo) or '' for built-ins
    dim: int
    query_prefix: str = ""
    passage_prefix: str = ""
    note: str = ""


# Supported models. e5 models require "query: " / "passage: " prefixes.
MODELS: dict[str, ModelSpec] = {m.name: m for m in (
    ModelSpec("multilingual-e5-small", "fastembed", "intfloat/multilingual-e5-small", 384,
              "query: ", "passage: ", "118M params"),
    ModelSpec("multilingual-e5-large", "fastembed", "intfloat/multilingual-e5-large", 1024,
              "query: ", "passage: ", "560M params"),
    # bge-m3 is the default model. fastembed (locked
    # >=0.8) does not ship BAAI/bge-m3, so backend='onnx' runs the official ONNX
    # export from the BAAI/bge-m3 HF repo (onnx/model.onnx + tokenizer) through
    # onnxruntime with CLS pooling + L2 normalisation (the model card's dense recipe).
    ModelSpec("bge-m3", "onnx", "BAAI/bge-m3", 1024, "", "",
              "default; official ONNX export, CLS pooling + L2 norm; XLM-RoBERTa tokenizer"),
    # Deterministic, dependency-free, NON-SEMANTIC baseline: hashed character 3-grams.
    # Used for tests and for exercising the dense/hybrid plumbing when no ONNX model is
    # installed. It must never be picked as the default.
    ModelSpec("hash-ngram", "hash", "", 512, "", "", "non-semantic char-3gram baseline"),
)}

DEFAULT_MODEL = "bge-m3"
# Token limit of one ONNX input. BGE-M3 reads up to 8192 tokens; a passage (at most
# chunker.WINDOW characters under its title, aliases and heading path) always fits, so no
# passage is cut short.
MAX_TOKENS = 8192


class Embedder(Protocol):
    spec: ModelSpec

    def embed_passages(self, texts: list[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...


def _l2(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


_WS = re.compile(r"\s+")


class HashEmbedder:
    """Char 3-gram feature hashing (blake2b) into `dim` buckets, L2-normalised."""

    def __init__(self, spec: ModelSpec):
        self.spec = spec

    def _one(self, text: str) -> np.ndarray:
        v = np.zeros(self.spec.dim, dtype=np.float32)
        t = f" {_WS.sub(' ', text.lower()).strip()} "
        for i in range(len(t) - 2):
            h = int.from_bytes(hashlib.blake2b(t[i:i + 3].encode(), digest_size=8).digest(), "little")
            v[h % self.spec.dim] += 1.0 if (h >> 63) & 1 else -1.0
        return v

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.spec.dim), dtype=np.float32)
        return _l2(np.stack([self._one(t) for t in texts]))

    def embed_query(self, text: str) -> np.ndarray:
        return _l2(self._one(text))


class FastEmbedder:
    """fastembed TextEmbedding (ONNX Runtime, CPU). Imported lazily; never downloads by default."""

    def __init__(self, spec: ModelSpec, cache_dir: str | None = None, threads: int | None = None,
                 allow_download: bool = False, batch_size: int = 32):
        try:
            from fastembed import TextEmbedding  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover - depends on the install
            raise EmbedderUnavailable(
                "fastembed is not installed (offline uv cache has no wheel); "
                "install it into this project's venv to use " + spec.name) from e
        supported = {m["model"] for m in TextEmbedding.list_supported_models()}
        if spec.source_id not in supported:
            raise EmbedderUnavailable(
                f"{spec.source_id} is not supported by the installed fastembed version")
        kw: dict = {"model_name": spec.source_id, "threads": threads,
                    "cache_dir": str(cache_dir or paths.model_cache())}
        if not allow_download:
            kw["local_files_only"] = True
        try:
            self._m = TextEmbedding(**{k: v for k, v in kw.items() if v is not None})
        except Exception as e:  # pragma: no cover - depends on the install
            raise EmbedderUnavailable(f"cannot load {spec.source_id} locally: {e}") from e
        self.spec = spec
        self.batch_size = batch_size

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.spec.dim), dtype=np.float32)
        docs = [self.spec.passage_prefix + t for t in texts]
        return _l2(np.stack(list(self._m.embed(docs, batch_size=self.batch_size))))

    def embed_query(self, text: str) -> np.ndarray:
        return _l2(next(iter(self._m.embed([self.spec.query_prefix + text]))))


class OnnxEmbedder:
    """Official ONNX export of a HF model via onnxruntime, CLS pooling + L2 norm.

    Files live under cache_dir/<repo>/ (downloaded once with huggingface_hub when
    allow_download; otherwise loaded from the local snapshot only).
    """

    def __init__(self, spec: ModelSpec, cache_dir: str | None = None, threads: int | None = None,
                 allow_download: bool = False, batch_size: int = 32):
        try:
            import onnxruntime as ort  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover - depends on the install
            raise EmbedderUnavailable(
                "onnxruntime is not installed; install the 'dense' extra (uv sync --extra dense)"
                " to use " + spec.name) from e
        root = str(cache_dir or paths.model_cache())
        model_dir = os.path.join(root, spec.source_id)
        model_path = os.path.join(model_dir, "onnx", "model.onnx")
        if allow_download and not os.path.exists(model_path):
            try:
                from huggingface_hub import snapshot_download
            except ImportError as e:  # pragma: no cover - depends on the install
                raise EmbedderUnavailable("huggingface_hub is not installed") from e
            snapshot_download(repo_id=spec.source_id, local_dir=model_dir,
                              allow_patterns=["onnx/model.onnx", "onnx/model.onnx_data",
                                              "tokenizer.json", "tokenizer_config.json",
                                              "sentencepiece.bpe.model", "config.json"])
        if not os.path.exists(model_path):
            raise EmbedderUnavailable(
                f"{spec.source_id} ONNX files not found under {model_dir};"
                " download them first (see README: Quick start)")
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads or (os.cpu_count() or 4)
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        try:
            self._sess = ort.InferenceSession(model_path, sess_options=so, providers=["CPUExecutionProvider"])
        except Exception as e:  # pragma: no cover - depends on the install
            raise EmbedderUnavailable(f"cannot load {model_path}: {e}") from e
        try:
            from tokenizers import Tokenizer  # type: ignore[import-not-found]
            self._tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        except Exception as e:
            raise EmbedderUnavailable(f"cannot load tokenizer for {spec.source_id}: {e}") from e
        self.spec = spec
        self.batch_size = batch_size
        self.model_dir = model_dir

    def _forward(self, texts: list[str]) -> np.ndarray:
        """CLS vectors of `texts`, in order, each read whole (up to MAX_TOKENS).

        Texts run shortest first in batches of similar length, so little compute goes to
        padding. A batch holds at most `batch_size` texts and at most the attention work
        of `batch_size` texts of 512 tokens, so a long text never raises peak memory.
        """
        enc = self._tok.encode_batch(texts)
        lens = [min(len(e.ids), MAX_TOKENS) for e in enc]
        order = sorted(range(len(texts)), key=lens.__getitem__)
        budget = self.batch_size * 512 * 512
        out = np.zeros((len(texts), self.spec.dim), dtype=np.float32)
        i = 0
        while i < len(order):
            j = i + 1
            while (j < len(order) and j - i < self.batch_size
                   and (j - i + 1) * lens[order[j]] ** 2 <= budget):
                j += 1
            batch = order[i:j]
            width = lens[batch[-1]]
            ids = np.zeros((len(batch), width), dtype=np.int64)
            mask = np.zeros((len(batch), width), dtype=np.int64)
            for row, k in enumerate(batch):
                ids[row, :lens[k]] = enc[k].ids[:lens[k]]
                mask[row, :lens[k]] = 1
            # Export emits (token_embeddings, sentence_embedding); CLS of the last
            # hidden state == sentence_embedding for this export, but pool CLS
            # ourselves from token_embeddings to keep the recipe explicit.
            last = self._sess.run(None, {"input_ids": ids, "attention_mask": mask})[0]
            out[batch] = np.asarray(last, dtype=np.float32)[:, 0, :]
            i = j
        return _l2(out)

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.spec.dim), dtype=np.float32)
        return self._forward([self.spec.passage_prefix + t for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._forward([self.spec.query_prefix + text])[0]


def get_embedder(name: str, **kw) -> Embedder:
    spec = MODELS.get(name)
    if spec is None:
        raise ValueError(f"unknown model {name!r}; expected one of {sorted(MODELS)}")
    if spec.backend == "hash":
        return HashEmbedder(spec)
    if spec.backend == "onnx":
        return OnnxEmbedder(spec, **kw)
    return FastEmbedder(spec, **kw)
