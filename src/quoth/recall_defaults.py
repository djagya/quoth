"""Every budget, count, cap and floor of the recall path, in one place.

The sidecar, the relevance gate, the reranker, the conversation lanes, the claim index, the
prefetch log and the eval harness (`eval run`, `eval sweep`) import these values instead of
copying them, and `tests/test_recall_defaults.py` checks the numbers quoted in
docs/HERMES.md and docs/EVAL.md against them. Change a default here and nowhere else.

Pure data: no imports from quoth, so any module (and the lightweight eval code) can read it.
"""
from __future__ import annotations

# ---------------------------------------------------------------- time budgets (seconds)

# How long a turn waits for vault evidence (and dense claims); the plugin's own timeout is 3 s.
EVIDENCE_BUDGET = 1.5
# How long a turn's reranking may take once the vault hits are ready (also ends at the
# evidence deadline).
RERANK_BUDGET = 0.8
# How long a concurrent caller waits for another thread's embedding of the same query
# before embedding it itself (`serving_cache.QueryEmbedder.vector`).
QUERY_EMBED_WAIT = 5.0

# ---------------------------------------------------------------- what a turn injects

PREFETCH_CLAIMS = 6       # accepted claims per turn
PREFETCH_EVIDENCE = 3     # vault hits per turn
PREFETCH_EPISODES = 2     # excerpts per conversation lane
VAULT_SNIPPET = 360       # characters of a vault hit: injected, and what the gate and reranker judge
EPISODE_SNIPPET = 240     # characters of a lane excerpt
QUERY_MAX_CHARS = 1000    # the search text is cut to this

# ---------------------------------------------------------------- candidate pools

# A turn judges every candidate in its pool and injects the best ones that pass, so a slot
# a weak candidate leaves is refilled by the next one (gate before the cut).
VAULT_POOL = 10           # vault hits a turn retrieves and judges
CLAIM_POOL = 12           # claims per path (meaning, words) a turn retrieves and judges
MAX_CANDIDATES = 20       # deepest list per store (dry runs, the evidence bundle's k)
MAX_WIDEN = 20            # most vault hits a rerank widen may score (= MAX_CANDIDATES)

# ---------------------------------------------------------------- retrieval (`quoth.retrieval`)

# How vault hits are ranked, for prefetch and for `evidence_bundle` alike: "hybrid" fuses the
# dense list (bge-m3 cosine) with the snapshot's BM25 (unicode61) and trigram lists and the
# notes the query names, by reciprocal rank (RRF); "dense" ranks by cosine alone.
# Dense is the default: on the golden sets equal-weight RRF lets the three keyword lists
# outvote the dense list (golden-v1 hit@10 0.47 hybrid vs 0.90 dense), so hybrid stays
# opt-in until its fusion is reweighted and measured.
RETRIEVAL = "dense"
RETRIEVAL_MODES = ("hybrid", "dense")
HYBRID_DEPTH = 50         # entries per list (dense, bm25, trigram, named) fused by RRF
EVIDENCE_K = 10           # `evidence_bundle` hits when the caller gives no k
# Hits from one note before any repeat: a turn's served slots, and an explicit bundle.
MAX_HITS_PER_NOTE = {"prefetch": 1, "explicit": 2}
MAX_LINKS = 5             # linked notes listed per hit
# Per-hit freshness against the live vault (`quoth.live`): at most this many stats and
# hashes per call, and only files up to LIVE_HASH_MAX_BYTES are hashed.
LIVE_MAX_STATS = 10
LIVE_MAX_HASHES = 3
LIVE_HASH_MAX_BYTES = 256 * 1024

# ---------------------------------------------------------------- floors (bge-m3 cosine)

CLAIM_INDEX_MIN_SIMILARITY = 0.5  # a claim counts as a meaning match at or above this
OWNER_MIN_SCORE = 0.62            # owner-lane excerpts
ASSISTANT_MIN_SCORE = 0.62        # assistant-lane excerpts

# The relevance gate's default thresholds (`relevance.Gate` fields; None = that check off).
# Calibrated on the golden sets against the real snapshot: no cosine or rerank floor
# separates unanswerable rows from answerable ones without losing answers, so only a
# conservative vault floor and gap are on.
GATE = {
    "vault_min_cosine": 0.53,
    "vault_max_gap": 0.15,
    "vault_lexical_rescue": None,
    "claims_min_cosine": None,
    "claims_min_lexical": None,
    "vault_min_rerank": None,
    "vault_max_rerank_gap": None,
    "vault_rerank_widen": None,
    "claims_min_rerank": None,
}

# ---------------------------------------------------------------- reranker

RERANK_MAX_TOKENS = 192   # query + passage tokens per pair (holds a VAULT_SNIPPET and a query)

# ---------------------------------------------------------------- background work

EPISODES_INTERVAL = 300.0       # seconds between lane ingest + embedding ticks
EPISODES_TICK_SECONDS = 60.0    # CPU budget of one lane tick
CLAIM_SYNC_INTERVAL = 60.0      # seconds between claim-index passes
PREFETCH_LOG_DAYS = 30          # prefetch log files older than this are pruned
