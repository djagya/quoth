"""Cross-encoder reranker for the prefetch relevance gate (local, CPU, no torch, no API).

bge-m3 ranks the vault by bi-encoder cosine: query and note are embedded apart, and the
cosine level shifts with query length and language, so one floor does not separate
relevant from irrelevant notes. A cross-encoder reads the query and one passage together
and returns one logit; ``sigmoid(logit)`` is the score in [0, 1] the gate compares.

Model: BAAI/bge-reranker-v2-m3 (multilingual, XLM-RoBERTa large). BAAI publishes no ONNX
export of it, so the ONNX files come from the onnx-community export (Hugging Face's
transformers.js org) pinned at one revision. ``bge-reranker-v2-m3-int8`` (default, 571 MB)
is its dynamically quantised graph; ``bge-reranker-v2-m3`` the fp32 graph (2.27 GB).
Files live under ``<cache>/<repo>/`` with the embedder's cache (``QUOTH_MODEL_CACHE``, else
``$QUOTH_HOME/models``) and are fetched only by `quoth models fetch --reranker NAME`.
``hash-rerank`` is a deterministic, non-semantic stand-in for tests.

Nothing here is part of the index contract: no score is stored in a snapshot.
`RerankService` loads the model off the request path and retries a failed load with backoff.
The sidecar scores on its own rerank worker, apart from the evidence worker, and a turn whose
scores are not ready in time keeps the cosine gate. The reranker scores the same excerpt the
recall block injects (`relevance.vault_text`).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
import re
import sys
import threading
import time

import numpy as np

from . import paths
from .recall_defaults import RERANK_BUDGET, RERANK_MAX_TOKENS

log = logging.getLogger(__name__)

REPO = "onnx-community/bge-reranker-v2-m3-ONNX"
REVISION = "6f5ff65298512715a1e669753bc754d2bc8f367b"  # 2025-09-01
# sha256 of every file at REVISION: LFS files from the hub's LFS oids, small files hashed
# locally after checking their git blob ids against the hub. A mismatch refuses to load.
TOKENIZER_SHA256 = (
    ("tokenizer.json", "8bf8afbfd11306bd872018c53bfdf2e160a56f8edbcf49933324404791c148d3"),
    ("tokenizer_config.json", "b87c8703482b0300d3da30e201519aa641f6a450f5eb5bf1e624afbf70c74d80"),
    ("special_tokens_map.json", "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835"),
    ("config.json", "122e922dcfed6503c8721e6fe1daf090340c3d95ca7f3aa3a72730b321a51cfd"),
)
# Query + passage tokens per pair (longest first truncation). The injected snippet
# (recall_defaults.VAULT_SNIPPET characters) fits with a query, and 3 pairs stay near half a
# second on 6 threads.
MAX_TOKENS = RERANK_MAX_TOKENS
# Passage characters handed to the tokenizer (bounded work before truncation).
PASSAGE_CHARS = 1200
# Seconds the scores of one turn may take after the vault hits are ready.
DEFAULT_BUDGET = RERANK_BUDGET


class RerankerUnavailable(RuntimeError):
    """The reranker backend or its model files are not installed on this host."""


@dataclass(frozen=True)
class RerankSpec:
    name: str
    backend: str          # 'onnx' | 'hash'
    repo: str = ""
    revision: str = ""
    onnx_file: str = ""   # graph inside the repo
    weights: tuple = ()   # external weight files next to the graph
    size_mb: int = 0      # download size
    note: str = ""
    sha256: tuple = ()    # ((file, sha256), ...) for every file: the graph, weights, tokenizer

    @property
    def files(self) -> list[str]:
        return [f for f, _ in self.sha256]


MODELS: dict[str, RerankSpec] = {m.name: m for m in (
    RerankSpec("bge-reranker-v2-m3-int8", "onnx", REPO, REVISION, "onnx/model_int8.onnx", (), 589,
               "BAAI/bge-reranker-v2-m3, onnx-community export, int8 dynamic quantisation",
               (("onnx/model_int8.onnx", "912fc1215c2dbff6499700534bd8d31253af01573861abbfc43afd1fab6cce5d"),
                *TOKENIZER_SHA256)),
    RerankSpec("bge-reranker-v2-m3", "onnx", REPO, REVISION, "onnx/model.onnx", ("onnx/model.onnx_data",), 2289,
               "BAAI/bge-reranker-v2-m3, onnx-community export, fp32",
               (("onnx/model.onnx", "faae32b124a9d54afb7e89b5e9896e03c18a9552d56d1d6b273a709a83012486"),
                ("onnx/model.onnx_data", "f009aa6c6cf21986fd7e0021fa66b20ccce27abc6900a57c7109c8496811bcbe"),
                *TOKENIZER_SHA256)),
    # Deterministic, dependency-free, NON-SEMANTIC: hashed character 3-gram cosine as a logit.
    RerankSpec("hash-rerank", "hash", note="tests and plumbing only; never a serving choice"),
)}
DEFAULT_MODEL = "bge-reranker-v2-m3-int8"


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x)) if x >= 0 else math.exp(x) / (1.0 + math.exp(x))


_WS = re.compile(r"\s+")


def passage(text: str) -> str:
    """What the reranker reads of a hit or claim: whitespace folded, bounded."""
    return _WS.sub(" ", str(text or "")).strip()[:PASSAGE_CHARS]


class HashReranker:
    """sigmoid(12 · (cosine of hashed char 3-grams − 0.4)): same text scores ~1, unrelated ~0.1."""

    def __init__(self, spec: RerankSpec):
        from .embed.backends import MODELS as EMBED_MODELS, HashEmbedder
        self.spec = spec
        self._embed = HashEmbedder(EMBED_MODELS["hash-ngram"])

    def logit(self, query: str, text: str) -> float:
        q, p = self._embed.embed_query(query), self._embed.embed_query(passage(text))
        return 12.0 * (float(q @ p) - 0.4)

    def score(self, query: str, texts: list[str], deadline: float | None = None) -> list[float | None]:
        return _score_each(self.logit, query, texts, deadline)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify(spec: RerankSpec, directory: str) -> None:
    """Every pinned file present with its sha256, else RerankerUnavailable (file names only)."""
    bad = [f for f, digest in spec.sha256
           if not os.path.exists(os.path.join(directory, f)) or sha256_file(os.path.join(directory, f)) != digest]
    if bad:
        raise RerankerUnavailable(f"{spec.name}: missing or checksum mismatch: {', '.join(bad)}")


class OnnxReranker:
    """One ONNX cross-encoder session (CPU); one pair per run, so a deadline stops between pairs."""

    def __init__(self, spec: RerankSpec, cache_dir: str | None = None, threads: int | None = None,
                 max_tokens: int = MAX_TOKENS):
        try:
            import onnxruntime as ort  # type: ignore[import-not-found]
            from tokenizers import Tokenizer  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover - depends on host
            raise RerankerUnavailable("onnxruntime/tokenizers are not installed; install the 'dense' extra") from e
        self.model_dir = model_dir(spec, cache_dir)
        path = os.path.join(self.model_dir, spec.onnx_file)
        if not os.path.exists(path):
            raise RerankerUnavailable(f"{spec.name} ONNX files not found under {self.model_dir};"
                                      f" run `quoth models fetch --no-embedder --reranker {spec.name}` first")
        verify(spec, self.model_dir)  # seconds for 0.6-2.3 GB: callers load off the request path
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads or (os.cpu_count() or 4)
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        try:
            self._sess = ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])
            self._tok = Tokenizer.from_file(os.path.join(self.model_dir, "tokenizer.json"))
        except Exception as e:  # pragma: no cover - depends on host
            raise RerankerUnavailable(f"cannot load {spec.name}: {type(e).__name__}") from e
        self._tok.no_padding()
        self._tok.enable_truncation(max_length=max_tokens, strategy="longest_first")
        self._inputs = {i.name for i in self._sess.get_inputs()}
        self.spec = spec
        self.max_tokens = max_tokens

    def logit(self, query: str, text: str) -> float:
        enc = self._tok.encode(query, passage(text))
        feed = {"input_ids": np.asarray([enc.ids], dtype=np.int64),
                "attention_mask": np.asarray([enc.attention_mask], dtype=np.int64)}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
        return float(np.asarray(self._sess.run(None, feed)[0]).reshape(-1)[0])

    def score(self, query: str, texts: list[str], deadline: float | None = None) -> list[float | None]:
        return _score_each(self.logit, query, texts, deadline)


def _score_each(logit, query: str, texts: list[str], deadline: float | None) -> list[float | None]:
    """sigmoid scores (4 decimals) in order; None for every pair not started by `deadline`."""
    out: list[float | None] = []
    for text in texts:
        if deadline is not None and time.monotonic() >= deadline:
            out.append(None)
            continue
        out.append(round(sigmoid(logit(query, text)), 4))
    return out


def model_dir(spec: RerankSpec, cache_dir: str | None = None) -> str:
    root = cache_dir or str(paths.model_cache())
    return os.path.join(root, spec.repo)


def score_pairs(model, query: str, texts: list[str], deadline: float | None = None,
                cancel: threading.Event | None = None) -> tuple[list, list]:
    """(scores, per-pair ms), one pair per model call; a pair not started before `deadline`
    or after `cancel` is set gets None for both."""
    scores, ms = [], []
    for text in texts:
        if (cancel is not None and cancel.is_set()) or (deadline is not None and time.monotonic() >= deadline):
            scores.append(None)
            ms.append(None)
            continue
        t0 = time.monotonic()
        scores.append(model.score(query, [text])[0])
        ms.append(round((time.monotonic() - t0) * 1000, 1))
    return scores, ms


def get_reranker(name: str, **kw):
    spec = MODELS.get(name)
    if spec is None:
        raise ValueError(f"unknown reranker {name!r}; expected one of {sorted(MODELS)}")
    if spec.backend == "hash":
        return HashReranker(spec)
    return OnnxReranker(spec, **kw)


# Seconds before each retry of a failed load; the last delay repeats.
RETRY_DELAYS = (60.0, 300.0, 1800.0)


class RerankService:
    """The sidecar's reranker: loaded once in the background, never on a turn.

    ``model()`` returns the loaded reranker or None (still loading, or unavailable); the
    caller then keeps the cosine gate. ``budget`` bounds one turn's scoring, ``claims``
    also scores the served dense claims. A failed load is logged and reported by
    ``status()``; it never fails a turn. The loader thread retries a failed load after each
    of ``retry_delays`` seconds in turn (1, 5 and 30 min by default, then every 30 min) until
    it loads or ``stop()`` is called, so model files fetched later need no restart.
    """

    def __init__(self, name: str = DEFAULT_MODEL, *, budget: float = DEFAULT_BUDGET, claims: bool = False,
                 max_tokens: int = MAX_TOKENS, cache_dir: str | None = None, loader=None,
                 retry_delays: tuple[float, ...] = RETRY_DELAYS):
        if name not in MODELS:
            raise ValueError(f"unknown reranker {name!r}; expected one of {sorted(MODELS)}")
        if not retry_delays:
            raise ValueError("retry_delays needs at least one delay")
        self.name = name
        self.budget = budget
        self.claims = claims
        self.max_tokens = max_tokens
        self.cache_dir = cache_dir
        self._loader = loader
        self._retry_delays = tuple(retry_delays)
        self._model = None
        self._state = "loading"
        self._load_ms = None
        self._attempts = 0
        self._retry_at = None  # monotonic time of the next attempt while unavailable
        self._lock = threading.Lock()
        self._thread = None
        self._settled = threading.Event()  # set once the first attempt has an outcome
        self._stop = threading.Event()

    def _load_once(self) -> bool:
        from .cpu import embed_threads
        t0 = time.monotonic()
        self._attempts += 1
        try:
            loader = self._loader or (lambda: get_reranker(self.name, cache_dir=self.cache_dir,
                                                           threads=embed_threads(), max_tokens=self.max_tokens))
            model = loader()
            model.score("warmup", ["warmup"])  # first run allocates; keep it off a live turn
        except RerankerUnavailable as e:  # names files, never content
            log.warning("reranker unavailable (attempt %d): %s", self._attempts, e)
            return False
        except Exception as e:
            log.warning("reranker %s unavailable (attempt %d): %s", self.name, self._attempts,
                        type(e).__name__)
            return False
        self._model, self._retry_at, self._state = model, None, "ready"
        self._load_ms = round((time.monotonic() - t0) * 1000, 1)
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._load_once():
                break
            delay = self._retry_delays[min(self._attempts, len(self._retry_delays)) - 1]
            self._retry_at = time.monotonic() + delay
            self._state = "unavailable"
            self._settled.set()
            if self._stop.wait(delay):
                break
            self._state = "loading"
        self._settled.set()

    def start(self) -> "RerankService":
        """Load in a background thread (sidecar startup)."""
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="quoth-rerank-load", daemon=True)
                self._thread.start()
        return self

    def stop(self) -> None:
        """End the retry loop (sidecar shutdown); a loaded model stays usable."""
        self._stop.set()

    def wait(self, timeout: float | None = None) -> str:
        """Start the load; return the state once the first attempt has ended (eval harness)."""
        self.start()
        self._settled.wait(timeout)
        return self._state

    def model(self):
        return self._model

    @property
    def state(self) -> str:
        return self._state

    def config(self) -> dict:
        spec = MODELS[self.name]
        return {"model": self.name, "repo": spec.repo or None, "revision": spec.revision or None,
                "budget": self.budget, "claims": self.claims, "max_tokens": self.max_tokens}

    def status(self) -> dict:
        retry_at = self._retry_at
        retry_in = (max(0.0, round(retry_at - time.monotonic(), 1))
                    if retry_at is not None and self._state == "unavailable" else None)
        return {**self.config(), "status": self._state, "load_ms": self._load_ms,
                "attempts": self._attempts, "retry_in_s": retry_in}


# ------------------------------------------------------------------ flags

def add_arguments(parser) -> None:
    """The reranker's flags, shared by `quoth sidecar` and `quoth eval run`."""
    parser.add_argument("--rerank-model", choices=sorted(MODELS), default=None,
                        help="load this cross-encoder and score the vault hits prefetch would inject"
                             f" (default off; the recommended model is {DEFAULT_MODEL}). Without a rerank threshold"
                             " (--vault-min-rerank, --vault-max-rerank-gap, --vault-rerank-widen,"
                             " --claims-min-rerank) only dry runs score: live turns are unchanged")
    parser.add_argument("--rerank-budget", type=float, default=DEFAULT_BUDGET,
                        help="seconds a turn's reranking may take once vault hits are ready; past it the"
                             " turn keeps the cosine gate")
    parser.add_argument("--rerank-claims", action="store_true",
                        help="also score the served meaning-matched claims (implied by --claims-min-rerank)")
    parser.add_argument("--rerank-max-tokens", type=int, default=MAX_TOKENS,
                        help="query + passage tokens per pair")


