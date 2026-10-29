"""Offline parity check: the production pipeline must return byte-identical bundles to the
direct dense path. Run only against an explicitly supplied frozen DB; it is never written."""
import argparse
import hashlib
import json
from pathlib import Path

from .. import paths
from ..db import connect_read_only, validate_evidence_db
from ..embed.backends import get_embedder
from ..evidence import evidence_bundle
from ..pipeline import production, run
from ..search import search
from .api import file_digest


def check(db: Path, questions: Path, cache: Path) -> dict:
    before = file_digest(db)
    queries = [json.loads(line)["question"] for line in questions.read_text().splitlines() if line.strip()]
    embedder = get_embedder("bge-m3", cache_dir=str(cache), threads=2)
    spec = production(db.stem, before)
    mismatches = 0
    with connect_read_only(db) as conn:
        validate_evidence_db(conn)
        for query in queries:
            _, bundle, _ = run(conn, spec, query, snapshot_id=db.stem, db_sha256=before, embedder=embedder)
            direct = evidence_bundle(search(conn, query, k=10, retriever="dense", model="bge-m3", embedder=embedder), k=10)
            mismatches += (json.dumps(bundle, ensure_ascii=False) != json.dumps(direct, ensure_ascii=False))
    after = file_digest(db)
    return {"queries": len(queries), "mismatches": mismatches, "db_sha256": after,
            "unchanged": before == after, "passed": before == after and mismatches == 0 and len(queries) > 0}


def main():
    parser = argparse.ArgumentParser(description="Read-only byte-parity check: the production pipeline against the direct dense path")
    parser.add_argument("--db", type=Path, required=True, help="frozen snapshot DB (only read)")
    parser.add_argument("--questions", type=Path, required=True,
                        help="JSONL file whose rows each carry a `question`")
    parser.add_argument("--model-cache", type=Path, default=None,
                        help="local model root (default $QUOTH_MODEL_CACHE)")
    args = parser.parse_args()
    result = check(args.db, args.questions, args.model_cache or paths.model_cache())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
