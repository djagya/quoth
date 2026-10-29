"""Isolated control tests: no WebUI, vault or production service."""
import io
import json
import http.client
import threading
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from wsgiref.simple_server import make_server

import pytest

from quoth.control.api import BoundedThreadingWSGIServer, ControlApp, PREFIX, QuietHandler
from quoth.control.lab import ASSETS, GATE_NAMES, _digest
from quoth.embed.backends import get_embedder
from quoth.embed.store import embed_chunks
from quoth.ingest import ingest_vault
from quoth.pipeline import spec_hash


@pytest.fixture
def app(conn, vault, db_path, tmp_path):
    ingest_vault(conn, vault)
    embedder = get_embedder("hash-ngram")
    embed_chunks(conn, embedder, batch_size=8)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    import hashlib
    digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
    token = tmp_path / "quoth.token"
    token.write_text("synthetic-token")
    service = ControlApp(db_path, "synthetic", digest, token, embedder=embedder, model="hash-ngram")
    assert service.ready
    return service, token, tmp_path


def call(app, path, method="GET", body=None, token: str | None = "synthetic-token", **env):
    data = b"" if body is None else json.dumps(body).encode()
    headers = {}
    def respond(status, fields):
        headers["status"] = int(status[:3]); headers.update(dict(fields))
    result = app({"PATH_INFO": path, "REQUEST_METHOD": method, "QUERY_STRING": "",
                  "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(data)),
                  "wsgi.input": io.BytesIO(data), "HTTP_X_HERMES_SIDECAR_TOKEN": token, **env}, respond)
    return headers, json.loads(b"".join(result) or b"null")


def trace_request(app, query="PRIVATE_SENTINEL", k=10):
    return {"query": query, "pipeline_hash": spec_hash(app.spec), "snapshot_id": "synthetic", "k": k,
            "query_class": "unknown", "reason_code": "unknown"}


def test_auth_rotation_path_and_private_errors(app):
    service, token, _ = app
    assert call(service, "/health", token=None)[1] == {"status": "ready"}
    for value, status in [(None, 401), ("forged", 401)]:
        hdr, body = call(service, PREFIX + "/status", token=value)
        assert hdr["status"] == status and hdr["Cache-Control"] == "no-store"
        assert "snapshot" not in str(body)
    token.write_text("rotated")
    assert call(service, PREFIX + "/status")[0]["status"] == 401
    assert call(service, PREFIX + "/status", token="rotated")[0]["status"] == 200
    snapshots = call(service, PREFIX + "/snapshots", token="rotated")[1]
    assert snapshots["snapshots"][0]["live"] == "unknown"
    for route in ("freshness", "runtime", "quality"):
        _, receipt = call(service, PREFIX + "/" + route, token="rotated")
        assert receipt["state"] == "unavailable" and receipt["data"] is None
    token.unlink()
    assert call(service, PREFIX + "/lab/operators", token="rotated")[0]["status"] == 503
    token.write_text("synthetic-token")
    for path in (PREFIX + "/../status", PREFIX + "/status?query=private", "/status"):
        assert call(service, path)[0]["status"] == 404
    assert call(service, PREFIX + "/status", QUERY_STRING="private")[0]["status"] == 404


def test_trace_reveal_expiry_and_non_persistence(app):
    service, token, directory = app
    sent = "PRIVATE_SENTINEL"
    hdr, result = call(service, PREFIX + "/lab/runs/trace", "POST", trace_request(service, sent))
    assert hdr["status"] == 200 and hdr["Cache-Control"] == "no-store"
    assert sent not in json.dumps(result)
    assert "Garden.md" not in json.dumps(result)
    ident, capability = result["run_id"], result["reveal_capability"]
    cid = result["trace"][1]["items"][0]["chunk_id"]
    route = PREFIX + "/lab/runs/" + ident + "/reveal"
    body = {"capability": capability, "node": "dense", "chunk_ids": [cid], "include_query": True}
    assert call(service, route, "POST", {**body, "capability": "bad"})[0]["status"] == 403
    assert call(service, route, "POST", {**body, "chunk_ids": [999999]})[0]["status"] == 400
    assert call(service, route, "POST", body)[1]["query"] == sent
    assert sent.encode() not in b"".join(p.read_bytes() for p in directory.iterdir() if p.is_file())
    assert call(service, PREFIX + "/lab/runs/" + ident, "DELETE")[0]["status"] == 204
    assert call(service, route, "POST", body)[0]["status"] == 404
    service.clock = lambda: 100
    _, second = call(service, PREFIX + "/lab/runs/trace", "POST", trace_request(service))
    service.clock = lambda: 701
    assert call(service, PREFIX + "/lab/runs/" + second["run_id"] + "/reveal", "POST", body)[0]["status"] == 404


