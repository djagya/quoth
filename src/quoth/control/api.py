"""Token-v1 loopback API; trusted browser-origin checks belong to WebUI proxy.

No forwarded identity is consumed. Only a fixed route table is dispatched. Every
private response is no-store, including errors. No request body is logged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from http import HTTPStatus
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from ..db import connect_read_only, validate_evidence_db
from ..pipeline import catalogue, production, run, spec_hash, validate
from ..embed.backends import MODELS, EmbedderUnavailable, get_embedder
from .memory import MemoryViewError, MemoryViews
from .observations import NAMES, Receipts
from .lab import SUITES, ASSETS, GATE_NAMES, evaluate, persist, _digest

PREFIX = "/api/memory/v1"
RUN_PATH = re.compile(r"^/lab/runs/([0-9a-f]{32})(?:/(reveal))?$")
EVAL_PATH = re.compile(r"^/lab/evals/([0-9a-f]{32})$")
CLAIM_PATH = re.compile(r"^/memory/claims/([1-9][0-9]{0,11})$")
CORE_VERSION_PATH = re.compile(r"^/memory/core/versions/([1-9][0-9]{0,11})$")
MEMORY_FILTERS = frozenset(("status", "kind", "authority", "q", "offset", "limit"))
MAX_BODY = 65536
MAX_HTTP_REQUESTS = 8


class APIError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


class ControlApp:
    """Inject immutable snapshot identity; never compute an identity from a filename."""

    def __init__(self, db_path: Path | None, snapshot_id: str | None, digest: str | None, token_file: Path,
                 *, embedder=None, model="bge-m3", model_cache=None, receipts=None,
                 lab_state: Path | None = None, code_commit: str | None = None, clock=time.monotonic,
                 memory: MemoryViews | None = None):
        # Memory-only (no lab snapshot): the memory views run, every lab route is 503.
        self.memory_only = db_path is None
        self.db_path = Path(db_path) if db_path is not None else None
        self.snapshot_id, self.digest = snapshot_id, digest
        self.token_file = Path(token_file)
        self.embedder, self.clock = embedder, clock
        self.model_cache = model_cache
        self.lab_state = Path(lab_state) if lab_state else None
        self.code_commit = code_commit
        self.evals = OrderedDict()
        self.eval_files = {}
        self.eval_slot = threading.BoundedSemaphore(1)
        self.eval_admissions = []
        self.receipts = Receipts(receipts)
        # Read-only memory views (ledger, core, recall, episodes, refresh history); each
        # source is optional and reports itself unconfigured instead of guessing.
        self.memory = memory or MemoryViews()
        if model not in MODELS:
            raise ValueError("unknown model")
        self.spec = None if self.memory_only else production(snapshot_id, digest, model=model)
        self.runs = OrderedDict()
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(2)
        self.ready = False
        self._snapshot_stat = None
        if self.memory_only:
            return
        try:
            admitted_stat = snapshot_stat(self.db_path)
            if file_digest(self.db_path) == digest and snapshot_stat(self.db_path) == admitted_stat:
                with connect_read_only(self.db_path) as conn:
                    validate_evidence_db(conn)
                    vector = conn.execute(
                        "SELECT e.dim, length(e.vector) FROM embeddings e JOIN chunks c ON c.id=e.chunk_id "
                        "JOIN source_versions v ON v.id=c.source_version_id AND v.is_current=1 "
                        "WHERE e.model=? LIMIT 1", (model,)).fetchone()
                    if vector is None or tuple(vector) != (MODELS[model].dim, 4 * MODELS[model].dim):
                        raise RuntimeError("model_vectors_unavailable")
                    invalid = conn.execute(
                        "SELECT count(*) FROM embeddings e JOIN chunks c ON c.id=e.chunk_id "
                        "JOIN source_versions v ON v.id=c.source_version_id AND v.is_current=1 "
                        "WHERE e.model=? AND (e.dim != ? OR length(e.vector) != ?)",
                        (model, MODELS[model].dim, 4 * MODELS[model].dim)).fetchone()[0]
                    if invalid:
                        raise RuntimeError("invalid_vectors")
                    excluded = conn.execute(
                        "SELECT count(*) FROM sources s JOIN source_versions v ON v.source_id=s.id "
                        "AND v.is_current=1 WHERE s.path LIKE 'ASSISTANT/%'").fetchone()[0]
                    if excluded:
                        raise RuntimeError("excluded_current_source")
                if snapshot_stat(self.db_path) != admitted_stat:
                    raise RuntimeError("snapshot_changed_during_admission")
                self._snapshot_stat = admitted_stat
                self.ready = True
        except (OSError, RuntimeError, sqlite3.DatabaseError):
            pass

    def _read_token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            raise APIError(503, "token_unavailable") from None
        if not 1 <= len(token) <= 512 or any(character.isspace() or not character.isprintable()
                                                  for character in token):
            raise APIError(503, "token_unavailable")
        return token

    def _token(self, supplied: str | None) -> None:
        token = self._read_token()
        if not supplied or not hmac.compare_digest(token, supplied):
            raise APIError(401, "unauthorized")

    def _snapshot_available(self) -> bool:
        """Cheaply bind every admission to the fully verified startup inode."""
        if not self.ready or self._snapshot_stat is None:
            return False
        try:
            current = snapshot_stat(self.db_path)
        except OSError:
            self.ready = False
            return False
        if current != self._snapshot_stat:
            self.ready = False
        return self.ready

    def _require_snapshot(self) -> None:
        if not self._snapshot_available():
            raise APIError(503, "snapshot_unavailable")

    def _body(self, env: dict, allowed: set[str], optional: frozenset[str] | set[str] = frozenset()) -> dict:
        if env.get("CONTENT_TYPE", "").split(";")[0].strip() != "application/json":
            raise APIError(400, "invalid_content_type")
        try:
            length = int(env.get("CONTENT_LENGTH") or "0")
        except ValueError:
            raise APIError(400, "invalid_length") from None
        if length > MAX_BODY:
            raise APIError(413, "too_large")
        if length <= 0:
            raise APIError(400, "invalid_body")
        try:
            body = json.loads(env["wsgi.input"].read(length))
        except (ValueError, UnicodeError):
            raise APIError(400, "invalid_body") from None
        if not isinstance(body, dict) or not allowed <= set(body) or set(body) - allowed - optional:
            raise APIError(400, "invalid_fields")
        return body

    def _prune(self):
        now = self.clock()
        for key, record in list(self.runs.items()):
            if record["deadline"] <= now:
                del self.runs[key]

    def _save(self, trace: dict, bundle: dict, private: dict, query: str) -> dict:
        with self.lock:
            self._prune()
            while len(self.runs) >= 20:
                self.runs.popitem(last=False)
            ident, capability = secrets.token_hex(16), secrets.token_hex(32)
            self.runs[ident] = {"deadline": self.clock() + 600, "capability": capability,
                                "bundle": bundle, "private": private, "query": query}
        return {**trace, "run_id": ident, "reveal_capability": capability, "expires_in_seconds": 600}

    def _resolve(self, body):
        spec = body.get("spec", self.spec)
        check = validate(spec, self.snapshot_id, self.digest)
        if check["status"] != "ok":
            raise APIError(400, "invalid_spec")
        if body["pipeline_hash"] != check["hash"] or body["snapshot_id"] != self.snapshot_id:
            raise APIError(409, "incompatible")
        return spec

    def _model(self):
        if self.embedder is None:
            with self.lock:
                if self.embedder is None:
                    try:
                        self.embedder = get_embedder("bge-m3", cache_dir=self.model_cache,
                                                     threads=2)
                    except EmbedderUnavailable:
                        raise APIError(503, "model_unavailable") from None

    def _dispatch(self, method: str, path: str, env: dict):
        if path == "/health" and method == "GET":
            try:
                self._read_token()
                available = self.memory_only or self._snapshot_available()
            except APIError:
                available = False
            out = {"status": "ready" if available else "unavailable"}
            if self.memory_only:
                out["lab"] = "disabled"
            return (200 if available else 503), out
        self._token(env.get("HTTP_X_HERMES_SIDECAR_TOKEN"))
        if not path.startswith(PREFIX) or env.get("QUERY_STRING"):
            raise APIError(404, "not_found")
        route = path[len(PREFIX):]
        if self.memory_only:
            if route.startswith("/memory/"):
                try:
                    return self._memory(method, route, env)
                except MemoryViewError as exc:
                    raise APIError(exc.status, exc.code) from None
            raise APIError(503, "snapshot_unavailable")
        available = self._snapshot_available()
        if route == "/status" and method == "GET":
            runtime = self.receipts.read("runtime")
            return (200 if available else 503), {"schema": "quoth-control/v1", "state": "ok" if available else "invalid",
                         "snapshot": {"id": self.snapshot_id, "db_sha256": self.digest} if available else None,
                         "effective_runtime": runtime, "reason": None if available else "snapshot_invalid"}
        if route == "/snapshots" and method == "GET":
            return 200, {"schema": "quoth-control/v1", "state": "ok" if available else "invalid",
                         "snapshots": [{"id": self.snapshot_id, "db_sha256": self.digest,
                                        "selectable": available, "live": "unknown", "rollback": "unknown"}]
                         if available else []}
        if route in ("/" + name for name in NAMES) and method == "GET":
            return 200, self.receipts.read(route[1:])
        if route == "/lab/operators" and method == "GET":
            return 200, catalogue()
        if route == "/lab/pipelines/production" and method == "GET":
            return 200, {"spec": self.spec, "hash": spec_hash(self.spec),
                         "fidelity": {"state": "unavailable", "reason": "no_ci_receipt"}}
        if route == "/lab/pipelines/validate" and method == "POST":
            return 200, validate(self._body(env, {"schema", "name", "parent", "snapshot", "registry", "nodes", "output", "budget"}, {"meta"}), self.snapshot_id, self.digest)
        if route.startswith("/memory/"):
            # Memory views do not depend on the frozen lab snapshot; same token check above.
            try:
                return self._memory(method, route, env)
            except MemoryViewError as exc:
                raise APIError(exc.status, exc.code) from None
        if not available:
            raise APIError(503, "snapshot_unavailable")
        if route == "/lab/evals" and method == "POST":
            if self.lab_state is None:
                raise APIError(503, "lab_state_unavailable")
            body = self._body(env, {"spec", "suites"})
            if body["suites"] != list(SUITES) or validate(body["spec"], self.snapshot_id, self.digest)["status"] != "ok":
                raise APIError(400, "invalid_spec_or_suites")
            with self.lock:
                now = self.clock()
                self.eval_admissions = [t for t in self.eval_admissions if now - t < 60]
                if len(self.eval_admissions) >= 2:
                    raise APIError(429, "eval_rate_limited")
                if not self.eval_slot.acquire(blocking=False):
                    raise APIError(429, "busy")
                self.eval_admissions.append(now)
            ident = secrets.token_hex(16)
            with self.lock:
                self.evals[ident] = {"state": "queued", "spec_hash": spec_hash(body["spec"]),
                                     "deadline": self.clock() + 120, "cancelled": False}
                while len(self.evals) > 50:
                    expired, _ = self.evals.popitem(last=False)
                    self.eval_files.pop(expired, None)
            try:
                threading.Thread(target=self._evaluate, args=(ident, body["spec"]), daemon=True).start()
            except RuntimeError:
                with self.lock:
                    self.evals.pop(ident, None)
                self.eval_slot.release()
                raise APIError(503, "eval_unavailable") from None
            return 202, {"eval_id": ident, "state": "queued"}
        match_eval = EVAL_PATH.fullmatch(route)
        if match_eval and method == "GET":
            with self.lock:
                job = self.evals.get(match_eval.group(1))
                if job is None:
                    raise APIError(404, "eval_unknown")
                if job["state"] in {"queued", "running"} and self.clock() >= job["deadline"]:
                    job["cancelled"] = True
                    job["state"] = "failed"
                    job["reason"] = "evaluation_budget_exceeded"
                return 200, {key: value for key, value in job.items() if key not in {"deadline", "cancelled"}}
        if match_eval and method == "DELETE":
            with self.lock:
                job = self.evals.get(match_eval.group(1))
                if job is None:
                    raise APIError(404, "eval_unknown")
                if job["state"] in {"queued", "running"}:
                    job["cancelled"] = True
                    job["state"] = "failed"
                    job["reason"] = "evaluation_cancelled"
            return 204, None
        if route == "/lab/candidates" and method == "POST":
            body = self._body(env, {"spec", "eval_id"})
            spec = body["spec"]
            if self.lab_state is None or not self.code_commit:
                raise APIError(503, "candidate_export_unavailable")
            check = validate(spec, self.snapshot_id, self.digest)
            with self.lock:
                job = self.evals.get(body["eval_id"])
            if check["status"] != "ok" or not job or job["state"] != "done" or job["result"]["spec_hash"] != check["hash"]:
                raise APIError(409, "eval_incomplete_or_mismatched")
            result = job["result"]
            gates = result["gates"]
            if (not result["eligible"] or set(gates) != GATE_NAMES or
                    not all(value is True for value in gates.values()) or
                    result["snapshot"] != {"id": self.snapshot_id, "db_sha256": self.digest} or
                    result["production_hash"] != spec_hash(self.spec) or
                    result["gates_digest"] != hashlib.sha256(json.dumps(
                        json.loads((ASSETS / "e1-temporal.json").read_text())["gates"],
                        sort_keys=True).encode()).hexdigest() or
                    any(_digest(ASSETS / name) != result["suites"][suite] for suite, name in
                        (("ordinary", "golden.jsonl"), ("protected_facets", "q2-controls.json"),
                         ("e1_temporal", "e1-temporal.json"))) or
                    result["suites"].get("abstention") != result["suites"]["e1_temporal"] or
                    _digest(self.eval_files[body["eval_id"]]) != job["eval_hash"].removeprefix("sha256:")):
                raise APIError(409, "protected_gate_rejected_or_changed")
            self._require_snapshot()
            payload = {"schema": "quoth-lab-candidate/v1", "spec": spec, "spec_hash": check["hash"],
                       "registry": spec["registry"], "code_commit": self.code_commit,
                       "snapshot": result["snapshot"], "suites": result["suites"],
                       "gates_digest": result["gates_digest"], "eval_aggregate": result["candidate"],
                       "gate_verdicts": result["gates"], "created_at": int(time.time())}
            digest, path = persist(self.lab_state, "candidates", payload)
            if _digest(path) != digest:
                raise APIError(503, "candidate_state_unavailable")
            return 201, {"candidate_hash": "sha256:" + digest, "path": str(path), "gate_summary": result["gates"]}
        if route == "/lab/runs/trace" and method == "POST":
            body = self._body(env, {"query", "pipeline_hash", "snapshot_id", "k", "query_class", "reason_code"}, {"spec"})
            spec = self._resolve(body)
            if body["query_class"] != "unknown" or body["reason_code"] != "unknown":
                raise APIError(400, "unsupported_classification")
            if not self.slots.acquire(blocking=False):
                raise APIError(429, "busy")
            try:
                # Reject an altered or missing snapshot before returning any private result.
                self._require_snapshot()
                self._model()
                with connect_read_only(self.db_path) as conn:
                    trace, bundle, private = run(conn, spec, body["query"], snapshot_id=self.snapshot_id,
                                                  db_sha256=self.digest, embedder=self.embedder, k=body["k"])
                self._require_snapshot()
                return 200, self._save(trace, bundle, private, body["query"])
            finally:
                self.slots.release()
        if route == "/lab/runs/compare" and method == "POST":
            body = self._body(env, {"query", "pipeline_hash", "snapshot_id", "k_a", "k_b"}, {"spec_b"})
            self._resolve(body)
            spec_b = body.get("spec_b", self.spec)
            if validate(spec_b, self.snapshot_id, self.digest)["status"] != "ok":
                raise APIError(400, "invalid_spec")
            if any(type(body[key]) is not int or not 1 <= body[key] <= 20 for key in ("k_a", "k_b")):
                raise APIError(400, "invalid_k")
            if not self.slots.acquire(blocking=False):
                raise APIError(429, "busy")
            try:
                self._require_snapshot()
                self._model()
                begin = self.clock()
                with connect_read_only(self.db_path) as conn:
                    a, a_bundle, a_private = run(conn, self.spec, body["query"], snapshot_id=self.snapshot_id,
                                                  db_sha256=self.digest, embedder=self.embedder, k=body["k_a"])
                    b, b_bundle, b_private = run(conn, spec_b, body["query"], snapshot_id=self.snapshot_id,
                                                  db_sha256=self.digest, embedder=self.embedder, k=body["k_b"])
                if self.clock() - begin > 8:
                    raise APIError(429, "budget_exceeded")
                self._require_snapshot()
                a_ids, b_ids = a["output"]["selected_refs"], b["output"]["selected_refs"]
                result = {"a": self._save(a, a_bundle, a_private, body["query"]),
                          "b": self._save(b, b_bundle, b_private, body["query"]),
                          "diff": {"added": [i for i in b_ids if i not in a_ids],
                                   "removed": [i for i in a_ids if i not in b_ids],
                                   "overlap": len(set(a_ids) & set(b_ids)),
                                   "rank_moves": [{"candidate_ref": i, "from": a_ids.index(i) + 1,
                                                   "to": b_ids.index(i) + 1} for i in b_ids
                                                  if i in a_ids and a_ids.index(i) != b_ids.index(i)]}}
                return 200, result
            finally:
                self.slots.release()
        match = RUN_PATH.fullmatch(route)
        if match:
            ident, action = match.groups()
            with self.lock:
                self._prune()
                record = self.runs.get(ident)
            if record is None:
                raise APIError(404, "run_expired")
            if not action and method == "DELETE":
                with self.lock:
                    self.runs.pop(ident, None)
                return 204, None
            if action == "reveal" and method == "POST":
                body = self._body(env, {"capability", "node", "chunk_ids", "include_query"})
                if not isinstance(body["capability"], str) or not hmac.compare_digest(record["capability"], body["capability"]):
                    raise APIError(403, "forbidden")
                node, ids = body["node"], body["chunk_ids"]
                if node not in record["private"] or not isinstance(ids, list) or len(ids) > 20 or any(type(i) is not int for i in ids) or type(body["include_query"]) is not bool:
                    raise APIError(400, "invalid_reveal")
                rows = record["private"][node]
                if any(i not in rows for i in ids):
                    raise APIError(400, "invalid_reveal")
                items = []
                remaining = 18000
                for i in ids:
                    row = rows[i]
                    text = str(row["snippet"])[:min(1800, remaining)]
                    remaining -= len(text)
                    items.append({"chunk_id": i, "locator": f'{row["path"]}:{row["line_start"]}-{row["line_end"]}',
                                  "heading_path": row["heading_path"], "text": text})
                return 200, {"items": items, "query": record["query"] if body["include_query"] else None}
        raise APIError(404, "not_found")

    def _memory(self, method: str, route: str, env: dict):
        """Read-only memory routes. POST only carries filters or a query; nothing is written."""
        memory = self.memory
        if route == "/memory/sources" and method == "GET":
            return 200, memory.sources()
        if route == "/memory/claims" and method == "POST":
            return 200, memory.claims(self._body(env, set(), MEMORY_FILTERS))
        match = CLAIM_PATH.fullmatch(route)
        if match and method == "GET":
            return 200, memory.claim(int(match.group(1)))
        if route == "/memory/queue" and method == "GET":
            return 200, memory.queue()
        if route == "/memory/core" and method == "GET":
            return 200, memory.core()
        match = CORE_VERSION_PATH.fullmatch(route)
        if match and method == "GET":
            return 200, memory.core_version(int(match.group(1)))
        if route == "/memory/recall" and method == "POST":
            return 200, memory.recall(self._body(env, {"text"}))
        if route == "/memory/episodes" and method == "POST":
            return 200, memory.episodes(self._body(env, {"query"}, {"k"}))
        if route == "/memory/sidecar" and method == "GET":
            return 200, memory.sidecar_health()
        if route == "/memory/refresh" and method == "GET":
            return 200, memory.refresh()
        raise APIError(404, "not_found")

    def _evaluate(self, ident, spec):
        def cancelled():
            with self.lock:
                job = self.evals.get(ident)
                return job is None or job["cancelled"] or self.clock() >= job["deadline"]
        try:
            with self.lock:
                if not self.evals[ident]["cancelled"]:
                    self.evals[ident]["state"] = "running"
            if cancelled():
                raise TimeoutError("evaluation_budget_exceeded")
            if not self._snapshot_available():
                raise ValueError("snapshot_unavailable")
            self._model()
            with connect_read_only(self.db_path) as conn:
                conn.set_progress_handler(lambda: 1 if cancelled() else 0, 1000)
                result = evaluate(conn, spec, self.spec, self.embedder, self.snapshot_id, self.digest,
                                  cancelled=cancelled)
            if cancelled():
                raise TimeoutError("evaluation_budget_exceeded")
            self._require_snapshot()
            digest, path = persist(self.lab_state, "evals", result)
            with self.lock:
                job = self.evals.get(ident)
                if job is not None and not job["cancelled"] and self.clock() < job["deadline"]:
                    self.eval_files[ident] = path
                    self.evals[ident] = {"state": "done", "eval_hash": "sha256:" + digest, "result": result}
                else:
                    raise TimeoutError("evaluation_budget_exceeded")
        except Exception:
            with self.lock:
                if ident in self.evals and self.evals[ident]["state"] != "failed":
                    self.evals[ident] = {"state": "failed", "reason": "evaluation_unavailable"}
        finally:
            self.eval_slot.release()

    def __call__(self, env, start_response):
        path, method = env.get("PATH_INFO", ""), env.get("REQUEST_METHOD", "")
        try:
            status, value = self._dispatch(method, path, env)
        except APIError as exc:
            status, value = exc.status, {"error": {"code": exc.code, "retryable": status_retryable(exc.status)}}
        except (ValueError, TypeError, KeyError, TimeoutError):
            status, value = 400, {"error": {"code": "invalid_request", "retryable": False}}
        except Exception:
            status, value = 500, {"error": {"code": "internal_error", "retryable": False}}
        payload = b"" if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        start_response(f"{status} {HTTPStatus(status).phrase}", [("Content-Type", "application/json; charset=utf-8"),
                       ("Cache-Control", "no-store"), ("Content-Length", str(len(payload))),
                       ("X-Content-Type-Options", "nosniff")])
        return [payload]


def status_retryable(status):
    return status in {429, 503}


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_stat(path):
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        # Never log raw paths, query strings, headers or bodies.
        pass


class BoundedThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Threaded WSGI with a hard cap before a request thread is created."""

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._request_slots = threading.BoundedSemaphore(MAX_HTTP_REQUESTS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            payload = b'{"error":{"code":"server_busy","retryable":true}}'
            response = (b"HTTP/1.1 503 Service Unavailable\r\n"
                        b"Content-Type: application/json; charset=utf-8\r\n"
                        b"Cache-Control: no-store\r\n"
                        b"X-Content-Type-Options: nosniff\r\n"
                        b"Connection: close\r\nContent-Length: " + str(len(payload)).encode() +
                        b"\r\n\r\n" + payload)
            try:
                getattr(request, "sendall")(response)
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def serve(app: ControlApp, port: int = 17889):
    with make_server("127.0.0.1", port, app, server_class=BoundedThreadingWSGIServer,
                     handler_class=QuietHandler) as server:
        server.serve_forever()
