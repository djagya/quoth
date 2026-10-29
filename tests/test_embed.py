"""The embedder input and the ONNX embedder's batching, with a stand-in tokenizer and
session (no model files)."""
import json
from types import SimpleNamespace

import numpy as np

from quoth.db import open_db
from quoth.embed.backends import MAX_TOKENS, OnnxEmbedder
from quoth.embed.store import embed_chunks
from quoth.ingest import ingest_vault


class Tokenizer:
    """One token per character (its code point), wrapped in <s> ... </s>."""

    def encode_batch(self, texts):
        return [SimpleNamespace(ids=[0, *(ord(c) for c in t), 2]) for t in texts]


class Session:
    """CLS vector = (sum of the real token ids, number of real tokens, 0, ...)."""

    def __init__(self, dim):
        self.dim, self.shapes = dim, []

    def run(self, _outputs, feed):
        ids, mask = feed["input_ids"], feed["attention_mask"]
        self.shapes.append(ids.shape)
        out = np.zeros((*ids.shape, self.dim), dtype=np.float32)
        out[:, 0, 0] = (ids * mask).sum(axis=1)
        out[:, 0, 1] = mask.sum(axis=1)
        return [out]


def embedder(batch_size=4, dim=8):
    e = object.__new__(OnnxEmbedder)
    e.spec = SimpleNamespace(name="bge-m3", dim=dim, passage_prefix="", query_prefix="")
    e.batch_size, e._tok, e._sess = batch_size, Tokenizer(), Session(dim)
    return e


def raw(text):
    ids = [0, *(ord(c) for c in text), 2][:MAX_TOKENS]
    v = np.zeros(8, dtype=np.float32)
    v[0], v[1] = sum(ids), len(ids)
    return v / np.linalg.norm(v)


def test_vectors_keep_input_order_and_read_every_token():
    e = embedder()
    texts = ["x" * 700, "short", "y" * 30, "", "z" * 600, "w" * 5]
    out = e.embed_passages(texts)
    assert out.shape == (len(texts), 8)
    for text, vec in zip(texts, out):
        np.testing.assert_allclose(vec, raw(text), rtol=1e-6)   # 700 > 512 chars: nothing cut


def test_batches_group_similar_lengths_and_bound_attention_work():
    e = embedder(batch_size=4)
    texts = ["a" * n for n in (900, 3, 1200, 10, 5, 2000, 7, 1)]
    e.embed_passages(texts)
    widths = [w for _, w in e._sess.shapes]
    assert widths == sorted(widths)                           # shortest first
    assert sum(b for b, _ in e._sess.shapes) == len(texts)
    for b, w in e._sess.shapes:
        assert b <= e.batch_size and (b == 1 or b * w * w <= e.batch_size * 512 * 512)
    assert e._sess.shapes[0] == (4, 9)                        # the four short texts together


def test_tokens_past_the_model_limit_are_dropped():
    e = embedder(batch_size=2)
    e.embed_passages(["q" * (MAX_TOKENS + 50)])
    assert e._sess.shapes == [(1, MAX_TOKENS)]


class Recorder:
    spec = SimpleNamespace(name="bge-m3", dim=4)

    def __init__(self):
        self.batches = []

    def embed_passages(self, texts):
        self.batches.append(list(texts))
        return np.ones((len(texts), 4), dtype="<f4") / 2


def test_embed_chunks_runs_shortest_first(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "long.md").write_text("# Long\n" + "word " * 200 + "\n")
    (vault / "short.md").write_text("# Short\nA line.\n")
    (vault / "mid.md").write_text("# Mid\n" + "word " * 40 + "\n")
    conn = open_db(tmp_path / "m.db")
    ingest_vault(conn, vault)
    rec = Recorder()
    stats = embed_chunks(conn, rec, batch_size=2)
    order = [t for batch in rec.batches for t in batch]
    assert [len(t) for t in order] == sorted(len(t) for t in order)
    assert stats.embedded == 3 and [len(b) for b in rec.batches] == [2, 1]
    assert conn.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 3


def test_passage_text_carries_title_aliases_and_heading_path():
    from quoth.chunker import note_aliases, passage_text

    assert passage_text("PEOPLE/Orla Quenby.md", "Orla Quenby", "text") == "Orla Quenby\ntext"
    assert passage_text("PEOPLE/Orla Quenby.md", "Contact", "t") == "Orla Quenby — Contact\nt"
    meta = '{"aliases": ["Orla", "OQ", "Orla Quenby", "Orla"]}'
    assert passage_text("PEOPLE/Orla Quenby.md", "Contact", "t", meta) == (
        "Orla Quenby (Orla, OQ) — Contact\nt")
    assert note_aliases('{"alias": "  The   cat  "}') == ["The cat"]
    assert note_aliases('{"aliases": []}') == note_aliases("{}") == note_aliases(None) == []
    assert note_aliases('{"aliases": [3, null, ""]}') == note_aliases("not json") == []
    many = json.dumps({"aliases": [f"name {i}" for i in range(20)] + ["x" * 200]})
    assert note_aliases(many) == [f"name {i}" for i in range(8)]
    assert note_aliases(json.dumps({"alias": "y" * 200})) == ["y" * 80]


def test_aliases_reach_the_embedder_and_the_reuse_key(tmp_path):
    vault = tmp_path / "vault"
    (vault / "PEOPLE").mkdir(parents=True)
    note = vault / "PEOPLE" / "Orla Quenby.md"
    note.write_text("---\naliases: [Orla, OQ]\n---\n# Orla Quenby\nRuns the pottery studio.\n")
    conn = open_db(tmp_path / "m.db")
    ingest_vault(conn, vault)
    rec = Recorder()
    embed_chunks(conn, rec)
    assert rec.batches == [["Orla Quenby (Orla, OQ)\n# Orla Quenby\nRuns the pottery studio."]]
    note.write_text("---\naliases: [Orla]\n---\n# Orla Quenby\nRuns the pottery studio.\n")
    ingest_vault(conn, vault)
    again = Recorder()
    assert embed_chunks(conn, again).reused == 0 and again.batches[0][0].startswith("Orla Quenby (Orla)\n")