def test_invalid_snapshot_and_registry(app, tmp_path):
    service, token, directory = app
    assert call(service, PREFIX + "/lab/runs/trace", "POST", {**trace_request(service), "extra": 1})[0]["status"] == 400
    assert call(service, PREFIX + "/lab/runs/trace", "POST", {**trace_request(service), "pipeline_hash": "sha256:wrong"})[0]["status"] == 409
    spec = deepcopy(service.spec)
    spec["nodes"][1]["op"] = "rerank_cross_encoder"
    assert call(service, PREFIX + "/lab/pipelines/validate", "POST", spec)[1]["status"] == "invalid"
    broken = ControlApp(tmp_path / "missing.db", "missing", "digest", token)
    assert not broken.ready
    assert call(broken, PREFIX + "/status")[1]["state"] == "invalid"
    assert call(broken, PREFIX + "/lab/runs/trace", "POST", trace_request(service))[0]["status"] == 503


def test_snapshot_changed_after_readiness_denies_trace(app):
    service, _, _ = app
    with service.db_path.open("ab") as db:
        db.write(b"synthetic_corruption")
    headers, body = call(service, PREFIX + "/lab/runs/trace", "POST", trace_request(service))
    assert headers["status"] == 503 and body["error"]["code"] == "snapshot_unavailable"
    assert not service.ready
    assert call(service, "/health", token=None)[0]["status"] == 503
    assert call(service, PREFIX + "/status")[0]["status"] == 503


@pytest.mark.parametrize("change", ["remove", "replace", "in_place"])
def test_snapshot_admission_detects_path_and_inode_changes(app, change):
    service, _, directory = app
    original = service.db_path.read_bytes()
    if change == "remove":
        service.db_path.unlink()
    elif change == "replace":
        replacement = directory / "replacement.db"
        replacement.write_bytes(original)
        replacement.replace(service.db_path)
    else:
        with service.db_path.open("r+b") as database:
            database.seek(100)
            database.write(bytes([original[100] ^ 1]))
    assert call(service, "/health", token=None)[0]["status"] == 503
    headers, body = call(service, PREFIX + "/status")
    assert headers["status"] == 503 and body["state"] == "invalid"
    assert call(service, PREFIX + "/lab/runs/trace", "POST", trace_request(service))[0]["status"] == 503


def test_startup_digest_mismatch_and_malformed_token_fail_closed(app):
    service, token, _ = app
    wrong = ControlApp(service.db_path, "synthetic", "0" * 64, token,
                       embedder=service.embedder, model="hash-ngram")
    assert not wrong.ready
    assert call(wrong, "/health", token=None)[0]["status"] == 503
    token.write_text("bad token")
    assert call(service, "/health", token=None)[0]["status"] == 503
    headers, body = call(service, PREFIX + "/status", token="bad token")
    assert headers["status"] == 503 and body["error"]["code"] == "token_unavailable"


def test_intact_requests_do_not_repeat_full_snapshot_digest(app):
    service, _, _ = app
    compare = {"query": "safe", "pipeline_hash": spec_hash(service.spec),
               "snapshot_id": "synthetic", "k_a": 2, "k_b": 1}
    with patch("quoth.control.api.file_digest", side_effect=AssertionError("digest reread")):
        assert call(service, PREFIX + "/lab/runs/trace", "POST",
                    trace_request(service, query="safe", k=2))[0]["status"] == 200
        assert call(service, PREFIX + "/lab/runs/compare", "POST", compare)[0]["status"] == 200


