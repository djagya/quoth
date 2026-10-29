"""quoth.evals: the one evaluation API (eval set v1, prefetch harness, online readout).

    from quoth.evals import load_set, validate, run_set, Receipt, InProcessTarget

    evalset = load_set("eval/public/set.jsonl")          # validates set + manifest
    target = InProcessTarget.build(db=..., ledger=..., model="hash-ngram")
    receipt, _ = run_set(evalset, target)                 # dry-run prefetch per row
    receipt.write("run.json")                             # no text, ids + metrics only

See docs/EVAL.md for the format, metrics, receipts and the CLI (`quoth eval ...`).
"""
from .compare import DEFAULT_TOLERANCES, compare, load_tolerances
from .convert import FORMATS, convert
from .format import (KINDS, ORIGINS, SCHEMA, STORES, EvalSet, EvalSetError, load_set, validate,
                     write_set)
from .online import online
from .run import InProcessTarget, Receipt, RunError, SocketTarget, run_set

__all__ = ["DEFAULT_TOLERANCES", "FORMATS", "KINDS", "ORIGINS", "SCHEMA", "STORES", "EvalSet",
           "EvalSetError", "InProcessTarget", "Receipt", "RunError", "SocketTarget", "compare",
           "convert", "load_set", "load_tolerances", "online", "run_set", "validate", "write_set"]
