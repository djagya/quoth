"""Bounded source diversity, without inferring relevance or source authority."""
from collections import Counter

from .dense import dense_topk as _cosine_topk


def diversify(ranked, source_ids, limit, per_source=2):
    """Defer a source's third and later chunks; backfill if sources run out.

    Keep dense order within both tiers. This does not collapse adjacent chunks,
    merge excerpts, or label any source canonical. Input is a bounded pool.
    """
    counts = Counter()
    selected, deferred = [], []
    for item in ranked:
        source = source_ids[item[0]]
        counts[source] += 1
        (selected if counts[source] <= per_source else deferred).append(item)
    return (selected + deferred)[:limit]


def diverse_topk(conn, query, scopes, limit, model, embedder=None):
    """Evaluation-only alternative; deliberately NOT wired to the MCP default.

    Pool: 4*k, capped at 100 (or k when k is larger). No lexical
    fallback, metadata-based authority inference, or extra model invocation.
    """
    if limit <= 0:
        return []
    ranked = _cosine_topk(conn, query, scopes, max(limit, min(4 * limit, 100)),
                          model, embedder)
    if not ranked:
        return []
    source_ids = dict(conn.execute(
        "SELECT c.id, v.source_id FROM chunks c "
        "JOIN source_versions v ON v.id=c.source_version_id "
        f"WHERE c.id IN ({','.join('?' * len(ranked))})",
        [cid for cid, _ in ranked]))
    return diversify(ranked, source_ids, limit)
