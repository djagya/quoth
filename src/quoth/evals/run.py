"""`eval run`: score an eval set against the real prefetch path, dry-run (nothing marked used).

Default is in-process: a `sidecar.Quoth` built from snapshot, ledger, episode lanes and
state.db paths, exactly as the sidecar builds it. `SocketTarget` asks a live sidecar instead.
Each row is one ``POST /v1/prefetch`` with ``dry_run`` and ``k_candidates``; the reply's
``detail.candidates`` (per store, before injection) is matched against the row's sources.

The receipt holds hashes, config and aggregate metrics, plus row ids for lost-gold
comparison and, per row, each candidate's scores and match flags (``candidates``, the
input of `eval sweep`); never question text, paths or excerpts (checked before it is
returned).
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from .. import __version__
from . import metrics as M
from . import sweep as SW
from .format import EvalSet, vault_paths

RECEIPT_SCHEMA = "quoth-eval-run/v1"
DEFAULT_K_CANDIDATES = 10
WARMUP_QUERY = "warmup"


class RunError(RuntimeError):
    pass


@dataclass
class Receipt:
    """Content-free result of one run; `data` is the JSON document."""

    data: dict

    @property
    def status(self) -> str:
        return self.data.get("status", "ok")

    def to_json(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def write(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Receipt":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != RECEIPT_SCHEMA:
            raise ValueError("not an eval run receipt")
        return cls(data)


# ---------------------------------------------------------------- targets

class InProcessTarget:
    """A `sidecar.Quoth` built here; closed by `close()`."""

    def __init__(self, app, *, owns: bool = True):
        self.app = app
        self.owns = owns
        self.mode = "in_process"

    @classmethod
    def build(cls, *, db=None, ledger=None, state_db=None, episodes=None, owner_episodes=None,
              model: str = "bge-m3", evidence_budget: float | None = None,
              episodes_prefetch: bool = False, owner_prefetch: bool = True,
              assistant_min_score: float | None = None, owner_min_score: float | None = None,
              core_dir=None, embedder=None, gate=None, reranker=None) -> "InProcessTarget":
        """`embedder` (optional) is an already loaded model for `model`, shared instead of
        loading another copy (the nightly gate scores two snapshots with one bge-m3);
        `gate` a `relevance.Gate` (default `relevance.default_gate()`, as the sidecar serves;
        `relevance.Gate()` for none), as the sidecar's gate flags set it;
        `reranker` a `rerank.RerankService` (default none), as `--rerank-model` sets it."""
        from .. import sidecar
        if ledger is None:
            raise RunError("ledger path required")
        evidence = None
        if db:
            from ..evidence_service import ReadOnlyEvidence
            from ..serving import ServingSnapshot
            evidence = ReadOnlyEvidence(ServingSnapshot(str(db)))
            if embedder is not None:
                evidence.queries.seed(model, embedder)
        assistant = sidecar._open_episodes(episodes, model, evidence) if episodes else None
        owner = sidecar._open_episodes(owner_episodes, model, evidence, role="owner") if owner_episodes else None
        kwargs = {}
        if evidence_budget is not None:
            kwargs["evidence_budget"] = evidence_budget
        if assistant_min_score is not None:
            kwargs["episode_min_score"] = assistant_min_score
        if owner_min_score is not None:
            kwargs["owner_min_score"] = owner_min_score
        app = sidecar.Quoth(ledger_path=ledger, state_db=state_db or None, evidence=evidence,
                                core_dir=core_dir, model=model, episodes=assistant,
                                episodes_prefetch=episodes_prefetch, owner_episodes=owner,
                                owner_prefetch=owner_prefetch, gate=gate, reranker=reranker, **kwargs)
        target = cls(app)
        target.db = db
        return target

    def prepare(self) -> None:
        """Embed every accepted claim (the sidecar's background sync, run to completion) and
        load the reranker, if any: a scoring run must not fall back to cosine unnoticed."""
        while self.app.sync_claims() > 0:
            pass
        reranker = self.app.reranker
        if reranker is not None and reranker.wait() != "ready":
            raise RunError(f"reranker {reranker.name} {reranker.state}; fetch it with `quoth models fetch --no-embedder --reranker {reranker.name}`")

    def prefetch(self, body: dict) -> dict:
        out = self.app.prefetch(body)
        self.app.wait_idle()
        return out

    def config(self) -> dict:
        app = self.app
        return {"mode": self.mode, "model": app.model, "evidence_budget": app.evidence_budget,
                "stores": {"vault": app.evidence is not None, "claims": True,
                           "owner": app.owner_lane is not None, "assistant": app.assistant_lane is not None},
                "prefetch": {"owner": bool(app.owner_lane and app.owner_lane.prefetch),
                             "assistant": bool(app.assistant_lane and app.assistant_lane.prefetch)},
                "min_score": {"owner": app.owner_lane.min_score if app.owner_lane else None,
                              "assistant": app.assistant_lane.min_score if app.assistant_lane else None},
                "gate": app.gate.to_dict(), **({"rerank": app.reranker.config()} if app.reranker else {})}

    def close(self) -> None:
        if self.owns:
            self.app.close()
            snapshot = getattr(self.app.evidence, "snapshot", None)
            if snapshot is not None:
                snapshot.close()  # also drops the per-connection matrix and date caches


class SocketTarget:
    """A live sidecar over its Unix socket (read-only: dry-run prefetch only)."""

    def __init__(self, path: str, timeout: float = 30.0, evidence_budget: float | None = 30.0):
        self.path = path
        # Wait for the vault like an operator would: the live 1.5 s budget is a serving
        # policy, not retrieval quality (statuses still report what was slow).
        self.evidence_budget = evidence_budget
        self.timeout = max(timeout, (evidence_budget or 0) + 15.0)
        self.mode = "socket"
        self.db = None

    def _post(self, route: str, body: dict | None, *, allow_unhealthy: bool = False) -> dict:
        import http.client
        import socket

        class _Conn(http.client.HTTPConnection):
            def connect(conn):  # noqa: N805
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(self.timeout)
                sock.connect(self.path)
                conn.sock = sock

        conn = _Conn("localhost", timeout=self.timeout)
        try:
            if body is None:
                conn.request("GET", route)
            else:
                conn.request("POST", route, body=json.dumps(body).encode("utf-8"),
                             headers={"Content-Type": "application/json"})
            reply = json.loads(conn.getresponse().read())
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise RunError(f"sidecar unreachable: {type(exc).__name__}") from exc
        finally:
            conn.close()
        # A sidecar that cannot open its snapshot answers /v1/health with 503 and its full body.
        if not reply.get("ok") and not (allow_unhealthy and reply.get("schema") == "quoth-sidecar/v1"):
            raise RunError(f"sidecar refused: {reply.get('error')}")
        return reply

    def prepare(self) -> None:
        self._post("/v1/health", None, allow_unhealthy=True)

    def prefetch(self, body: dict) -> dict:
        if self.evidence_budget is not None:
            # A reranking sidecar scores within the same wait (dry runs only).
            body = {**body, "evidence_budget": self.evidence_budget, "rerank_budget": self.evidence_budget}
        out = self._post("/v1/prefetch", body)
        if "candidates" not in (out.get("detail") or {}):
            raise RunError("sidecar too old: dry-run detail has no candidates (needs quoth >= 0.10)")
        return out

    def config(self) -> dict:
        health = self._post("/v1/health", None, allow_unhealthy=True)
        return {"mode": self.mode, "sidecar_version": health.get("version"),
                "stores": {"vault": health.get("evidence", {}).get("status") == "ok", "claims": True,
                           "owner": health.get("owner_episodes", {}).get("status") not in (None, "disabled"),
                           "assistant": health.get("episodes", {}).get("status") not in (None, "disabled")},
                "gate": health.get("relevance_gate"),
                **({"rerank": health["reranker"]} if health.get("reranker") else {})}

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- gold drift

def snapshot_drift(evalset: EvalSet, db) -> tuple[dict, dict]:
    """Resolve expected vault paths against the snapshot once (nightly gate rules).

    Returns ({row id: resolved paths | None for stale rows}, report). A path missing from
    the current snapshot but whose content identity maps to exactly one current path is
    renamed; otherwise the row is stale and excluded from vault scoring. Raises
    gold_drift.ExcessiveDrift past the gate's bound.
    """
    from ..db import connect_read_only
    from ..gold_drift import resolve, snapshot_identities

    target = Path(db).resolve()
    old = snapshot_identities(target)
    with closing(connect_read_only(target)) as conn:
        current = dict(conn.execute(
            "SELECT s.path, v.content_hash FROM sources s JOIN source_versions v ON v.source_id = s.id"
            " AND v.is_current = 1"))
    gold = [{"id": r["id"], "expected_paths": vault_paths(r)} for r in evalset.rows if vault_paths(r)]
    anchored, _aliases, report = resolve({"set": gold}, old, current)
    kept = {r["id"]: r["expected_paths"] for r in anchored["set"]}
    return {r["id"]: kept.get(r["id"]) for r in gold}, report


def _resolved_sources(row: dict, resolved: dict | None) -> list | None:
    """The row's sources with drifted vault paths rewritten; None when the row is stale."""
    if resolved is None or row["id"] not in resolved:
        return row["expect"]["sources"]
    paths = resolved[row["id"]]
    if paths is None:
        return None
    mapping = dict(zip(vault_paths(row), paths))
    return [{"store": s["store"], "match": {"path": mapping.get(s["match"]["path"], s["match"]["path"])}}
            if s["store"] == "vault" and "path" in s["match"] else s for s in row["expect"]["sources"]]


# ---------------------------------------------------------------- run

def _guard_no_text(receipt: dict, evalset: EvalSet) -> None:
    """Fail closed if any question, path or hint of the set appears in the receipt."""
    blob = json.dumps(receipt, ensure_ascii=False)
    for row in evalset.rows:
        needles = [row["question"], *vault_paths(row), row["expect"].get("answer_hint") or ""]
        needles += [t["text"] for t in row.get("context", [])]
        for needle in needles:
            if needle and len(needle) >= 6 and needle in blob:
                raise RunError(f"{row['id']}: receipt would contain row content; refusing")


def run_set(evalset: EvalSet, target, *, k_candidates: int = DEFAULT_K_CANDIDATES,
            gold_drift: bool = False, detail: bool = False, warmup: bool = True) -> tuple[Receipt, list | None]:
    """Run every row through `target.prefetch` (dry run). Returns (receipt, per-row detail|None)."""
    target.prepare()
    if warmup:
        target.prefetch({"query": WARMUP_QUERY, "dry_run": True, "k_candidates": k_candidates})
    resolved, drift = None, None
    status = "ok"
    if gold_drift:
        if getattr(target, "db", None) is None:
            raise RunError("gold drift needs --db (in-process)")
        from ..gold_drift import ExcessiveDrift, report_only
        try:
            resolved, report = snapshot_drift(evalset, target.db)
            drift = report_only(report)
        except ExcessiveDrift as exc:
            drift, status = report_only(exc.report), "gold_drift_excessive"
    observations, timings, statuses, rows_detail, recorded = [], [], {}, [], {}
    if status == "ok":
        for row in evalset.rows:
            sources = _resolved_sources(row, resolved)
            t0 = time.monotonic()
            # A reranking sidecar scores every candidate (`k_rerank`) so a sweep can widen.
            out = target.prefetch({"query": row["question"], "dry_run": True, "k_candidates": k_candidates,
                                   "k_rerank": k_candidates})
            wall = round((time.monotonic() - t0) * 1000, 2)
            d = out.get("detail") or {}
            candidates = d.get("candidates") or {}
            stage = dict(d.get("timings_ms") or {})
            stage["wall"] = wall
            timings.append(stage)
            for store, st in (d.get("status") or {}).items():
                statuses.setdefault(store, {}).setdefault(st, 0)
                statuses[store][st] += 1
            if sources is None:  # stale gold: drop vault expectations, keep the rest
                sources = [s for s in row["expect"]["sources"] if s["store"] != "vault"]
                if not sources and not row["expect"]["abstain"]:
                    continue
            obs = M.observe(row, candidates, sources=sources)
            observations.append(obs)
            recorded[row["id"]] = SW.record(row, candidates, sources, timings=stage)
            if detail:
                rows_detail.append({"id": row["id"], "kind": row["kind"], "origin": row["origin"],
                                    "lang": row["lang"], "observation": obs, "timings_ms": stage,
                                    "candidates": {s: [{k: v for k, v in it.items() if k != "text"}
                                                       for it in items] for s, items in candidates.items()}})
    data = {
        "schema": RECEIPT_SCHEMA,
        "created_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "code": {"package": "quoth", "version": __version__},
        "set": {"schema": evalset.manifest.get("schema"), "sha256": evalset.sha256,
                "rows": len(evalset.rows), "privacy": evalset.manifest.get("privacy"),
                "fixture": evalset.manifest.get("fixture")},
        "config": {**target.config(), "k_candidates": k_candidates, "gold_drift": gold_drift,
                   "context": "ignored", "asked_at": "ignored"},
        "status": status,
        "gold_drift": drift,
        "scored": len(observations),
        "metrics": M.aggregate(observations) if observations else None,
        "latency_ms": M.latency(timings),
        "statuses": statuses,
        "items": M.items(observations),
        "candidates": {"schema": SW.CANDIDATES_SCHEMA, "rows": recorded},
    }
    _guard_no_text(data, evalset)
    return Receipt(data), (rows_detail if detail else None)
