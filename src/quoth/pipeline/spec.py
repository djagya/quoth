"""Immutable pipeline identity and fail-closed typed DAG validation."""
import hashlib
import json
import re
from .registry import OPS, EXECUTABLE, registry_hash


def spec_hash(spec: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps({k: v for k, v in spec.items() if k != "meta"},
        sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def production(snapshot_id: str, db_sha256: str, *, model: str = "bge-m3") -> dict:
    return {"schema": "quoth-pipeline/v1", "name": "production-dense", "parent": None,
            "snapshot": {"id": snapshot_id, "db_sha256": db_sha256}, "registry": registry_hash(),
            "nodes": [
                {"id": "guard", "op": "query_guard", "op_version": "1", "in": {"query": "$query"}, "config": {"max_chars": 2048}},
                {"id": "dense", "op": "dense_retrieve", "op_version": "1", "in": {"query": "guard"}, "config": {"model": model, "k": 10}},
                {"id": "out", "op": "evidence_bundle", "op_version": "1", "in": {"in": "dense"}, "config": {"k": 10}},
            ], "output": "out", "budget": {"trace_ms": 5000, "max_candidates_per_edge": 200}}


def validate(spec: dict, snapshot_id: str, db_sha256: str) -> dict:
    from ..embed.backends import MODELS
    errors = []
    if not isinstance(spec, dict) or set(spec) - {"schema", "name", "parent", "snapshot", "registry", "nodes", "output", "budget", "meta"}:
        return {"status": "invalid", "errors": ["shape"]}
    if spec.get("schema") != "quoth-pipeline/v1" or spec.get("registry") != registry_hash() or spec.get("snapshot") != {"id": snapshot_id, "db_sha256": db_sha256}:
        errors.append("snapshot_or_registry_mismatch")
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 24:
        return {"status": "invalid", "errors": errors + ["node_limit"]}
    types = {"$query": "Query"}
    if sum(isinstance(n, dict) and n.get("op") in {"dense_retrieve", "lexical_bm25", "lexical_trigram"} for n in nodes) > 4:
        errors.append("retrieval_limit")
    if (not isinstance(spec.get("name"), str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", spec["name"])
            or not isinstance(spec.get("budget"), dict) or "meta" in spec
            or spec.get("parent") is not None and (not isinstance(spec["parent"], str) or
                not re.fullmatch(r"sha256:[0-9a-f]{64}", spec["parent"]))):
        errors.append("metadata_or_budget")
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"id", "op", "op_version", "in", "config"}:
            errors.append("node_shape")
            continue
        name, op, inputs, config = node["id"], node["op"], node["in"], node["config"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name) or name in types:
            errors.append("duplicate_or_invalid_id")
            continue
        if not isinstance(op, str) or op not in OPS or node["op_version"] != "1":
            errors.append("unknown_operator")
            continue
        if op not in EXECUTABLE:
            errors.append("operator_unavailable:" + op)
        if not isinstance(inputs, dict) or not isinstance(config, dict):
            errors.append("ports_or_config")
            continue
        expected_port = "lists" if op == "fuse" else "in" if OPS[op][0] == "CandidateList" else "query"
        source = inputs.get(expected_port)
        if op == "fuse":
            valid_input = isinstance(source, list) and 2 <= len(source) <= 4 and all(
                isinstance(s, str) and types.get(s) == "CandidateList" for s in source)
        else:
            valid_input = isinstance(source, str) and types.get(source) == OPS[op][0]
        if set(inputs) != {expected_port} or not valid_input:
            errors.append("input_type_or_order:" + name)
        if op == "query_guard":
            valid_config = config == {"max_chars": 2048}
        elif op == "filter":
            valid_config = set(config) == {"path_prefix"} and isinstance(config.get("path_prefix"), str) and len(config["path_prefix"]) <= 120 and not any(c in config["path_prefix"] for c in "%_")
        elif op == "dedupe_diversity":
            valid_config = set(config) == {"k", "per_source"} and type(config.get("per_source")) is int and 1 <= config["per_source"] <= 10
        elif op == "fuse":
            valid_config = set(config) == {"k", "rrf_k"} and config.get("rrf_k") == 60
        else:
            valid_config = set(config) == ({"model", "k"} if op == "dense_retrieve" else {"k"}) and (op != "dense_retrieve" or isinstance(config.get("model"), str) and config["model"] in MODELS)
        if op not in {"query_guard", "filter"}:
            valid_config = valid_config and type(config.get("k")) is int and 1 <= config["k"] <= (20 if op == "evidence_bundle" else 200)
        if not valid_config:
            errors.append("config:" + name)
        types[name] = OPS[op][1]
    if not isinstance(spec.get("output"), str) or types.get(spec["output"]) != "EvidenceBundle" or spec["output"] != (nodes[-1].get("id") if isinstance(nodes[-1], dict) else None):
        errors.append("output_type")
    if not isinstance(nodes[0], dict) or nodes[0].get("op") != "query_guard" or not isinstance(nodes[-1], dict) or nodes[-1].get("op") != "evidence_bundle":
        errors.append("graph_boundary")
    if spec.get("budget") != {"trace_ms": 5000, "max_candidates_per_edge": 200}:
        errors.append("budget")
    return {"status": "invalid" if errors else "ok", "errors": errors, "hash": spec_hash(spec) if not errors else None}