def test_compare_is_real_and_query_private(app):
    service, _, directory = app
    request = {"query": "PRIVATE_SENTINEL", "pipeline_hash": spec_hash(service.spec),
               "snapshot_id": "synthetic", "k_a": 10, "k_b": 1}
    headers, result = call(service, PREFIX + "/lab/runs/compare", "POST", request)
    assert headers["status"] == 200
    assert len(result["a"]["output"]["selected_refs"]) >= len(result["b"]["output"]["selected_refs"])
    assert result["diff"]["overlap"] == len(result["b"]["output"]["selected_refs"])
    assert result["a"]["execution_hash"] != result["b"]["execution_hash"]
    assert "PRIVATE_SENTINEL" not in json.dumps(result)
    assert call(service, PREFIX + "/lab/runs/compare", "POST", {**request, "k_b": 21})[0]["status"] == 400
    assert b"PRIVATE_SENTINEL" not in b"".join(p.read_bytes() for p in directory.iterdir() if p.is_file())

def test_inline_dag_trace_and_general_compare(app):
    service, _, directory = app
    draft = deepcopy(service.spec)
    draft["parent"] = spec_hash(service.spec)
    draft["name"] = "synthetic-filtered"
    draft["nodes"].insert(2, {"id": "filter", "op": "filter", "op_version": "1",
                              "in": {"in": "dense"}, "config": {"path_prefix": "ME/"}})
    draft["nodes"][-1]["in"] = {"in": "filter"}
    result = call(service, PREFIX + "/lab/pipelines/validate", "POST", draft)[1]
    assert result["status"] == "ok" and result["hash"] == spec_hash(draft)
    req = {**trace_request(service), "pipeline_hash": result["hash"], "spec": draft}
    hdr, trace = call(service, PREFIX + "/lab/runs/trace", "POST", req)
    assert hdr["status"] == 200 and trace["pipeline_hash"] == result["hash"]
    assert "PRIVATE_SENTINEL" not in json.dumps(trace)
    compare = {"query": "PRIVATE_SENTINEL", "pipeline_hash": spec_hash(service.spec),
               "snapshot_id": "synthetic", "k_a": 10, "k_b": 10, "spec_b": draft}
    hdr, pair = call(service, PREFIX + "/lab/runs/compare", "POST", compare)
    assert hdr["status"] == 200 and pair["b"]["pipeline_hash"] == result["hash"]
    assert pair["a"]["pipeline_hash"] != pair["b"]["pipeline_hash"]
    assert b"PRIVATE_SENTINEL" not in b"".join(p.read_bytes() for p in directory.iterdir() if p.is_file())

