"""`quoth` command line.

Conventions shared by every command:

- Commands with a human-readable form print text by default and JSON with ``--json``.
  Commands whose result is itself a JSON document (refresh receipts, bundle manifests,
  freshness reports) always print JSON.
- Errors go to stderr. Exit codes: 0 ok, 1 runtime failure, 2 usage error, and 3 or
  more for command-specific states listed in that command's help.
- Default locations come from ``quoth.paths`` (``QUOTH_HOME``), resolved when the
  command runs.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from . import paths

EVIDENCE_DB = "memory.db"

# Exit codes of `quoth freshness` / `quoth successor`, by snapshot state.
FRESHNESS_EXIT = {"fresh": 0, "stale": 3, "incompatible": 4, "invalid": 5}
FRESHNESS_EPILOG = ("exit codes: 0 fresh, 1 runtime failure, 2 usage error, 3 stale,"
                    " 4 incompatible, 5 invalid")

# Subcommands that own their argument parsing: name -> (module, help).
DELEGATED = {
    "refresh-nightly": ("nightly", "build a successor snapshot, gate it on retrieval quality,"
                                   " and switch serving to it when it passes"),
    "heartbeat": ("heartbeat", "check the nightly refresh and the serving snapshot are recent"
                               " (exit 0 green, 1 red)"),
    "ledger": ("ledger.cli", "the reviewed memory ledger: propose, review, check, compile the core"),
    "sidecar": ("sidecar", "serve memory to the Hermes memory provider over a Unix socket"),
    "episodes": ("episodes", "episode lanes: ingest, search or inspect what the assistant said"
                             " and what the owner wrote"),
    "eval": ("evals.cli", "eval sets: run, validate, convert, compare, sweep"),
    "hermes-plugin": ("hermes_install", "install or locate the Hermes memory-provider plugin"),
    "health": ("health", "check the install: venv, heartbeat, sidecar, review queue, episode lanes"),
    "rerank": ("rerank", "the prefetch gate's cross-encoder: check it loads, time it"),
    "models": ("models", "download and verify the local ONNX model files (embedder, reranker)"),
}


def _evidence_db(value: str | None) -> Path:
    return Path(value) if value is not None else paths.state_file(EVIDENCE_DB)


def _fail(command: str, error: BaseException | str, code: int = 1) -> int:
    print(f"quoth {command}: {error}", file=sys.stderr)
    return code


def _cmd_ingest(a) -> int:
    from .db import open_db
    from .ingest import ingest_vault

    t0 = time.monotonic()
    try:
        st = ingest_vault(open_db(_evidence_db(a.db)), a.vault)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as e:
        return _fail("ingest", e)
    elapsed = time.monotonic() - t0
    if a.json:
        print(json.dumps({**st.as_dict(), "seconds": round(elapsed, 3)}, ensure_ascii=False))
    else:
        print(f"new={st.new} changed={st.changed} unchanged={st.unchanged} removed={st.removed}"
              f" chunks_added={st.chunks_added} errors={len(st.errors)} seconds={elapsed:.2f}")
    for e in st.errors:
        print(f"quoth ingest: {e}", file=sys.stderr)
    return 1 if st.errors else 0


def _cmd_search(a) -> int:
    from .db import connect_read_only
    from .search import search

    try:
        hits = search(connect_read_only(_evidence_db(a.db)), a.query, k=a.top_k)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as e:
        return _fail("search", e)
    if a.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
    else:
        for i, h in enumerate(hits, 1):
            loc = f"{h['path']}:{h['line_start']}-{h['line_end']}"
            head = f" # {h['heading_path']}" if h["heading_path"] else ""
            print(f"{i:>2}. [{h['score']:.4f} {h['retriever']} {h['scope']}] {loc}{head}")
            print(f"    {h['snippet'].replace(chr(10), ' ')}")
    return 0


def _cmd_embed(a) -> int:
    from .db import open_db
    from .embed.backends import get_embedder
    from .embed.store import embed_chunks

    try:
        embedder = get_embedder(a.model, threads=a.threads)
        stats = embed_chunks(open_db(_evidence_db(a.db)), embedder,
                             batch_size=a.batch_size, limit=a.limit)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as e:
        return _fail("embed", e)
    out = stats.as_dict()
    if a.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(" ".join(f"{k}={v}" for k, v in out.items()))
    return 0


def _cmd_refresh(a) -> int:
    from .refresh import refresh
    try:
        result = refresh(Path(a.source), Path(a.target), Path(a.vault),
                         resume=a.resume, retries=a.retries, threads=a.threads,
                         batch_size=a.batch_size,
                         allow_scope_contract_change=a.allow_scope_contract_change,
                         allow_index_contract_rebase=a.allow_index_contract_rebase)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, json.JSONDecodeError) as e:
        return _fail("refresh", e)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _cmd_snapshot(a) -> int:
    from .refresh import snapshot
    try:
        result = snapshot(Path(a.target), Path(a.vault), resume=a.resume, retries=a.retries,
                          threads=a.threads, batch_size=a.batch_size)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, json.JSONDecodeError) as e:
        return _fail("snapshot", e)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _cmd_freshness(a) -> int:
    from .freshness import Rejected, status, successor
    try:
        if a.cmd == "freshness":
            result = status(Path(a.source), Path(a.vault))
            state = result["state"]
        else:
            result = successor(Path(a.source), Path(a.target), Path(a.vault),
                               resume=a.resume, retries=a.retries,
                               threads=a.threads, batch_size=a.batch_size,
                               allow_index_contract_rebase=a.allow_index_contract_rebase)
            state = result["status"]["state"]
    except Rejected as exc:
        result, state = {"state": exc.state, "reasons": [exc.reason]}, exc.state
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError):
        result, state = {"state": "invalid", "reasons": ["successor_failed_resume_or_inspect"]}, "invalid"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return FRESHNESS_EXIT[state]


def _cmd_bundle(a) -> int:
    from .bundle import export_bundle, import_bundle, validate_bundle
    try:
        if a.bundle_cmd == "export":
            result = export_bundle(a.source, a.target, scope=a.scope, resume=a.resume)
        elif a.bundle_cmd == "validate":
            result = validate_bundle(a.db, a.manifest)
        else:
            result = import_bundle(a.db, a.target, a.manifest)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, json.JSONDecodeError) as e:
        return _fail(f"bundle {a.bundle_cmd}", e)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _cmd_mcp(a) -> int:
    from .mcp_server import _exit_now, main as mcp_main
    _exit_now(mcp_main(["--db", str(_evidence_db(a.db))]))
    return 0  # not reached


def _db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=None,
                        help=f"evidence DB (default $QUOTH_HOME/{EVIDENCE_DB})")


def _embed_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--threads", type=int, default=4, help="embedding threads (default 4)")
    parser.add_argument("--batch-size", type=int, default=32, help="chunks per embedding batch (default 32)")


REBASE_HELP = ("accept a source snapshot whose only index-code changes are ones declared not to"
               " alter stored chunks or vectors (one explicit successor)")


def build_parser() -> argparse.ArgumentParser:
    from .embed.backends import DEFAULT_MODEL, MODELS

    p = argparse.ArgumentParser(
        prog="quoth", description="Local evidence retrieval and reviewed memory for an AI assistant.",
        epilog="exit codes: 0 ok, 1 runtime failure, 2 usage error; 3 and up are listed per command")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    for name, (_, help_text) in DELEGATED.items():
        sub.add_parser(name, help=help_text, add_help=False)

    pi = sub.add_parser("ingest", help="index a Markdown vault into the evidence DB (never writes the vault)")
    pi.add_argument("--vault", required=True, help="vault directory to index")
    _db_arg(pi)
    pi.add_argument("--json", action="store_true", help="print the counts as JSON")
    pi.set_defaults(func=_cmd_ingest)

    pem = sub.add_parser("embed", help="fill missing chunk vectors from a locally cached model")
    _db_arg(pem)
    pem.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(MODELS),
                     help=f"embedding model (default {DEFAULT_MODEL})")
    _embed_args(pem)
    pem.add_argument("--limit", type=int, default=None, help="embed at most N chunks")
    pem.add_argument("--json", action="store_true", help="print the counts as JSON")
    pem.set_defaults(func=_cmd_embed)

    ps = sub.add_parser("search", help="lexical search of the evidence DB (BM25 and trigram, fused)")
    ps.add_argument("query")
    ps.add_argument("-k", "--top-k", type=int, default=10, help="number of hits (default 10)")
    _db_arg(ps)
    ps.add_argument("--json", action="store_true", help="print the hits as JSON")
    ps.set_defaults(func=_cmd_search)

    psn = sub.add_parser("snapshot", help="build a first frozen snapshot of a vault from scratch;"
                                          " prints the receipt as JSON")
    psn.add_argument("--target", required=True, help="new output DB; must not exist yet")
    psn.add_argument("--vault", required=True, help="vault directory")
    psn.add_argument("--resume", action="store_true",
                     help="continue this exact interrupted build from its checkpoint")
    psn.add_argument("--retries", type=int, default=2, help="embedding retries (default 2)")
    _embed_args(psn)
    psn.set_defaults(func=_cmd_snapshot)

    pref = sub.add_parser("refresh", help="build a new frozen snapshot from a frozen source and"
                                          " the current vault; prints the receipt as JSON")
    pref.add_argument("--source", required=True, help="frozen source DB, with its .manifest.json beside it")
    pref.add_argument("--target", required=True, help="new output DB; an existing frozen snapshot is never overwritten")
    pref.add_argument("--vault", required=True, help="vault directory")
    pref.add_argument("--resume", action="store_true", help="resume this exact unfinished target")
    pref.add_argument("--retries", type=int, default=2, help="embedding retries (default 2)")
    _embed_args(pref)
    pref.add_argument("--allow-scope-contract-change", action="store_true",
                      help="accept a source whose only code changes are in the scope configuration")
    pref.add_argument("--allow-index-contract-rebase", action="store_true", help=REBASE_HELP)
    pref.set_defaults(func=_cmd_refresh)

    helps = {"freshness": "report whether a snapshot matches the current vault and code; prints JSON",
             "successor": "build a successor snapshot when the source is stale; prints JSON"}
    for command in ("freshness", "successor"):
        pf = sub.add_parser(command, help=helps[command], description=helps[command],
                            epilog=FRESHNESS_EPILOG)
        pf.add_argument("--source", required=True, help="frozen snapshot DB")
        pf.add_argument("--vault", required=True, help="vault directory")
        if command == "successor":
            pf.add_argument("--target", required=True, help="new output DB; reuse the same path on retry")
            pf.add_argument("--resume", action="store_true", help="resume this exact unfinished target")
            pf.add_argument("--retries", type=int, default=2, help="embedding retries (default 2)")
            _embed_args(pf)
            pf.add_argument("--allow-index-contract-rebase", action="store_true", help=REBASE_HELP)
        pf.set_defaults(func=_cmd_freshness)

    pb = sub.add_parser("bundle", help="portable frozen snapshot bundles, full or one scope;"
                                       " prints the manifest as JSON")
    pbs = pb.add_subparsers(dest="bundle_cmd", required=True, metavar="ACTION")
    pbe = pbs.add_parser("export", help="freeze a bundle from a source DB (the source is only read)")
    pbe.add_argument("--source", required=True, help="source DB")
    pbe.add_argument("--target", required=True, help="new output DB; its .manifest.json is written beside it")
    pbe.add_argument("--scope", default=None, help="export only sources and claims with this scope label")
    pbe.add_argument("--resume", action="store_true", help="restart this exact unfinished export")
    pbv = pbs.add_parser("validate", help="verify a bundle's digests, schema, inventory and scope")
    pbv.add_argument("--db", required=True, help="bundle DB")
    pbv.add_argument("--manifest", default=None, help="manifest (default: beside the DB)")
    pbi = pbs.add_parser("import", help="validate a bundle and restore it into a new local DB")
    pbi.add_argument("--db", required=True, help="bundle DB")
    pbi.add_argument("--target", required=True, help="new local DB")
    pbi.add_argument("--manifest", default=None, help="manifest (default: beside the bundle DB)")
    for parser in (pbe, pbv, pbi):
        parser.set_defaults(func=_cmd_bundle)

    pmc = sub.add_parser("mcp", help="serve read-only evidence from a snapshot over MCP (stdio)")
    pmc.add_argument("--db", default=None,
                     help=f"snapshot DB, or a symlink to one (default $QUOTH_HOME/{EVIDENCE_DB})")
    pmc.set_defaults(func=_cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in DELEGATED:
        from importlib import import_module
        module = import_module(f"{__package__}.{DELEGATED[args[0]][0]}")
        return module.main(args[1:])
    a = build_parser().parse_args(args)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