def from_args(args, gate, parser=None) -> RerankService | None:
    """The service the flags set, or None; rerank thresholds without a model are a usage error."""
    name = getattr(args, "rerank_model", None)
    if name is None:
        if gate.rerank_on or gate.claims_min_rerank is not None:
            msg = "rerank thresholds need --rerank-model"
            if parser is None:
                raise ValueError(msg)
            parser.error(msg)
        return None
    return RerankService(name, budget=args.rerank_budget, max_tokens=args.rerank_max_tokens,
                         claims=bool(args.rerank_claims) or gate.claims_min_rerank is not None)


# ------------------------------------------------------------------ `quoth rerank check`

CHECK_QUERY = "How often do I feed the sourdough starter in winter?"
CHECK_PASSAGES = (
    "Sourdough: in winter the starter gets fed once a day at a 1:5:5 ratio, kept near the oven.",
    "The bike needs new brake pads before the spring trip; the shop on the corner has them.",
    "Закваска зимой: кормлю раз в день, пропорция 1:5:5, держу у плиты.",
)


def check(name: str, *, cache_dir: str | None = None,
          max_tokens: int = MAX_TOKENS, repeats: int = 5) -> dict:
    """Load the model, score 3 public pairs, time them."""
    from .cpu import embed_threads
    spec = MODELS[name]
    threads = embed_threads()
    t0 = time.monotonic()
    model = get_reranker(name, cache_dir=cache_dir, threads=threads, max_tokens=max_tokens) if spec.backend == "onnx" else get_reranker(name)
    load_s = round(time.monotonic() - t0, 2)
    scores = model.score(CHECK_QUERY, list(CHECK_PASSAGES))

    def timed(passages):
        times = []
        for _ in range(repeats):
            t1 = time.monotonic()
            model.score(CHECK_QUERY, passages)
            times.append(round((time.monotonic() - t1) * 1000, 1))
        return {"median": sorted(times)[len(times) // 2], "min": min(times), "max": max(times)}

    # Worst case: passages long enough to fill max_tokens (a vault hit is up to 1800 characters).
    full = [((p + " ") * 40)[:PASSAGE_CHARS] for p in CHECK_PASSAGES]
    out = {"model": name, "repo": spec.repo or None, "revision": spec.revision or None,
           "files": spec.files, "size_mb": spec.size_mb, "threads": threads, "max_tokens": max_tokens,
           "load_s": load_s, "scores": scores, "ms_3_pairs": timed(list(CHECK_PASSAGES)),
           "ms_3_pairs_full": timed(full)}
    if spec.backend == "onnx":
        out["model_dir"] = model_dir(spec, cache_dir)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="quoth rerank", description="the prefetch gate's cross-encoder reranker")
    sub = p.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("check", help="load the reranker, score 3 public pairs, report latency")
    pc.add_argument("--model", choices=sorted(MODELS), default=DEFAULT_MODEL)
    pc.add_argument("--cache-dir", default=None, help="model root (default $QUOTH_MODEL_CACHE, else $QUOTH_HOME/models)")
    pc.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    a = p.parse_args(argv)
    try:
        out = check(a.model, cache_dir=a.cache_dir, max_tokens=a.max_tokens)
    except (RerankerUnavailable, OSError) as e:
        print(f"quoth rerank: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