def test_fixed_eval_poll_and_protected_export_denial(app):
    service, _, directory = app
    service.lab_state = directory / "lab"
    service.code_commit = "a" * 40
    fake = {"schema": "quoth-lab-eval/v1", "spec_hash": spec_hash(service.spec),
            "snapshot": {"id": "synthetic", "db_sha256": service.digest},
            "suites": {"e1_temporal": "digest", "protected_facets": "digest"},
            "candidate": {"ordinary": {}}, "gates_digest": "digest",
            "gates": {"G1_temporal_answer": False, "Q2_protected_facets": True}, "eligible": False}
    assert call(service, PREFIX + "/lab/evals", "POST", {"spec": service.spec, "suites": ["ordinary"]})[0]["status"] == 400
    with patch("quoth.control.api.evaluate", return_value=fake):
        hdr, submitted = call(service, PREFIX + "/lab/evals", "POST", {"spec": service.spec,
            "suites": ["ordinary", "protected_facets", "e1_temporal", "abstention"]})
        assert hdr["status"] == 202
        import time
        for _ in range(100):
            _, job = call(service, PREFIX + "/lab/evals/" + submitted["eval_id"])
            if job["state"] in ("done", "failed"):
                break
            time.sleep(.01)
    assert job["state"] == "done" and not job["result"]["eligible"]
    assert call(service, PREFIX + "/lab/candidates", "POST",
                {"spec": service.spec, "eval_id": submitted["eval_id"]})[0]["status"] == 409
    assert not list((directory / "lab").glob("candidates/*.json"))

    passing = deepcopy(fake)
    passing["gates"] = {key: True for key in GATE_NAMES}
    passing["eligible"] = True
    passing["production_hash"] = spec_hash(service.spec)
    import hashlib
    passing["gates_digest"] = hashlib.sha256(json.dumps(json.loads(
        (ASSETS / "e1-temporal.json").read_text())["gates"], sort_keys=True).encode()).hexdigest()
    passing["suites"] = {"ordinary": _digest(ASSETS / "golden.jsonl"),
                         "protected_facets": _digest(ASSETS / "q2-controls.json"),
                         "e1_temporal": _digest(ASSETS / "e1-temporal.json"),
                         "abstention": _digest(ASSETS / "e1-temporal.json")}
    with patch("quoth.control.api.evaluate", return_value=passing):
        _, accepted = call(service, PREFIX + "/lab/evals", "POST", {"spec": service.spec,
            "suites": ["ordinary", "protected_facets", "e1_temporal", "abstention"]})
        for _ in range(100):
            _, job = call(service, PREFIX + "/lab/evals/" + accepted["eval_id"])
            if job["state"] in ("done", "failed"):
                break
            time.sleep(.01)
    assert job["state"] == "done"
    passing_file = service.eval_files[accepted["eval_id"]]
    original = passing_file.read_bytes()
    passing_file.write_text("corrupt")
    assert call(service, PREFIX + "/lab/candidates", "POST",
                {"spec": service.spec, "eval_id": accepted["eval_id"]})[0]["status"] == 409
    passing_file.write_bytes(original)
    hdr, exported = call(service, PREFIX + "/lab/candidates", "POST",
                         {"spec": service.spec, "eval_id": accepted["eval_id"]})
    assert hdr["status"] == 201
    content = Path(exported["path"]).read_bytes()
    assert exported["candidate_hash"] == "sha256:" + __import__("hashlib").sha256(content).hexdigest()
    assert b"PRIVATE_SENTINEL" not in content


def test_eval_budget_rate_and_cancellation_fail_closed(app):
    import threading
    import time
    service, _, directory = app
    service.lab_state = directory / "lab"
    now = [100.0]
    service.clock = lambda: now[0]
    entered, release = threading.Event(), threading.Event()
    request = {"spec": service.spec, "suites": ["ordinary", "protected_facets", "e1_temporal", "abstention"]}

    def stalled(*args, **kwargs):
        entered.set()
        release.wait(2)
        return {"eligible": True, "spec_hash": spec_hash(service.spec)}

    with patch("quoth.control.api.evaluate", side_effect=stalled):
        _, first = call(service, PREFIX + "/lab/evals", "POST", request)
        assert entered.wait(1)
        assert call(service, PREFIX + "/lab/evals", "POST", request)[1]["error"]["code"] == "busy"
        now[0] += 121
        path = PREFIX + "/lab/evals/" + first["eval_id"]
        _, state = call(service, path)
        assert state["state"] == "failed" and state["reason"] == "evaluation_budget_exceeded"
        assert call(service, path, "DELETE")[0]["status"] == 204
        release.set()
        for _ in range(100):
            if service.eval_slot.acquire(blocking=False):
                service.eval_slot.release()
                break
            time.sleep(.01)
        else:
            pytest.fail("eval worker did not release slot")
        assert call(service, path)[1]["state"] == "failed"
        # Hold the second worker inside evaluate so the DELETE cannot race completion.
        release.clear()
        _, second = call(service, PREFIX + "/lab/evals", "POST", request)
        second_path = PREFIX + "/lab/evals/" + second["eval_id"]
        assert call(service, second_path, "DELETE")[0]["status"] == 204
        assert call(service, second_path)[1]["reason"] == "evaluation_cancelled"
        release.set()
        for _ in range(100):
            if service.eval_slot.acquire(blocking=False):
                service.eval_slot.release()
                break
            time.sleep(.01)
        else:
            pytest.fail("cancelled worker did not release slot")
        _, third = call(service, PREFIX + "/lab/evals", "POST", request)
        assert "eval_id" in third
        headers, rejected = call(service, PREFIX + "/lab/evals", "POST", request)
        assert headers["status"] == 429 and rejected["error"]["code"] == "eval_rate_limited"
        assert call(service, PREFIX + "/lab/candidates", "POST",
                    {"spec": service.spec, "eval_id": first["eval_id"]})[0]["status"] == 503
        # The third evaluation runs concurrently and may legitimately persist its
        # aggregate; wait for it, then require that only it reached disk.
        for _ in range(300):
            if service.eval_slot.acquire(blocking=False):
                service.eval_slot.release()
                break
            time.sleep(.01)
        else:
            pytest.fail("third worker did not release slot")
        third_state = call(service, PREFIX + "/lab/evals/" + third["eval_id"])[1]
        persisted = sorted(p.name for p in (directory / "lab").glob("evals/*.json"))
        if third_state["state"] == "done":
            assert persisted == [third_state["eval_hash"].removeprefix("sha256:") + ".json"]
        else:
            assert persisted == []


