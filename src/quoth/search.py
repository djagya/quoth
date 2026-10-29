"""Lexical hybrid retrieval: BM25 over unicode61 + trigram FTS5, fused with RRF.

An `entity` list is fused the same RRF way when the query contains entity tokens.
"""

from __future__ import annotations

import re
import sqlite3

RRF_K = 60
CANDIDATES = 100
BM25_WEIGHTS = (5.0, 2.0, 1.0)  # title, heading_path, text
SNIPPET_TOKENS = 24

_WORD = re.compile(r"\w+", re.UNICODE)


def _terms(query: str) -> list[str]:
    seen, out = set(), []
    for t in _WORD.findall(query.lower()):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _q(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def unicode61_query(query: str) -> str | None:
    terms = _terms(query)
    # Prefix match lets English/Russian inflections that share the full query form match.
    return " OR ".join(f"{_q(t)}*" for t in terms) or None


def trigram_query(query: str) -> str | None:
    """Substring query; long words are cut to a stem so Russian inflections still match."""
    parts = []
    for t in _terms(query):
        if len(t) < 3:
            continue
        stem = t if len(t) < 5 else t[: max(4, len(t) - 2)]
        parts.append(_q(stem))
    return " OR ".join(dict.fromkeys(parts)) or None


def _run(conn, table: str, match: str, limit: int, *, where: str = "", params: tuple = (),
         ids_only: bool = False):
    """One FTS5 list, best BM25 first. `where` (an SQL fragment starting with " AND", over
    `s` = sources) pre-filters the candidates; `ids_only` skips the scores and snippet."""
    w = ", ".join(str(x) for x in BM25_WEIGHTS)
    columns = (f"c.id AS chunk_id" if ids_only else
               f"c.id AS chunk_id, s.path, c.heading_path, c.line_start, c.line_end, s.scope,"
               f" bm25({table}, {w}) AS bm25,"
               f" snippet({table}, 2, '**', '**', ' … ', {SNIPPET_TOKENS}) AS snippet")
    sql = (
        f"SELECT {columns}"
        f" FROM {table} f"
        f" JOIN chunks c ON c.id = f.rowid"
        f" JOIN source_versions v ON v.id = c.source_version_id AND v.is_current = 1"
        f" JOIN sources s ON s.id = v.source_id"
        f" WHERE {table} MATCH ?{where}"
        f" ORDER BY bm25({table}, {w}) LIMIT ?"
    )
    return conn.execute(sql, (match, *params, limit)).fetchall()


def lexical_lists(conn, query: str, limit: int, *, where: str = "", params: tuple = ()) -> dict[str, list[int]]:
    """{retriever name: chunk ids best first} for the BM25 and trigram lists (serving's
    hybrid retrieval fuses these with the dense list)."""
    out = {}
    for name, table, build in RETRIEVERS:
        match = build(query)
        out[name] = [r[0] for r in _run(conn, table, match, limit, where=where, params=params,
                                        ids_only=True)] if match else []
    return out


RETRIEVERS = (("bm25", "chunks_fts", unicode61_query), ("trigram", "chunks_fts_trigram", trigram_query))

RETRIEVER_MODES = ("lexical", "dense")

def dense_rows(conn, query, limit, model, embedder, *, ranked=None):
    from .dense import dense_topk
    if ranked is None:
        ranked = dense_topk(conn, query, (), limit, model, embedder)
    if not ranked:
        return []
    ids = [cid for cid, _ in ranked]
    meta = {r["chunk_id"]: dict(r) for r in conn.execute(
        "SELECT c.id AS chunk_id, s.path, c.heading_path, c.line_start,"
        " c.line_end, s.scope, substr(c.text, 1, 1800) AS snippet"
        " FROM chunks c JOIN source_versions v ON v.id=c.source_version_id AND v.is_current=1"
        " JOIN sources s ON s.id=v.source_id"
        f" WHERE c.id IN ({','.join('?' * len(ids))})", ids)}
    return [meta[cid] for cid in ids]

_ENTITY_SQL = (
    "SELECT c.id AS chunk_id, s.path, c.heading_path, c.line_start, c.line_end, s.scope,"
    " SUM(e.cnt) AS weight"
    " FROM entities e"
    " JOIN chunks c ON c.id = e.chunk_id"
    " JOIN source_versions v ON v.id = c.source_version_id AND v.is_current = 1"
    " JOIN sources s ON s.id = v.source_id"
    " WHERE e.entity IN (%s)"
    " GROUP BY c.id ORDER BY weight DESC, c.id LIMIT ?"
)


def _run_entity(conn, ents: list[str], limit: int):
    sql = _ENTITY_SQL % ",".join("?" * len(ents))
    rows = conn.execute(sql, (*ents, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # cheap snippet: first 160 chars of the chunk text
        txt = conn.execute("SELECT text FROM chunks WHERE id=?", (d["chunk_id"],)).fetchone()[0]
        d["snippet"] = (txt[:160] + " …") if len(txt) > 160 else txt
        out.append(d)
    return out


def lexical_search(conn: sqlite3.Connection, query: str, k: int = 10) -> list[dict]:
    """Two-list baseline (bm25+trigram) without the entity signal."""
    return _search(conn, query, k=k, use_entities=False)


def _search(conn, query, k, use_entities, retriever: str = "lexical", model: str | None = None,
            embedder=None):
    if retriever not in RETRIEVER_MODES:
        raise ValueError(f"unknown retriever {retriever!r}")
    if k <= 0:
        return []
    if retriever == "dense":
        if model is None:
            from .embed.backends import DEFAULT_MODEL
            model = DEFAULT_MODEL
        rows = dense_rows(conn, query, k, model, embedder)
        return [{**r, "score": round(1 / (RRF_K + rank), 6), "retriever": "dense"}
                for rank, r in enumerate(rows, 1)]
    fused: dict[int, dict] = {}

    def add(rows, name):
        for rank, r in enumerate(rows, start=1):
            hit = fused.get(r["chunk_id"])
            if hit is None:
                hit = fused[r["chunk_id"]] = {
                    "chunk_id": r["chunk_id"], "path": r["path"], "heading_path": r["heading_path"],
                    "line_start": r["line_start"], "line_end": r["line_end"], "scope": r["scope"],
                    "score": 0.0, "snippet": r["snippet"], "_retrievers": [],
                }
            hit["score"] += 1.0 / (RRF_K + rank)
            hit["_retrievers"].append(name)

    for name, table, build in RETRIEVERS:
        match = build(query)
        if not match:
            continue
        add(_run(conn, table, match, max(CANDIDATES, k)), name)

    if use_entities:
        from .entities import query_entities

        ents = query_entities(query)
        if ents:
            add(_run_entity(conn, ents, max(CANDIDATES, k)), "entity")

    ranked = sorted(fused.values(), key=lambda h: (-h["score"], h["chunk_id"]))[:k]
    for h in ranked:
        h["score"] = round(h["score"], 6)
        h["retriever"] = "+".join(h.pop("_retrievers"))
    return ranked


def search(
    conn: sqlite3.Connection, query: str, k: int = 10,
    retriever: str = "lexical", model: str | None = None, embedder=None,
) -> list[dict]:
    """Full retrieval over every scope; scope labels never filter results."""
    return _search(conn, query, k=k, use_entities=True, retriever=retriever, model=model,
                   embedder=embedder)
