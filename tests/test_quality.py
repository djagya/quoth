"""Synthetic source-diversity counterexamples and honest metric semantics (lab scorers)."""
from unittest.mock import patch

import pytest

from quoth.control.lab import path_metrics
from quoth.control.lab_suites import facet_results
from quoth.embed.backends import get_embedder
from quoth.embed.store import embed_chunks
from quoth.evidence import ANSWER_USE, evidence_bundle
from quoth.ingest import ingest_vault
from quoth.ranking import diversify, diverse_topk
from quoth.search import search


def test_diversity_preserves_order_and_backfills():
    ranks = [(i, 1 - i / 10) for i in range(1, 7)]
    sources = {1: 'a', 2: 'a', 3: 'a', 4: 'b', 5: 'b', 6: 'a'}
    assert diversify(ranks, sources, 4) == [ranks[i] for i in (0, 1, 3, 4)]
    assert diversify(ranks, sources, 6) == [ranks[i] for i in (0, 1, 3, 4, 2, 5)]
    assert diversify(ranks, sources, 1) == ranks[:1]


def test_diversity_can_lose_decisive_third_excerpt():
    # A source cap is NOT semantic deduplication. This is why it stays opt-in.
    ranked = [(1, .9), (2, .8), (3, .7), (4, .6)]
    chosen = diversify(ranked, {1: 'lease', 2: 'lease', 3: 'lease', 4: 'other'}, 3)
    assert 3 not in {cid for cid, _ in chosen}  # decisive appendix correction
    assert 3 in {cid for cid, _ in ranked[:3]}


def test_metric_empty_gold_is_unjudged_not_abstention():
    assert path_metrics([{'path': 'unrelated'}], []) is None
    m = path_metrics([{'path': 'a'}, {'path': 'a'}, {'path': 'b'}], ['a', 'b', 'c'])
    assert m is not None
    assert m['recall@5'] == pytest.approx(2/3)
    assert m['mrr@10'] == 1
    assert m['hit@10'] == 1


def test_facet_requires_actual_bounded_text_not_just_chunk_identity():
    control = [{'name': 'explicit zero', 'requires': [[7, 'Current index count: 0.']]}]
    assert not facet_results([{'chunk_id': 7, 'snippet': 'x'*1800 + 'Current index count: 0.'}], control)['explicit zero']
    assert facet_results([{'chunk_id': 7, 'snippet': 'Current index count: 0.'}], control)['explicit zero']
    assert not facet_results([], control)['explicit zero']


def test_candidate_real_search_envelope_preserves_bounds(conn, vault):
    ingest_vault(conn, vault)
    embedder = get_embedder('hash-ngram')
    embed_chunks(conn, embedder)
    before = conn.total_changes
    with patch('quoth.dense.dense_topk', diverse_topk):
        hits = search(conn, 'tomatoes basil', k=10, retriever='dense', model='hash-ngram',
                      embedder=embedder)
    bundle = evidence_bundle(hits)
    assert bundle['answer_use'] == ANSWER_USE
    assert sum(len(h['text']) for h in bundle['hits']) <= 18000
    for h, out in zip(hits, bundle['hits'], strict=True):
        assert out['text'] == h['snippet'][:1800]
        assert out['locator'] == f"{h['path']}:{h['line_start']}-{h['line_end']}"
    assert hits[0]['path'] == 'PROJECTS/Garden.md'
    assert conn.total_changes == before
    assert diverse_topk(conn, '', (), 10, 'hash-ngram', embedder) == []
    assert diverse_topk(conn, 'x', (), 0, 'hash-ngram', embedder) == []


def test_candidate_does_not_fallback_from_failed_model(conn, vault):
    ingest_vault(conn, vault)
    embed_chunks(conn, get_embedder('hash-ngram'))
    class Broken:
        def embed_query(self, text):
            raise RuntimeError('model unavailable')
    with pytest.raises(RuntimeError, match='model unavailable'):
        diverse_topk(conn, 'basil', (), 10, 'hash-ngram', Broken())