def test_unexpected_eval_failure_is_terminal_private_and_releases_slot(app):
    service, _, directory = app
    service.lab_state = directory / "lab"
    request = {"spec": service.spec,
               "suites": ["ordinary", "protected_facets", "e1_temporal", "abstention"]}
    completed = {"eligible": False, "spec_hash": spec_hash(service.spec)}
    with patch("quoth.control.api.evaluate",
               side_effect=[RuntimeError("PRIVATE_SENTINEL"), completed]):
        _, first = call(service, PREFIX + "/lab/evals", "POST", request)
        first_path = PREFIX + "/lab/evals/" + first["eval_id"]
        failed = {}
        for _ in range(100):
            _, failed = call(service, first_path)
            if failed["state"] == "failed":
                break
            time.sleep(.01)
        assert failed == {"state": "failed", "reason": "evaluation_unavailable"}
        assert "PRIVATE_SENTINEL" not in json.dumps(failed)

        _, second = call(service, PREFIX + "/lab/evals", "POST", request)
        second_path = PREFIX + "/lab/evals/" + second["eval_id"]
        succeeded = {}
        for _ in range(100):
            _, succeeded = call(service, second_path)
            if succeeded["state"] == "done":
                break
            time.sleep(.01)
        assert succeeded["state"] == "done"


def test_unexpected_request_failure_is_bounded_json(app):
    service, _, _ = app
    with patch.object(service.receipts, "read", side_effect=RuntimeError("PRIVATE_SENTINEL")):
        headers, body = call(service, PREFIX + "/status")
    assert headers["status"] == 500
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert body == {"error": {"code": "internal_error", "retryable": False}}
    assert "PRIVATE_SENTINEL" not in json.dumps(body)


def test_loopback_server_stays_responsive_while_expensive_work_is_blocked(app):
    service, _, _ = app
    entered = threading.Event()
    release = threading.Event()
    count = 0
    count_lock = threading.Lock()

    def blocked_run(*args, **kwargs):
        nonlocal count
        with count_lock:
            count += 1
            if count == 2:
                entered.set()
        release.wait(2)
        return ({"trace": [], "output": {"selected_refs": []}}, {}, {})

    server = make_server("127.0.0.1", 0, service,
                         server_class=BoundedThreadingWSGIServer, handler_class=QuietHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.server_port

    def request(path, method="GET", body=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        payload = None if body is None else json.dumps(body)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Hermes-Sidecar-Token"] = "synthetic-token"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), json.loads(response.read() or b"null")
        connection.close()
        return result

    workers = []
    results = []
    try:
        with patch("quoth.control.api.run", side_effect=blocked_run):
            for _ in range(2):
                worker = threading.Thread(target=lambda: results.append(request(
                    PREFIX + "/lab/runs/trace", "POST", trace_request(service, query="blocked"))))
                workers.append(worker)
                worker.start()
            assert entered.wait(1)
            assert request("/health", token=False)[0] == 200
            status, headers, value = request(PREFIX + "/status")
            assert status == 200 and value["state"] == "ok" and headers["Cache-Control"] == "no-store"
            busy = request(PREFIX + "/lab/runs/trace", "POST",
                           trace_request(service, query="excess"))
            assert busy[0] == 429 and busy[2]["error"]["code"] == "busy"
            release.set()
            for worker in workers:
                worker.join(2)
            assert [result[0] for result in results] == [200, 200]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        server_thread.join(2)
