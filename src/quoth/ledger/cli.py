"""`quoth ledger …`: operator and review access to the mutable core-memory ledger.

Claim records and reports print as JSON. Exit codes: 0 ok, 1 runtime failure or a red
health check, 2 usage error, 3 the ledger refused the action (the reason is printed to
stderr as {"error": code}).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import paths
from . import core
from .authored import StateDbVerifier
from .compile import compile_core, publish_core

EXIT_REFUSED = 3


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


NUDGE = ("Memory review: {n} claims waiting (oldest {age:.0f} d). "
         "Say \"review memory\" when you have ten minutes; I show 10 at a time.")


def nudge_line(health: dict) -> str:
    """The weekly reminder, or "" when nothing is pending. Counts only, never claim text."""
    n = health.get("pending") or 0
    if not n:
        return ""
    return NUDGE.format(n=n, age=health.get("oldest_pending_age_days") or 0)


def _nudge(ledger_path: str) -> int:
    """Read-only (mode=ro + query_only): the cron never creates, migrates or writes the ledger."""
    import sqlite3

    from ..control.memory import connect_ro
    path = Path(ledger_path)
    if not path.is_file():
        print(json.dumps({"error": "ledger_unavailable"}), file=sys.stderr)
        return 1
    try:
        conn = connect_ro(path)
        try:
            line = nudge_line(core.health(conn))
        finally:
            conn.close()
    except sqlite3.Error:
        print(json.dumps({"error": "ledger_unavailable"}), file=sys.stderr)
        return 1
    if line:
        print(line)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="quoth ledger", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ledger", type=Path, default=None,
                   help="ledger file (default $QUOTH_HOME/ledger.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("propose", help="record a pending claim (write-time reconcile)")
    pp.add_argument("--text", required=True)
    pp.add_argument("--kind", required=True, choices=core.KINDS)
    pp.add_argument("--origin", required=True, choices=core.ORIGINS)
    pp.add_argument("--proposer", default="operator")
    pp.add_argument("--reason")
    pp.add_argument("--quote")
    pp.add_argument("--locator", help="hermes:<session_id>:<message_id> or a vault path")
    pp.add_argument("--confidence", type=float)
    pp.add_argument("--subject-key")
    pp.add_argument("--supersedes", type=int)
    pp.add_argument("--state-db", type=Path, default=None,
                    help="Hermes state.db that verifies an owner_statement quote"
                         " (default $HERMES_HOME/state.db)")

    for name, text in (("accept", "accept a pending claim"), ("reject", "reject a pending claim")):
        pr = sub.add_parser(name, help=text)
        pr.add_argument("id", type=int)
        pr.add_argument("--note")
    pe = sub.add_parser("edit", help="replace a claim with the owner's wording (accepted)")
    pe.add_argument("id", type=int)
    pe.add_argument("--text")
    pe.add_argument("--kind", choices=core.KINDS)
    pe.add_argument("--note")
    pf = sub.add_parser("forget", help="end an accepted claim's validity (it stays in history)")
    pf.add_argument("id", type=int)
    pf.add_argument("--reason", required=True)
    pu = sub.add_parser("undo-auto", help="return an auto-accepted claim to pending review")
    pu.add_argument("id", type=int)
    pq = sub.add_parser("queue", help="oldest pending claims for review")
    pq.add_argument("--limit", type=int, default=20)
    px = sub.add_parser("expire", help="expire pending claims older than N days")
    px.add_argument("--days", type=float, default=30)
    px.add_argument("--dry-run", action="store_true", help="list what would expire, change nothing")
    ph = sub.add_parser("health", help="queue size/age and churn; exit 1 when red")
    ph.add_argument("--max-pending", type=int, default=40)
    ph.add_argument("--max-age-days", type=float, default=14)
    ps = sub.add_parser("seed", help="import JSONL {text, kind?, reason?} as pending imported claims")
    ps.add_argument("--file", required=True, type=Path)
    sub.add_parser("nudge", help="print the weekly review reminder, or nothing when the queue is empty"
                   " (read-only; for a script-only cron)")
    pc = sub.add_parser("compile-core", help="print the budgeted core block")
    pc.add_argument("--budget", type=int, default=4000)
    pc.add_argument("--headroom", type=float, default=0.2)
    pc.add_argument("--publish", type=Path, help="write core-<sha>.md and repoint core.md here")
    a = p.parse_args(argv)
    ledger_path = a.ledger or paths.state_file("ledger.db")
    if a.cmd == "nudge":
        return _nudge(ledger_path)

    conn = core.open_ledger(ledger_path)
    try:
        if a.cmd == "propose":
            verifier = (StateDbVerifier(a.state_db or paths.hermes_state_db())
                        if a.origin == "owner_statement" else None)
            _print(core.propose(conn, text=a.text, kind=a.kind, origin=a.origin, proposer=a.proposer,
                                reason=a.reason, evidence_quote=a.quote, evidence_locator=a.locator,
                                confidence=a.confidence, subject_key=a.subject_key,
                                supersedes=a.supersedes, verifier=verifier))
        elif a.cmd == "accept":
            _print(core.accept(conn, a.id, note=a.note))
        elif a.cmd == "reject":
            _print(core.reject(conn, a.id, note=a.note))
        elif a.cmd == "edit":
            _print(core.edit(conn, a.id, text=a.text, kind=a.kind, note=a.note))
        elif a.cmd == "forget":
            _print(core.forget(conn, a.id, a.reason))
        elif a.cmd == "undo-auto":
            _print(core.undo_auto_accept(conn, a.id))
        elif a.cmd == "queue":
            _print(core.review_queue(conn, a.limit))
        elif a.cmd == "expire":
            if a.dry_run:
                _print({"would_expire": core.expirable(conn, a.days)})
            else:
                _print({"expired": core.expire_pending(conn, a.days)})
        elif a.cmd == "health":
            result = core.health(conn, max_pending=a.max_pending, max_pending_age_days=a.max_age_days)
            _print(result)
            return 0 if result["status"] == "green" else 1
        elif a.cmd == "seed":
            rows = [json.loads(line) for line in a.file.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
            _print(core.seed(conn, rows))
        elif a.cmd == "compile-core":
            if a.publish:
                _print(publish_core(conn, a.publish, a.budget, a.headroom))
            else:
                sys.stdout.write(compile_core(conn, a.budget, a.headroom)["text"])
    except core.LedgerRejected as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return EXIT_REFUSED
    finally:
        conn.close()
    return 0
