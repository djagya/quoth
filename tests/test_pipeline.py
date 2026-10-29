"""Synthetic production parity and honest trace boundaries."""
import copy
import json

import pytest

from quoth.embed.backends import get_embedder
from quoth.embed.store import embed_chunks
from quoth.evidence import evidence_bundle
from quoth.ingest import ingest_vault
from quoth.pipeline import catalogue, production, run, spec_hash, validate
from quoth.search import search


def test_registry_hash_and_typed_validation():
    spec = production("snapshot", "digest")
    assert validate(spec, "snapshot", "digest")["status"] == "ok"
    assert spec_hash({**spec, "meta": {"ui_layout": []}}) == spec_hash(spec)
    changed = copy.deepcopy(spec)
    changed["nodes"][1]["config"]["k"] = 11
    assert spec_hash(changed) != spec_hash(spec)
    assert validate(spec, "other", "digest")["status"] == "invalid"
    changed["nodes"][1]["in"]["query"] = "missing"
    assert "input_type_or_order:dense" in validate(changed, "snapshot", "digest")["errors"]
    changed["nodes"][1]["op"] = "rerank_cross_encoder"
    assert "operator_unavailable:rerank_cross_encoder" in validate(changed, "snapshot", "digest")["errors"]
    for rejected in ("order", "temporal_parse", "support_check"):
        changed["nodes"][1]["op"] = rejected
        assert "operator_unavailable:" + rejected in validate(changed, "snapshot", "digest")["errors"]
    assert any(o["name"] == "bge_m3_colbert" and not o["available"] for o in catalogue()["operators"])
    for nodes in ([None], [{"id": [], "op": "query_guard", "op_version": "1",
                          "in": {"query": "$query"}, "config": {"max_chars": 2048}}],
                  ["not-a-node"]):
        assert validate({**spec, "nodes": nodes}, "snapshot", "digest")["status"] == "invalid"


@pytest.mark.parametrize("k", [1, 10, 20])
def test_production_trace_matches_legacy(conn, vault, k):
    ingest_vault(conn, vault)
    embedder = get_embedder("hash-ngram")
    embed_chunks(conn, embedder, batch_size=8)
    spec = production("synthetic", "digest", model="hash-ngram")
    for query in ["tomatoes basil", "platypus", "meditation", "quarterly pipeline"] * 5:
        trace, bundle, private = run(conn, spec, query, snapshot_id="synthetic", db_sha256="digest", embedder=embedder, k=k)
        legacy = evidence_bundle(search(conn, query, k=k, retriever="dense", model="hash-ngram", embedder=embedder), k=k)
        assert json.dumps(bundle, ensure_ascii=False) == json.dumps(legacy, ensure_ascii=False)
        assert [i["chunk_id"] for i in trace["trace"][1]["items"]] == list(private["dense"])
        assert [i["candidate_ref"] for i in trace["trace"][-1]["items"] if i["selected"]] == trace["output"]["selected_refs"]
        public = json.dumps(trace, ensure_ascii=False)
        assert "tomatoes basil" not in public and "Garden.md" not in public and "south bed" not in public
        assert trace["effective_k"] == k and trace["execution_hash"].startswith("sha256:")
    with pytest.raises(ValueError):
        run(conn, spec, " ", snapshot_id="synthetic", db_sha256="digest", embedder=embedder)

def test_typed_fanin_filter_and_rejected_operator(conn, vault):
    ingest_vault(conn, vault)
    embedder = get_embedder("hash-ngram")
    embed_chunks(conn, embedder, batch_size=8)
    spec = production("synthetic", "digest", model="hash-ngram")
    spec["nodes"][1]["config"]["k"] = 20
    spec["nodes"][2:2] = [
        {"id": "lex", "op": "lexical_bm25", "op_version": "1", "in": {"query": "guard"}, "config": {"k": 20}},
        {"id": "fused", "op": "fuse", "op_version": "1", "in": {"lists": ["dense", "lex"]}, "config": {"k": 20, "rrf_k": 60}},
        {"id": "filtered", "op": "filter", "op_version": "1", "in": {"in": "fused"}, "config": {"path_prefix": "ME/"}},
    ]
    spec["nodes"][-1]["in"] = {"in": "filtered"}
    assert validate(spec, "synthetic", "digest")["status"] == "ok"
    trace, bundle, _ = run(conn, spec, "tomatoes", snapshot_id="synthetic", db_sha256="digest", embedder=embedder)
    assert all(h["path"].startswith("ME/") for h in bundle["hits"])
    assert trace["trace"][3]["n_in"] >= trace["trace"][3]["n_out"]
    assert all("path" not in i for stage in trace["trace"] for i in stage["items"])
    bad = copy.deepcopy(spec)
    bad["nodes"][3]["in"]["lists"] = ["dense", "missing"]
    assert validate(bad, "synthetic", "digest")["status"] == "invalid"
    bad = copy.deepcopy(spec)
    bad["nodes"][2]["op"] = "rerank_cross_encoder"
    assert "operator_unavailable:rerank_cross_encoder" in validate(bad, "synthetic", "digest")["errors"]
