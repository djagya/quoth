"""`quoth eval run|validate|convert|compare|sweep|online|fixture`.

Exit codes: 0 ok/pass, 1 regression (compare) or invalid set (validate), 2 unusable input
or usage error, 3 run not comparable / gold drift excessive.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .. import recall_defaults as D
from ..assets import eval_path
from .format import EvalSetError

PRIVATE_WARNING = ("warning: --detail-out holds per-row ids, locators and vault paths of this set; "
                   "it is private data: keep it on this machine, never commit or share it")
TRACE_WARNING = ("warning: --trace-out holds every question, recall block and excerpt of this run; "
                 "it is private data: keep it on this machine, never commit or share it")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _err(msg: str) -> None:
    print(f"quoth eval: {msg}", file=sys.stderr)


def _cmd_validate(a) -> int:
    from .format import validate
    errors = validate(a.set)
    _print({"valid": not errors, "errors": errors})
    return 1 if errors else 0


def _cmd_convert(a) -> int:
    from .convert import ConvertError, convert
    try:
        summary = convert(a.fmt, a.input, a.output, privacy=a.privacy, fixture=a.fixture,
                          origin=a.origin, seed_out=a.seed_out)
    except (ConvertError, EvalSetError, OSError) as exc:
        _err(str(exc))
        return 2
    _print(summary)
    return 0


def _target(a):
    from .. import relevance, rerank
    from .run import InProcessTarget, RunError, SocketTarget
    gate = relevance.from_args(a)
    reranker = rerank.from_args(a, gate)
    if a.socket:
        if gate != relevance.default_gate() or reranker is not None or a.retrieval != D.RETRIEVAL or a.vault:
            raise RunError("gate, rerank, retrieval and vault flags apply in-process only; a live sidecar uses its own"
                           " (see /v1/health)")
        target = SocketTarget(a.socket, evidence_budget=30.0 if a.evidence_budget is None else a.evidence_budget)
        target.db = a.db  # read-only, only for --gold-drift against the served snapshot
        return target
    if a.fixture_dir:
        d = Path(a.fixture_dir)
        defaults = {"db": d / "snapshot.db", "ledger": d / "ledger.db", "state_db": d / "state.db",
                    "episodes": d / "episodes.db", "owner_episodes": d / "owner-episodes.db"}
        for key, path in defaults.items():
            if getattr(a, key) is None and path.exists():
                setattr(a, key, str(path))
        recorded = d / "fixture.json"
        if a.model is None and recorded.is_file():
            a.model = json.loads(recorded.read_text(encoding="utf-8")).get("model")
    a.model = a.model or "bge-m3"
    return InProcessTarget.build(
        db=a.db, ledger=a.ledger, state_db=a.state_db, episodes=a.episodes,
        owner_episodes=a.owner_episodes, model=a.model, evidence_budget=a.evidence_budget,
        episodes_prefetch=a.episodes_prefetch == "on", owner_prefetch=a.owner_prefetch == "on",
        assistant_min_score=a.assistant_min_score, owner_min_score=a.owner_min_score, gate=gate,
        reranker=reranker, retrieval=a.retrieval, vault=a.vault)


def _cmd_run(a) -> int:
    from .format import load_set
    from .run import RunError, run_set
    try:
        evalset = load_set(a.set)
    except EvalSetError as exc:
        _err(f"invalid set: {exc}")
        return 2
    try:
        target = _target(a)
    except (RunError, OSError, ValueError, RuntimeError) as exc:
        _err(f"cannot build target: {type(exc).__name__}: {exc}")
        return 2
    trace = [] if a.trace_out else None
    try:
        receipt, detail = run_set(evalset, target, k_candidates=a.k_candidates,
                                  gold_drift=a.gold_drift, detail=bool(a.detail_out), trace=trace)
    except RunError as exc:
        _err(str(exc))
        return 2
    finally:
        target.close()
    if a.out:
        receipt.write(a.out)
    if a.detail_out:
        print(PRIVATE_WARNING, file=sys.stderr)
        out = Path(a.detail_out)
        out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in detail), encoding="utf-8")
        out.chmod(0o600)
    if a.trace_out:
        print(TRACE_WARNING, file=sys.stderr)
        out = Path(a.trace_out)
        out.touch(mode=0o600)
        out.chmod(0o600)
        out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in trace), encoding="utf-8")
    if not a.quiet:
        print(receipt.to_json(), end="")
    return 3 if receipt.status != "ok" else 0


def _cmd_compare(a) -> int:
    from .compare import compare, load_tolerances
    from .run import Receipt
    try:
        tolerances = load_tolerances(a.tolerances)
        result = compare(Receipt.load(a.a), Receipt.load(a.b), tolerances)
    except (OSError, ValueError) as exc:
        _err(f"unusable input: {exc}")
        return 2
    _print(result)
    return result["exit"]


def _parse_grid(raw: str | None):
    """Comma-separated thresholds ("none" for off), or None for the default grid."""
    if raw is None:
        return None
    return [None if v.strip().lower() == "none" else float(v) for v in raw.split(",") if v.strip()]


def _cmd_sweep(a) -> int:
    from .run import Receipt
    from .sweep import (SweepError, VAULT_GAPS, VAULT_RESCUES, CLAIMS_LEXICALS, RERANK_FLOORS, RERANK_GAPS,
                        RERANK_WIDENS, sweep, table)
    try:
        result = sweep(Receipt.load(a.receipt).data, max_loss=a.max_loss,
                       vault_cosines=_parse_grid(a.vault_cosines),
                       vault_gaps=_parse_grid(a.vault_gaps) or VAULT_GAPS,
                       vault_rescues=_parse_grid(a.vault_rescues) or VAULT_RESCUES,
                       claims_cosines=_parse_grid(a.claims_cosines),
                       claims_lexicals=_parse_grid(a.claims_lexicals) or CLAIMS_LEXICALS,
                       vault_reranks=[v for v in _parse_grid(a.vault_reranks) or RERANK_FLOORS if v is not None],
                       vault_rerank_gaps=_parse_grid(a.vault_rerank_gaps) or RERANK_GAPS,
                       vault_rerank_widens=_parse_grid(a.vault_rerank_widens) or RERANK_WIDENS,
                       claims_reranks=[v for v in _parse_grid(a.claims_reranks) or RERANK_FLOORS if v is not None],
                       rerank_budget=a.rerank_budget, evidence_budget=a.evidence_budget)
    except (OSError, SweepError, ValueError) as exc:
        _err(f"unusable input: {exc}")
        return 2
    if a.out:
        Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
    if a.format == "json":
        _print(result)
    else:
        print(table(result, top=a.top), end="")
    return 0


def _cmd_online(a) -> int:
    from .online import online
    if not Path(a.log).is_dir():
        _err("log directory not found")
        return 2
    _print(online(a.log, a.days))
    return 0


def _cmd_fixture(a) -> int:
    from .fixture import build_fixture
    try:
        _print(build_fixture(a.source, a.out, model=a.model))
    except (OSError, ValueError, RuntimeError) as exc:
        _err(f"fixture build failed: {exc}")
        return 2
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="quoth eval", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("validate", help="check a set against eval set v1 and its manifest")
    pv.add_argument("set")
    pv.set_defaults(func=_cmd_validate)

    pc = sub.add_parser("convert", help="convert golden-v1 / golden-v3 / ledger-updates to eval set v1")
    pc.add_argument("--from", dest="fmt", required=True, choices=("golden-v1", "golden-v3", "ledger-updates"))
    pc.add_argument("input")
    pc.add_argument("output")
    pc.add_argument("--privacy", choices=("public", "private"), default="private")
    pc.add_argument("--fixture", default="private", help="the fixture (store set) the rows target")
    pc.add_argument("--origin", choices=("real_user", "assistant_query", "authored", "synthetic"),
                    help="origin for formats that do not record one (default authored; synthetic for ledger-updates)")
    pc.add_argument("--seed-out", help="ledger-updates: where to write the ledger seed")
    pc.set_defaults(func=_cmd_convert)

    pr = sub.add_parser("run", help="score a set against the real prefetch path (dry run)")
    pr.add_argument("set")
    pr.add_argument("--socket", help="live sidecar socket instead of an in-process build")
    pr.add_argument("--fixture-dir", help="directory built by `eval fixture` (fills the store paths)")
    pr.add_argument("--db", help="serving snapshot or pointer (omit: no vault store)")
    pr.add_argument("--ledger")
    pr.add_argument("--state-db")
    pr.add_argument("--episodes", help="assistant lane DB (what the assistant said)")
    pr.add_argument("--owner-episodes", help="owner lane DB (what the owner wrote)")
    pr.add_argument("--model", default=None,
                    help="embedding model (default: the fixture's recorded model, else bge-m3)")
    pr.add_argument("--evidence-budget", type=float, default=None,
                    help=f"seconds prefetch waits for evidence (in-process default: the sidecar's"
                         f" {D.EVIDENCE_BUDGET:g}; --socket default: 30)")
    pr.add_argument("--episodes-prefetch", choices=("on", "off"), default="off")
    pr.add_argument("--owner-prefetch", choices=("on", "off"), default="on")
    pr.add_argument("--assistant-min-score", type=float)
    pr.add_argument("--owner-min-score", type=float)
    pr.add_argument("--k-candidates", type=int, default=10, help="candidates per store (max 20)")
    pr.add_argument("--gold-drift", action="store_true",
                    help="resolve expected vault paths against the snapshot (nightly gate rules)")
    pr.add_argument("--out", help="write the receipt here")
    pr.add_argument("--detail-out", help="PRIVATE per-row ids/locators/scores (local only)")
    pr.add_argument("--trace-out", help="PRIVATE JSONL per row: query, exact recall block, explicit hits"
                                        " with text (caller tests; local only)")
    pr.add_argument("--retrieval", choices=D.RETRIEVAL_MODES, default=D.RETRIEVAL,
                    help=f"vault ranking, as the sidecar's --retrieval (default {D.RETRIEVAL})")
    pr.add_argument("--vault", help="live vault root: per-hit freshness (live) in explicit hits, as the"
                                    " sidecar's --vault")
    pr.add_argument("--quiet", action="store_true", help="do not print the receipt")
    from .. import relevance, rerank
    relevance.add_arguments(pr)
    rerank.add_arguments(pr)
    pr.set_defaults(func=_cmd_run)

    pm = sub.add_parser("compare", help="gate-style diff of two receipts (exit 0 pass, 1 fail, 3 incomparable)")
    pm.add_argument("a")
    pm.add_argument("b")
    pm.add_argument("--tolerances", help="tolerances JSON (default: built-in, see docs/EVAL.md)")
    pm.set_defaults(func=_cmd_compare)

    ps = sub.add_parser("sweep", help="relevance-gate thresholds replayed on a run receipt's candidates")
    ps.add_argument("receipt", help="an `eval run` receipt (quoth >= 0.12.5)")
    ps.add_argument("--max-loss", type=float, default=0.01,
                    help="largest allowed drop of vault/claims hit@5, recall@10 and injected hit rate")
    ps.add_argument("--vault-cosines", help="comma list of vault floors (default: 0.01 steps over the recorded scores)")
    ps.add_argument("--vault-gaps", help="comma list of max gaps to the top hit ('none' = off)")
    ps.add_argument("--vault-rescues", help="comma list of lexical rescue shares ('none' = off)")
    ps.add_argument("--claims-cosines", help="comma list of claim cosine floors (default: 0.01 steps)")
    ps.add_argument("--claims-lexicals", help="comma list of word-match shares for claims ('none' = off)")
    ps.add_argument("--vault-reranks", help="comma list of vault rerank floors (receipts with rerank scores;"
                                            " default a 0.001..0.9 grid)")
    ps.add_argument("--vault-rerank-gaps", help="comma list of max rerank gaps to the turn's best ('none' = off)")
    ps.add_argument("--vault-rerank-widens", help="comma list of rerank widens, hits ('none' = off; at most"
                                                  " the run's --k-candidates)")
    ps.add_argument("--claims-reranks", help="comma list of claim rerank floors")
    ps.add_argument("--rerank-budget", type=float, default=D.RERANK_BUDGET,
                    help="the live sidecar's --rerank-budget (s): rerank configs replay it on the recorded"
                         " per-pair scoring times; one whose p95 exceeds it is not eligible")
    ps.add_argument("--evidence-budget", type=float, default=D.EVIDENCE_BUDGET,
                    help="the live sidecar's --evidence-budget (s): scoring also ends at this deadline,"
                         " after the row's recorded evidence search")
    ps.add_argument("--format", choices=("table", "json"), default="table")
    ps.add_argument("--top", type=int, default=10, help="eligible configs shown in the table")
    ps.add_argument("--out", help="write the full result (every config) as JSON here")
    ps.set_defaults(func=_cmd_sweep)

    po = sub.add_parser("online", help="aggregate readout of the sidecar's --prefetch-log")
    po.add_argument("--log", required=True)
    po.add_argument("--days", type=int, default=None)
    po.set_defaults(func=_cmd_online)

    pf = sub.add_parser("fixture", help="build a fixture's stores (snapshot, ledger, state.db, lanes)")
    pf.add_argument("source", nargs="?", default=str(eval_path("public")))
    pf.add_argument("--out", required=True)
    pf.add_argument("--model", default="hash-ngram")
    pf.set_defaults(func=_cmd_fixture)

    a = p.parse_args(argv)
    if a.cmd == "run" and not a.socket and not a.ledger and not a.fixture_dir:
        p.error("run needs --socket, --fixture-dir or --ledger")
    if a.cmd == "run" and not 1 <= a.k_candidates <= 20:
        p.error("--k-candidates must be 1..20")
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
