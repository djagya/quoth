"""Trace real pre-projection cosine ranks; retain raw evidence only in caller RAM."""
from __future__ import annotations
import hashlib
from time import monotonic
from ..dense import dense_topk
from ..evidence import evidence_bundle
from ..search import RRF_K, dense_rows, _run, unicode61_query, trigram_query
from ..ranking import diversify
from .spec import validate, spec_hash


def run(conn, spec: dict, query: str, *, snapshot_id: str, db_sha256: str,
        embedder=None, k: int | None = None) -> tuple[dict, dict, dict]:
    check = validate(spec, snapshot_id, db_sha256)
    if check["status"] != "ok":
        raise ValueError("invalid pipeline: " + ",".join(check["errors"]))
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2048:
        raise ValueError("invalid query")
    if k is not None and (type(k) is not int or not 1 <= k <= 20):
        raise ValueError("invalid k")
    start = monotonic()
    trace, values, private = [], {"$query": query}, {}
    for node in spec["nodes"]:
        begin = monotonic()
        op, ident = node["op"], node["id"]
        source = next(iter(node["in"].values()))
        dropped = []
        if op == "query_guard":
            values[ident] = values[source]
            items = []
            count_in = count_out = 1
        elif op == "dense_retrieve":
            limit = k if k is not None and len(spec["nodes"]) == 3 else node["config"]["k"]
            model = node["config"]["model"]
            ranked = dense_topk(conn, values[source], (), limit, model, embedder)
            rows = dense_rows(conn, values[source], limit, model, embedder, ranked=ranked)
            values[ident] = [{**r, "score": round(1 / (RRF_K + rank), 6), "retriever": "dense"}
                             for rank, r in enumerate(rows, 1)]
            items = [{"chunk_id": cid, "candidate_ref": str(cid), "rank": rank,
                      "score": score, "score_kind": "cosine", "selected": True}
                     for rank, (cid, score) in enumerate(ranked, 1)]
            count_in, count_out = 1, len(items)
            private[ident] = {r["chunk_id"]: r for r in values[ident]}
        elif op in {"lexical_bm25", "lexical_trigram"}:
            table, build = (("chunks_fts", unicode61_query) if op == "lexical_bm25"
                            else ("chunks_fts_trigram", trigram_query))
            match = build(values[source])
            rows = _run(conn, table, match, node["config"]["k"]) if match else []
            values[ident] = [{**dict(r), "score": round(1 / (RRF_K + rank), 6),
                              "retriever": op} for rank, r in enumerate(rows, 1)]
            items = [{"chunk_id": r["chunk_id"], "candidate_ref": str(r["chunk_id"]),
                      "rank": rank, "score": r["bm25"], "score_kind": "bm25", "selected": True}
                     for rank, r in enumerate(rows, 1)]
            count_in, count_out = 1, len(items)
            private[ident] = {r["chunk_id"]: r for r in values[ident]}
        elif op in {"filter", "fuse", "dedupe_diversity"}:
            if op == "fuse":
                sources = [values[s] for s in source]
                fused = {}
                for rows in sources:
                    for rank, row in enumerate(rows, 1):
                        cid = row["chunk_id"]
                        if cid not in fused:
                            fused[cid] = {**row, "score": 0.0, "retriever": ""}
                        fused[cid]["score"] += 1 / (60 + rank)
                        fused[cid]["retriever"] += ("+" if fused[cid]["retriever"] else "") + row["retriever"]
                incoming = list(fused.values())
                rows = sorted(incoming, key=lambda r: (-r["score"], r["chunk_id"]))[:node["config"]["k"]]
                rows = [{**r, "score": round(r["score"], 6)} for r in rows]
                count_in = sum(map(len, sources))
            else:
                incoming = values[source]
                count_in = len(incoming)
                if op == "filter":
                    rows = [r for r in incoming if r["path"].startswith(node["config"]["path_prefix"])]
                else:
                    ids = [r["chunk_id"] for r in incoming]
                    source_ids = dict(conn.execute(
                        "SELECT c.id, v.source_id FROM chunks c JOIN source_versions v ON v.id=c.source_version_id "
                        f"WHERE c.id IN ({','.join('?' * len(ids))})", ids)) if ids else {}
                    ranked = diversify([(r["chunk_id"], r["score"]) for r in incoming], source_ids,
                                       node["config"]["k"], node["config"]["per_source"])
                    by_id = {r["chunk_id"]: r for r in incoming}
                    rows = [by_id[cid] for cid, _ in ranked]
            values[ident] = rows
            chosen = {r["chunk_id"] for r in rows}
            dropped = [{"chunk_id": r["chunk_id"], "reason": "filter" if op == "filter" else "limit"}
                       for r in incoming if r["chunk_id"] not in chosen][:50]
            items = [{"chunk_id": r["chunk_id"], "candidate_ref": str(r["chunk_id"]),
                      "rank": rank, "score": r["score"], "score_kind": "rrf_rank", "selected": True}
                     for rank, r in enumerate(rows, 1)][:50]
            count_out = len(rows)
            private[ident] = {r["chunk_id"]: r for r in rows}
        elif op == "evidence_bundle":
            rows = values[source]
            values[ident] = evidence_bundle(rows, k=k if k is not None else node["config"]["k"])
            selected = {r["path"] + ":" + str(r["line_start"]) + "-" + str(r["line_end"])
                        for r in values[ident]["hits"]}
            items = [{"chunk_id": r["chunk_id"], "candidate_ref": str(r["chunk_id"]),
                      "rank": rank, "score": r["score"], "score_kind": "rrf_rank",
                      "selected": f'{r["path"]}:{r["line_start"]}-{r["line_end"]}' in selected}
                     for rank, r in enumerate(rows, 1)]
            count_in, count_out = len(rows), len(values[ident]["hits"])
            dropped = [{"chunk_id": item["chunk_id"], "reason": "projection"} for item in items if not item["selected"]][:50]
            private[ident] = {r["chunk_id"]: r for r in rows}
        else:
            raise ValueError("operator unavailable")
        trace.append({"node": ident, "op_version": "1", "op": op, "n_in": count_in,
                      "n_out": count_out, "ms": round((monotonic() - begin) * 1000, 3),
                      "items": items[:50], "dropped": dropped,
                      "dropped_count": len(dropped) if op in {"filter", "fuse", "dedupe_diversity"} else
                      max(0, count_in - count_out) if op == "evidence_bundle" else 0})
        if (monotonic() - start) * 1000 > 5000:
            raise TimeoutError("budget_exceeded")
    bundle = values[spec["output"]]
    effective_k = k if k is not None else spec["nodes"][-1]["config"]["k"]
    execution_hash = hashlib.sha256((spec_hash(spec) + ":k=" + str(effective_k)).encode()).hexdigest()
    return ({"pipeline_hash": spec_hash(spec), "execution_hash": "sha256:" + execution_hash,
             "effective_k": effective_k, "snapshot_id": snapshot_id, "registry": spec["registry"],
             "trace": trace, "query_decision": {"class": "unknown", "action": "not_evaluated",
             "reason": "caller_policy_not_observed", "source": "not_observed"},
             "temporal": {"intent": "unknown", "time_basis": "unknown", "operator": "none", "qualification": "unsupported"},
             "output": {"selected_refs": [str(r["chunk_id"]) for r in values[next(iter(spec["nodes"][-1]["in"].values()))] if
                       any(i["candidate_ref"] == str(r["chunk_id"]) and i["selected"] for i in trace[-1]["items"])],
                        "answer_use_contract_version": "current", "answer_use_applied": "not_observed",
                        "answer_result": "not_evaluated", "abstention": "unknown", "evidence_budget": 18000},
             "latency": {"compute_ms": round((monotonic() - start) * 1000, 3)}}, bundle, private)
