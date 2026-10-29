"""`quoth health`: every row goes red on its broken state; no memory content in output."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import socketserver
import sys
import tempfile
import threading

import pytest

from quoth import __version__, cli, health
from quoth.ledger import cli as ledger_cli
from quoth.ledger import core as ledger

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
SENTINEL = "PRIVATE_SENTINEL_CLAIM_TEXT"


def stamp(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def green_health() -> dict:
    lane = {"status": "green", "reasons": [], "messages": 12, "embedded": 12, "pending": 0,
            "newest": stamp(NOW), "last_tick_at": stamp(NOW), "prefetch": False}
    return {"ok": True, "schema": "quoth-sidecar/v1", "version": __version__, "status": "green",
            "ledger": {"status": "green", "reasons": [], "pending": 3, "accepted_current": 20,
                       "oldest_pending_age_days": 1.5},
            "evidence": {"status": "ok", "snapshot_id": "snap-1"}, "quote_verification": True,
            "episodes": dict(lane), "owner_episodes": dict(lane)}


class FakeSidecar:
    """A Unix-socket HTTP/1.0 responder whose /v1/health body and status the test controls."""

    def __init__(self):
        self.body = green_health()
        self.status = 200
        self.dir = Path(tempfile.mkdtemp(prefix="mn"))  # AF_UNIX path length on macOS
        self.path = self.dir / "s.sock"
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.rfile.readline()
                data = json.dumps(fake.body).encode()
                self.wfile.write(f"HTTP/1.0 {fake.status} X\r\nContent-Length: {len(data)}\r\n\r\n".encode()
                                 + data)

        self.server = socketserver.ThreadingUnixStreamServer(str(self.path), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def fake():
    f = FakeSidecar()
    yield f
    f.close()


@pytest.fixture
def heartbeat(tmp_path):
    path = tmp_path / "refresh" / "heartbeat.json"
    path.parent.mkdir()

    def write(**over):
        beat = {"schema": "quoth-heartbeat/v1", "status": "green", "reasons": [],
                "checked_at": stamp(NOW - timedelta(hours=2)), "fresh_as_of": stamp(NOW - timedelta(hours=3))}
        beat.update(over)
        path.write_text(json.dumps(beat))
        return path
    write()
    return write


def rows_by_name(rows):
    return {r["row"]: r for r in rows}


def everything(tmp_path, fake, venv=None):
    return health.run(state_dir=tmp_path, socket_path=fake.path, venv=venv, now=NOW)


def test_all_green(tmp_path, fake, heartbeat):
    rows = everything(tmp_path, fake, venv=Path(sys.prefix))
    assert [r["row"] for r in rows] == list(health.ROWS)
    assert {r["status"] for r in rows} == {"PASS"}, rows
    assert health.exit_code(rows) == 0
    assert rows_by_name(rows)["quoth/review-queue"]["detail"] == "3 pending, oldest 1.5 d, 20 accepted"
    assert set(rows[0]) == {"row", "status", "detail"}


# ---------------------------------------------------------------- heartbeat row

@pytest.mark.parametrize("broken, why", [
    ({"schema": "quoth-heartbeat/v0"}, "not quoth-heartbeat/v1"),
    ({"checked_at": stamp(NOW - timedelta(hours=31))}, "nightly refresh not running"),
    ({"checked_at": "garbage"}, "nightly refresh not running"),
    ({"status": "red", "reasons": ["last_run_hold", "freshness_unconfirmed"]}, "red: last_run_hold"),
])
def test_heartbeat_red(tmp_path, fake, heartbeat, broken, why):
    heartbeat(**broken)
    row = rows_by_name(everything(tmp_path, fake))["quoth/heartbeat"]
    assert row["status"] == "FAIL" and why in row["detail"]


def test_missing_heartbeat_is_fail_not_pass(tmp_path, fake):
    row = rows_by_name(everything(tmp_path, fake))["quoth/heartbeat"]
    assert row["status"] == "FAIL" and "cannot read" in row["detail"]


def test_heartbeat_reasons_are_tokens_only(tmp_path, fake, heartbeat):
    heartbeat(status="red", reasons=["last_run_fail", f"{SENTINEL} with spaces"])
    row = rows_by_name(everything(tmp_path, fake))["quoth/heartbeat"]
    assert SENTINEL not in row["detail"] and "+1 other" in row["detail"]


# ---------------------------------------------------------------- sidecar rows

def test_missing_socket_fails_every_sidecar_row(tmp_path, heartbeat):
    rows = health.run(state_dir=tmp_path, venv=None, now=NOW)  # no socket under state dir
    assert [r["row"] for r in rows] == list(health.ROWS[1:])
    for r in rows[1:]:
        assert r["status"] == "FAIL", r
    assert "no answer on" in rows[1]["detail"]
    assert health.exit_code(rows) == 1


def test_sidecar_http_error_is_fail(tmp_path, fake, heartbeat):
    fake.status = 500
    rows = rows_by_name(everything(tmp_path, fake))
    assert rows["quoth/sidecar"]["status"] == "FAIL" and "returned 500" in rows["quoth/sidecar"]["detail"]


def test_sidecar_503_red_body_is_still_read_row_by_row(tmp_path, fake, heartbeat):
    """The sidecar answers 503 when its status is red; a queue backlog stays a WARN row."""
    fake.status = 503
    fake.body.update(ok=False, error="unhealthy", status="red")
    fake.body["ledger"].update(status="red", reasons=["pending_queue_old"])
    rows = rows_by_name(everything(tmp_path, fake))
    assert rows["quoth/sidecar"]["status"] == "PASS"
    assert rows["quoth/review-queue"]["status"] == "WARN"


def test_unknown_health_schema_is_fail(tmp_path, fake, heartbeat):
    fake.body["schema"] = "other/v1"
    assert rows_by_name(everything(tmp_path, fake))["quoth/sidecar"]["status"] == "FAIL"


def test_default_paths_come_from_quoth_home(tmp_path, monkeypatch, heartbeat):
    monkeypatch.setenv("QUOTH_HOME", str(tmp_path))
    rows = rows_by_name(health.run(venv=None, now=NOW))
    assert rows["quoth/heartbeat"]["status"] == "PASS"  # <QUOTH_HOME>/refresh/heartbeat.json
    assert str(tmp_path / "quoth.sock") in rows["quoth/sidecar"]["detail"]


@pytest.mark.parametrize("over", [{"evidence": {"status": "unavailable"}}, {"evidence": {"status": "disabled"}},
                                  {"quote_verification": False}])
def test_sidecar_red(tmp_path, fake, heartbeat, over):
    fake.body.update(over)
    row = rows_by_name(everything(tmp_path, fake))["quoth/sidecar"]
    assert row["status"] == "FAIL"


def test_a_rejected_pointer_is_fail_with_its_reason_and_digest_only(tmp_path, fake, heartbeat):
    fake.body["evidence"] = {"status": "pointer_rejected", "snapshot_id": "snap-1", "state": "rejected",
                             "rejected": {"reason": "manifest_invalid", "target_digest": "0123456789abcdef",
                                          "since": stamp(NOW), "attempts": 2, "retry_in_s": 900.0}}
    row = rows_by_name(everything(tmp_path, fake))["quoth/sidecar"]
    assert row["status"] == "FAIL"
    assert "evidence=pointer_rejected (pointer manifest_invalid, target 0123456789abcdef" in row["detail"]
    fake.body["evidence"]["rejected"]["reason"] = "/srv/" + SENTINEL  # never echoed
    row = rows_by_name(everything(tmp_path, fake))["quoth/sidecar"]
    assert SENTINEL not in row["detail"] and "pointer None" in row["detail"]


@pytest.mark.parametrize("token", ["candidate_stalled", "watcher_stalled"])
def test_a_stalled_admission_is_fail(tmp_path, fake, heartbeat, token):
    fake.body["evidence"] = {"status": token, "snapshot_id": "snap-1"}
    row = rows_by_name(everything(tmp_path, fake))["quoth/sidecar"]
    assert row["status"] == "FAIL" and f"evidence={token}" in row["detail"]


def test_an_unavailable_reranker_is_warn(tmp_path, fake, heartbeat):
    fake.body["reranker"] = {"status": "unavailable", "attempts": 2, "retry_in_s": 280.0}
    rows = everything(tmp_path, fake)
    row = rows_by_name(rows)["quoth/sidecar"]
    assert row["status"] == "WARN" and "reranker unavailable (attempt 2, retry in 280.0 s" in row["detail"]
    assert health.exit_code(rows) == 0
    fake.body["reranker"] = {"status": "ready", "attempts": 1, "retry_in_s": None}
    assert rows_by_name(everything(tmp_path, fake))["quoth/sidecar"]["status"] == "PASS"


def test_review_queue_backlog_is_warn(tmp_path, fake, heartbeat):
    fake.body["ledger"].update(status="red", reasons=["pending_queue_old"], pending=50)
    rows = everything(tmp_path, fake)
    row = rows_by_name(rows)["quoth/review-queue"]
    assert row["status"] == "WARN" and row["detail"].startswith("pending_queue_old: 50 pending")
    assert health.exit_code(rows) == 0
    assert health.exit_code(rows, fail_on_warn=True) == 3


def test_review_queue_without_ledger_block_is_warn(tmp_path, fake, heartbeat):
    del fake.body["ledger"]
    assert rows_by_name(everything(tmp_path, fake))["quoth/review-queue"]["status"] == "WARN"


@pytest.mark.parametrize("key, name", [("episodes", "quoth/episodes"),
                                       ("owner_episodes", "quoth/owner-episodes")])
@pytest.mark.parametrize("lane", [None, {"status": "disabled"}])
def test_lane_off_is_skipped_not_failed(tmp_path, fake, heartbeat, key, name, lane):
    if lane is None:
        del fake.body[key]
    else:
        fake.body[key] = lane
    row = rows_by_name(everything(tmp_path, fake))[name]
    assert row["status"] == "SKIP" and "off" in row["detail"]


@pytest.mark.parametrize("key, name", [("episodes", "quoth/episodes"),
                                       ("owner_episodes", "quoth/owner-episodes")])
@pytest.mark.parametrize("lane, why", [
    ({"status": "red", "reasons": ["stale"]}, "stale"),
    ({"status": "red", "reasons": ["never_ticked"]}, "never_ticked"),
    ({"status": "red", "reasons": ["error:OperationalError"]}, "error:OperationalError"),
])
def test_lanes_red(tmp_path, fake, heartbeat, key, name, lane, why):
    fake.body[key] = lane
    row = rows_by_name(everything(tmp_path, fake))[name]
    assert row["status"] == "FAIL" and why in row["detail"]


# ---------------------------------------------------------------- venv row

def test_venv_missing_is_fail(tmp_path):
    row = health.check_venv(tmp_path / "quoth-current")
    assert row["status"] == "FAIL" and "missing" in row["detail"]


def test_venv_dangling_interpreter_is_fail(tmp_path):
    venv = tmp_path / "quoth-0.0.0"
    (venv / "bin").mkdir(parents=True)
    os.symlink("/nonexistent/python3.13", venv / "bin" / "python")
    current = tmp_path / "quoth-current"
    os.symlink(venv.name, current)
    row = health.check_venv(current)
    assert row["status"] == "FAIL" and "dangling" in row["detail"]


def test_dangling_current_symlink_is_fail(tmp_path):
    os.symlink("quoth-9.9.9", tmp_path / "quoth-current")
    assert health.check_venv(tmp_path / "quoth-current")["status"] == "FAIL"


def test_venv_that_cannot_import_is_fail(tmp_path):
    venv = tmp_path / "v"
    (venv / "bin").mkdir(parents=True)
    fake_python = venv / "bin" / "python"
    fake_python.write_text("#!/bin/sh\nexit 3\n")
    fake_python.chmod(0o755)
    row = health.check_venv(venv)
    assert row["status"] == "FAIL" and "cannot import" in row["detail"]


def test_sidecar_on_another_version_warns(tmp_path, fake, heartbeat):
    fake.body["version"] = "0.0.1"
    row = rows_by_name(everything(tmp_path, fake, venv=Path(sys.prefix)))["quoth/venv"]
    assert row["status"] == "WARN" and "restart" in row["detail"]


# ---------------------------------------------------------------- CLI

def test_cli_json_and_text(tmp_path, fake, heartbeat, capsys):
    heartbeat(checked_at=stamp(datetime.now(UTC)))
    args = ["health", "--state-dir", str(tmp_path), "--socket", str(fake.path), "--no-venv"]
    assert cli.main(args + ["--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["row"] for r in rows] == list(health.ROWS[1:])
    assert cli.main(args) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("PASS quoth/heartbeat: fresh as of")
    fake.body["quote_verification"] = False
    assert cli.main(args) == 1
    assert "FAIL quoth/sidecar:" in capsys.readouterr().out


def test_no_memory_content_in_output(tmp_path, fake, heartbeat, capsys):
    """Even if the sidecar put text where counts belong, none of it is echoed."""
    fake.body["ledger"].update(status="red", reasons=[SENTINEL + " text"], pending=SENTINEL)
    fake.body["episodes"] = {"status": "red", "reasons": [f"{SENTINEL} said"]}
    fake.body["version"] = SENTINEL + " v"
    cli.main(["health", "--state-dir", str(tmp_path), "--socket", str(fake.path), "--no-venv", "--json"])
    assert SENTINEL not in capsys.readouterr().out


# ---------------------------------------------------------------- ledger nudge

def test_nudge_prints_one_line_or_nothing(tmp_path, capsys):
    path = tmp_path / "ledger.db"
    conn = ledger.open_ledger(path)
    assert ledger_cli.main(["--ledger", str(path), "nudge"]) == 0
    assert capsys.readouterr().out == ""
    ledger.propose(conn, text=SENTINEL, kind="preference", origin="assistant_proposal", proposer="assistant")
    ledger.propose(conn, text="Another synthetic claim.", kind="preference", origin="assistant_proposal",
                   proposer="assistant")
    assert cli.main(["ledger", "--ledger", str(path), "nudge"]) == 0
    out = capsys.readouterr().out
    assert out == ('Memory review: 2 claims waiting (oldest 0 d). Say "review memory" when you have ten '
                   'minutes; I show 10 at a time.\n')
    assert SENTINEL not in out
    conn.close()


def test_nudge_matches_the_old_script_rounding():
    assert ledger_cli.nudge_line({"pending": 0}) == ""
    assert ledger_cli.nudge_line({"pending": None}) == ""
    assert "5 claims waiting (oldest 13 d)" in ledger_cli.nudge_line(
        {"pending": 5, "oldest_pending_age_days": 12.6})


def test_nudge_never_creates_or_migrates_a_ledger(tmp_path, capsys):
    path = tmp_path / "absent" / "ledger.db"
    assert ledger_cli.main(["--ledger", str(path), "nudge"]) == 1
    assert not path.exists() and not path.parent.exists()
    assert "ledger_unavailable" in capsys.readouterr().err
