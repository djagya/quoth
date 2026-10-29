"""`quoth health`: every memory row a host monitor checks, in one call.

Rows (each PASS, WARN or FAIL):

- ``quoth/venv``           the install's interpreter resolves and runs
- ``quoth/heartbeat``      the nightly refresh heartbeat (``quoth-heartbeat/v1``)
- ``quoth/sidecar``        ``/v1/health`` answers on the socket, evidence ok (the pointer's target
                           admitted), quotes verifiable; WARN while a configured reranker is unavailable
- ``quoth/review-queue``   pending review backlog (WARN, never FAIL: a backlog is not an outage)
- ``quoth/episodes``       the assistant lane's background tick
- ``quoth/owner-episodes`` the owner lane's background tick

A missing heartbeat or socket is FAIL, never PASS. When the sidecar does not answer, the
rows that depend on it are FAIL too, so the row set is the same on every run. The sidecar
answers 503 when it cannot open its snapshot; its body is still read row by row.

Output is counts, versions, timestamps and status tokens only: no claim text, query,
note path or message. Text lines are ``STATUS row: detail``; ``--json`` prints a list of
``{row, status, detail}``.

Paths default under ``$QUOTH_HOME`` (``~/.local/share/quoth``): the socket
``quoth.sock`` and the heartbeat ``refresh/heartbeat.json``. The venv checked is the one
running this command unless ``--venv`` names another (e.g. the fixed ``quoth-current``
link of a release install). After an upgrade, a sidecar still running another version than
the venv turns the venv row WARN (restart the sidecar).

Exit status: 0 when no row is FAIL (WARN rows allowed), 1 when any row is FAIL. With
``--fail-on-warn``, a WARN without any FAIL exits 3.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys


from . import paths

HEARTBEAT_SCHEMA = "quoth-heartbeat/v1"
SIDECAR_SCHEMA = "quoth-sidecar/v1"
HEARTBEAT_MAX_AGE_HOURS = 30.0
ROWS = ("quoth/venv", "quoth/heartbeat", "quoth/sidecar", "quoth/review-queue",
        "quoth/episodes", "quoth/owner-episodes")
LANES = (("episodes", "quoth/episodes", "--episodes"),
         ("owner_episodes", "quoth/owner-episodes", "--owner-episodes"))
_TOKEN = re.compile(r"^[A-Za-z0-9_.:@=+-]{1,96}$")
_STAMP = re.compile(r"^[0-9T:.+Z-]{10,40}$")
_VERSION = re.compile(r"^[0-9A-Za-z.+-]{1,40}$")


def _row(status: str, row: str, detail: str) -> dict:
    return {"row": row, "status": status, "detail": detail}


def _reasons(values) -> str:
    """Status tokens only; anything else is counted, never echoed."""
    values = values if isinstance(values, list) else []
    kept = [v for v in values if isinstance(v, str) and _TOKEN.fullmatch(v)]
    other = len(values) - len(kept)
    return ", ".join(kept + ([f"+{other} other"] if other else []))


def _token(value) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _stamp(value) -> str:
    return value if isinstance(value, str) and _STAMP.fullmatch(value) else "?"


def _count(value) -> str:
    return str(value) if type(value) in (int, float) else "?"


def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


# ------------------------------------------------------------------ rows

def check_venv(venv: Path) -> dict:
    """The install's interpreter. A venv links to a system python, so an image or OS change
    can leave it dangling, which stops the sidecar, the nightly and this command."""
    name = "quoth/venv"
    python = Path(venv) / "bin" / "python"
    if not Path(venv).exists():
        return _row("FAIL", name, f"{venv} missing or dangling")
    try:
        target = python.resolve(strict=True)
    except (OSError, RuntimeError):
        return _row("FAIL", name, f"{python} missing or dangling (interpreter gone from this image?)")
    if not os.access(target, os.X_OK):
        return _row("FAIL", name, f"{target} not executable")
    try:
        out = subprocess.run(
            [str(python), "-I", "-c",
             "import sys, quoth; print(quoth.__version__, '%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _row("FAIL", name, f"{python} does not run: {type(exc).__name__}")
    parts = out.stdout.split()
    if out.returncode != 0 or len(parts) != 2 or not all(_VERSION.fullmatch(p) for p in parts):
        return _row("FAIL", name, f"{python} cannot import quoth (exit {out.returncode})")
    return {**_row("PASS", name, f"{Path(venv).resolve().name}: quoth {parts[0]}, python {parts[1]}"),
            "version": parts[0]}


def check_heartbeat(path: Path, *, max_age_hours: float = HEARTBEAT_MAX_AGE_HOURS,
                    now: datetime | None = None) -> dict:
    """Nightly refresh heartbeat; red on HOLD/INCOMPLETE/FAIL, a stale snapshot or no rewrite."""
    name = "quoth/heartbeat"
    now = now or datetime.now(UTC)
    try:
        beat = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _row("FAIL", name, f"cannot read {path}: {type(exc).__name__}")
    if not isinstance(beat, dict) or beat.get("schema") != HEARTBEAT_SCHEMA:
        return _row("FAIL", name, f"not {HEARTBEAT_SCHEMA}")
    checked = _parse(beat.get("checked_at"))
    if checked is None or now - checked > timedelta(hours=max_age_hours):
        return _row("FAIL", name, f"not rewritten since {_stamp(beat.get('checked_at'))} "
                                  "(nightly refresh not running)")
    detail = f"fresh as of {_stamp(beat.get('fresh_as_of'))}"
    if beat.get("status") != "green":
        status = beat.get("status") if isinstance(beat.get("status"), str) and \
            _TOKEN.fullmatch(beat["status"]) else "?"
        return _row("FAIL", name, f"{status}: {_reasons(beat.get('reasons')) or 'no reasons'}; {detail}")
    return _row("PASS", name, detail)


def sidecar_health(sock_path: Path, timeout: float = 5.0) -> tuple[int, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(sock_path))
        conn.sendall(b"GET /v1/health HTTP/1.0\r\nHost: quoth\r\n\r\n")
        raw = b""
        while chunk := conn.recv(65536):
            raw += chunk
            if len(raw) > 1024 * 1024:
                raise ValueError("response too large")
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(None, 2)[1]), json.loads(body or b"null")


def check_sidecar(sock_path: Path, *, timeout: float = 5.0) -> list[dict]:
    """Sidecar, review queue and both episodic lanes, from one ``/v1/health`` call."""
    try:
        code, health = sidecar_health(sock_path, timeout)
    except (OSError, ValueError, IndexError) as exc:
        why = f"no answer on {sock_path}: {type(exc).__name__}"
        return [_row("FAIL", "quoth/sidecar", why)] + \
            [_row("FAIL", row, "sidecar not answering") for row in ROWS[3:]]
    # 503 is the sidecar saying it cannot serve evidence: the body still has every part.
    if code not in (200, 503) or not isinstance(health, dict) or health.get("schema") != SIDECAR_SCHEMA:
        return [_row("FAIL", "quoth/sidecar", f"/v1/health returned {code}")] + \
            [_row("FAIL", row, "sidecar not answering") for row in ROWS[3:]]
    rows = []
    version = health.get("version") if isinstance(health.get("version"), str) and \
        _VERSION.fullmatch(health["version"]) else "?"
    block = health.get("evidence") if isinstance(health.get("evidence"), dict) else {}
    evidence = _token(block.get("status"))
    rejected = block.get("rejected") if isinstance(block.get("rejected"), dict) else None
    reranker = health.get("reranker") if isinstance(health.get("reranker"), dict) else None
    rerank_state = _token(reranker.get("status")) if reranker is not None else None
    if evidence != "ok" or not health.get("quote_verification"):
        detail = f"evidence={evidence}"
        if rejected is not None:  # why the pointer's target is not served: reason class + digest only
            detail += (f" (pointer {_token(rejected.get('reason'))}, target {_token(rejected.get('target_digest'))},"
                       f" since {_stamp(rejected.get('since'))})")
        rows.append(_row("FAIL", "quoth/sidecar",
                         f"{detail} quote_verification={bool(health.get('quote_verification'))}"))
    elif rerank_state == "unavailable":
        rows.append(_row("WARN", "quoth/sidecar",
                         f"v{version} evidence ok; reranker unavailable (attempt {_count(reranker.get('attempts'))},"
                         f" retry in {_count(reranker.get('retry_in_s'))} s; turns keep the cosine gate)"))
    else:
        rows.append(_row("PASS", "quoth/sidecar", f"v{version} evidence ok"))
    rows[0]["version"] = version if version != "?" else None
    ledger = health.get("ledger") if isinstance(health.get("ledger"), dict) else {}
    detail = (f"{_count(ledger.get('pending'))} pending, oldest {_count(ledger.get('oldest_pending_age_days'))} d, "
              f"{_count(ledger.get('accepted_current'))} accepted")
    if ledger.get("status") == "green":
        rows.append(_row("PASS", "quoth/review-queue", detail))
    else:
        rows.append(_row("WARN", "quoth/review-queue",
                         f"{_reasons(ledger.get('reasons')) or 'no ledger status'}: {detail}"))
    for key, name, flag in LANES:
        lane = health.get(key) if isinstance(health.get(key), dict) else {"status": "disabled"}
        if lane.get("status") == "green":
            rows.append(_row("PASS", name, f"{_count(lane.get('messages'))} messages, "
                                           f"{_count(lane.get('pending'))} to embed, "
                                           f"last tick {_stamp(lane.get('last_tick_at'))}"))
        elif lane.get("status") == "disabled":
            rows.append(_row("SKIP", name, f"off (sidecar started without {flag})"))
        else:
            rows.append(_row("FAIL", name, _reasons(lane.get("reasons")) or "red"))
    return rows


def run(*, state_dir: Path | None = None, heartbeat: Path | None = None,
        socket_path: Path | None = None, venv: Path | None = None,
        max_age_hours: float = HEARTBEAT_MAX_AGE_HOURS, timeout: float = 5.0,
        now: datetime | None = None) -> list[dict]:
    state_dir = Path(state_dir) if state_dir is not None else paths.home()
    rows = []
    if venv is not None:
        rows.append(check_venv(Path(venv)))
    rows.append(check_heartbeat(heartbeat or state_dir / "refresh" / "heartbeat.json",
                                max_age_hours=max_age_hours, now=now))
    rows.extend(check_sidecar(socket_path or state_dir / "quoth.sock", timeout=timeout))
    # After an upgrade the fixed path moves before the sidecar restarts: say so (WARN).
    installed = next((r.get("version") for r in rows if r["row"] == "quoth/venv"), None)
    serving = next((r.get("version") for r in rows if r["row"] == "quoth/sidecar"), None)
    if installed and serving and installed != serving:
        venv_row = next(r for r in rows if r["row"] == "quoth/venv")
        venv_row.update(status="WARN", detail=venv_row["detail"] + f"; sidecar still runs {serving}, restart it")
    for r in rows:
        r.pop("version", None)
    return rows


def exit_code(rows: list[dict], *, fail_on_warn: bool = False) -> int:
    if any(r["status"] == "FAIL" for r in rows):
        return 1
    if fail_on_warn and any(r["status"] == "WARN" for r in rows):
        return 3
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="quoth health", description=__doc__.split("\n\n")[0],
                                epilog="Exit 0: no FAIL row (WARN allowed). 1: any FAIL. "
                                       "2: usage error. 3: WARN only, with --fail-on-warn.")
    p.add_argument("--state-dir", type=Path, default=None,
                   help="quoth's state directory, heartbeat and socket default under it "
                        "(default $QUOTH_HOME)")
    p.add_argument("--heartbeat", type=Path, help="default <state-dir>/refresh/heartbeat.json")
    p.add_argument("--socket", type=Path, help="sidecar socket, default <state-dir>/quoth.sock")
    p.add_argument("--venv", type=Path, default=None,
                   help="install whose interpreter is checked (default: the one running this command)")
    p.add_argument("--no-venv", action="store_true", help="skip the quoth/venv row")
    p.add_argument("--max-age-hours", type=float, default=HEARTBEAT_MAX_AGE_HOURS,
                   help="heartbeat must be rewritten within this many hours")
    p.add_argument("--timeout", type=float, default=5.0, help="socket timeout, seconds")
    p.add_argument("--fail-on-warn", action="store_true", help="exit 3 when a row is WARN and none FAIL")
    p.add_argument("--json", action="store_true", help="print a JSON list of {row, status, detail}")
    a = p.parse_args(argv)
    rows = run(state_dir=a.state_dir, heartbeat=a.heartbeat, socket_path=a.socket,
               venv=None if a.no_venv else (a.venv or Path(sys.prefix)), max_age_hours=a.max_age_hours, timeout=a.timeout)
    if a.json:
        print(json.dumps(rows))
    else:
        for r in rows:
            print(f"{r['status']} {r['row']}: {r['detail']}")
    return exit_code(rows, fail_on_warn=a.fail_on_warn)


if __name__ == "__main__":
    sys.exit(main())
