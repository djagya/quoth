"""Loopback control process for the WebUI extension. No env/credential discovery.

Typical install: memory views only, ledger via the sidecar socket:
``python -m quoth.control --token-file … --quoth-socket …
--nightly-receipts … --heartbeat …``. The retrieval lab needs a frozen snapshot and digest.
"""
import argparse
from pathlib import Path
from .api import ControlApp, serve
from .memory import MemoryViews


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m quoth.control",
                                description="Read-only control API for the WebUI memory panel")
    lab = p.add_argument_group("retrieval lab (all three or none; none = memory views only, lab routes 503)")
    lab.add_argument("--snapshot", type=Path, help="existing frozen DB, never a writable live DB")
    lab.add_argument("--snapshot-id")
    lab.add_argument("--sha256", help="expected frozen DB SHA-256")
    p.add_argument("--token-file", type=Path, required=True, help="WebUI sidecar-auth/quoth.token")
    p.add_argument("--model-cache", type=Path, help="local model root (the lab's query embedder)")
    p.add_argument("--receipts", type=Path, help="directory of allowlisted controller receipts (read only)")
    p.add_argument("--lab-state", type=Path, help="separate lab aggregates; never snapshot or vault")
    p.add_argument("--code-commit", help="exact candidate commit for local export provenance")
    p.add_argument("--port", type=int, default=17889)
    memory = p.add_argument_group("read-only memory views (each optional; a missing one shows as unconfigured)")
    memory.add_argument("--ledger", type=Path,
                        help="development only: read ledger.db directly (mode=ro + query_only). Omit in an"
                             " install: with --quoth-socket alone, the sidecar serves the ledger and core views")
    memory.add_argument("--core-dir", type=Path,
                        help="development only, with --ledger: published core directory (core.md)")
    memory.add_argument("--core-budget", type=int, default=4000, help="budget for the live core compile")
    memory.add_argument("--quoth-socket", type=Path,
                        help="Quoth sidecar Unix socket, for recall (prefetch dry_run) and episode search")
    memory.add_argument("--nightly-receipts", type=Path, help="nightly refresh receipts directory; read only")
    memory.add_argument("--heartbeat", type=Path, help="heartbeat.json (default <nightly-receipts>/../heartbeat.json)")
    a = p.parse_args(argv)
    lab_args = (a.snapshot, a.snapshot_id, a.sha256)
    if any(v is not None for v in lab_args) and not all(v is not None for v in lab_args):
        p.error("--snapshot, --snapshot-id and --sha256 go together")
    if a.snapshot is not None and a.model_cache is None:
        p.error("the lab needs --model-cache")
    if not 1024 <= a.port <= 65535 or (a.sha256 is not None and (
            len(a.sha256) != 64 or any(c not in "0123456789abcdef" for c in a.sha256))):
        p.error("invalid port or snapshot digest")
    service = ControlApp(a.snapshot, a.snapshot_id, a.sha256, a.token_file,
                         model_cache=str(a.model_cache) if a.model_cache else None, receipts=a.receipts,
                         lab_state=a.lab_state, code_commit=a.code_commit,
                         memory=MemoryViews(ledger_path=a.ledger, core_dir=a.core_dir,
                                            core_budget=a.core_budget, sidecar_socket=a.quoth_socket,
                                            nightly_receipts=a.nightly_receipts, heartbeat=a.heartbeat))
    if not service.memory_only and not service.ready:
        p.error("snapshot unavailable or invalid; refusing to start")
    serve(service, port=a.port)


if __name__ == "__main__":
    main()
