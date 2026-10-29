"""Full v1 catalogue; statuses are provenance, not availability guesses."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

# name: (input type, output type, status, reason, backing source)
OPS = {
    "query_guard": ("Query", "Query", "serving", None, "mcp_server.py"),
    "dense_retrieve": ("Query", "CandidateList", "serving", None, "dense.py"),
    "lexical_bm25": ("Query", "CandidateList", "lab", "Quality not measured for serving", "search.py"),
    "lexical_trigram": ("Query", "CandidateList", "lab", "Quality not measured for serving", "search.py"),
    "entity_match": ("Query", "CandidateList", "lab", "Quality not measured for serving", "search.py"),
    "query_variants": ("Query", "QueryList", "lab", "Deterministic variants only", "search.py"),
    "filter": ("CandidateList", "CandidateList", "lab", "Event time not inferred from mtime", "search.py"),
    "fuse": ("CandidateList[]", "CandidateList", "lab", "Unmeasured quality", "search.py"),
    "order": ("CandidateList", "CandidateList", "rejected_serving", "E1 temporal gate failed", "control/lab_suites.py"),
    "temporal_parse": ("Query", "TemporalInterp", "rejected_serving", "E1 temporal gate failed", "control/lab_suites.py"),
    "dedupe_diversity": ("CandidateList", "CandidateList", "rejected_serving", "Q2 protected regression", "ranking.py"),
    "expand_context": ("CandidateList", "CandidateList", "lab", "No quality gate passed", "search.py"),
    "current_state_resolver": ("CandidateList", "CandidateList", "lab", "Semantic supersession unknown", "search.py"),
    "support_check": ("EvidenceBundle", "Decision", "rejected_serving", "No validated abstention signal", "control/lab_suites.py"),
    "abstain_gate": ("Decision", "Decision", "rejected_serving", "E1 abstention gate failed", "control/lab_suites.py"),
    "evidence_bundle": ("CandidateList", "EvidenceBundle", "serving", None, "evidence.py"),
    "caller_policy": ("Query", "QueryPlan", "unavailable", "Caller policy not activated", "mcp_server.py"),
    "rerank_cross_encoder": ("CandidateList", "CandidateList", "unavailable", "No approved local cross-encoder weights", "dense.py"),
    "bge_m3_sparse": ("Query", "CandidateList", "unavailable", "ONNX sparse head absent", "dense.py"),
    "bge_m3_colbert": ("Query", "CandidateList", "unavailable", "ONNX ColBERT head absent", "dense.py"),
    "llm_rewrite": ("Query", "Query", "unavailable", "External LLM disabled in v1", "mcp_server.py"),
    "llm_answer": ("EvidenceBundle", "Decision", "unavailable", "External LLM disabled in v1", "mcp_server.py"),
    "llm_judge": ("Decision", "Decision", "unavailable", "External LLM disabled in v1", "mcp_server.py"),
}
EXECUTABLE = {"query_guard", "dense_retrieve", "lexical_bm25", "lexical_trigram",
              "filter", "fuse", "dedupe_diversity", "evidence_bundle"}


def registry_hash() -> str:
    base = Path(__file__).resolve().parents[1]
    digest = [(name, "1", hashlib.sha256((base / source).read_bytes()).hexdigest())
              for name, (_, _, _, _, source) in sorted(OPS.items())]
    return "sha256:" + hashlib.sha256(json.dumps(digest, separators=(",", ":")).encode()).hexdigest()


def catalogue() -> dict:
    return {"registry": registry_hash(), "operators": [
        {"name": name, "version": "1", "input": item[0], "output": item[1],
         "status": item[2], "available": name in EXECUTABLE,
         "reason": item[3] or (None if name in EXECUTABLE else "Not implemented in v1 lab")}
        for name, item in sorted(OPS.items())]}
