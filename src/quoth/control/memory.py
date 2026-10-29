"""Read-only memory views for the WebUI extension: ledger, review queue, core, recall,
episodes and refresh history.

Two sources, one owner each, so the WebUI and the assistant never disagree:

- The ledger (claims, events, presort suggestions, core versions) is opened per request
  with a ``mode=ro`` URI and ``PRAGMA query_only``. The ledger is WAL-mode and live; a
  WAL reader never blocks the sidecar's writer. The views call the same
  ``ledger.core``/``ledger.compile`` read functions the sidecar's ``/quoth`` command uses.
- Recall and episode search go to the running Quoth sidecar over its Unix socket
  (``POST /v1/prefetch`` with ``dry_run``, ``POST /v1/episodes``). Meaning-based claim
  recall needs the sidecar's in-memory claim index and its bge-m3 query embedder, so a
  second copy here could drift from what the assistant is actually given. ``dry_run`` never marks
  claims used.

The deployed configuration is **socket mode**: no ``ledger_path``, only the sidecar socket. The
ledger views then ask the sidecar (``POST /v1/ledger-view``), which runs these same read
functions over its own ``mode=ro`` connection. A read-only bind mount of a WAL ledger can
refuse readers (they need the ``-shm`` file writable), so the control container never
mounts the ledger. Direct reads (``ledger_path`` set) remain for development.

Refresh history reads nightly receipts and ``heartbeat.json`` through an allowlist:
outcomes, reasons, timings and counts only, never paths or note text. Nothing here
writes: no ledger write, no file write, no decision route.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from ..ledger import core as ledger
from ..ledger.compile import compile_core, published_core

AUTHORITY_FILTER = ("owner", "assistant", "imported")
STATUS_FILTER = ("pending", "accepted", "rejected", "expired", "current")
KIND_FILTER = (*ledger.KINDS, "unclassified")
QUEUE_GROUPS = ("accept", "duplicate", "covered", "reject", "stale", "keep", "none")
MAX_PAGE = 100
QUEUE_MAX = 300
CHAIN_MAX = 50
CORE_VERSIONS_MAX = 100
MAX_FILE = 1024 * 1024
MAX_SIDECAR_RESPONSE = 2 * 1024 * 1024
RECEIPTS_MAX = 60
RECEIPT_SCAN_MAX = 400
OUTCOMES = frozenset(("SWITCH", "SKIP", "HOLD", "INCOMPLETE", "FAIL", "BUSY"))
GATE_MODES = frozenset(("content", "code"))
TOKEN = re.compile(r"^[A-Za-z0-9_.:@=+-]{1,96}$")
# Views the sidecar serves over its socket for a control process without ledger access.
LEDGER_VIEWS = ("claims", "claim", "queue", "core", "core_version")


class MemoryViewError(Exception):
    """A refused memory-view request; `code` is stable and carries no content."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


def connect_ro(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite file read-only: `mode=ro` URI plus `query_only`.

    Never `immutable=1`: the ledger is live and WAL readers must see new commits.
    """
    uri = "file:" + quote(str(Path(path).resolve())) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 2000")
    return conn


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def _iso(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _tokens(values) -> list[str]:
    return [v for v in values if isinstance(v, str) and TOKEN.fullmatch(v)] if isinstance(values, list) else []


def _number(value) -> float | None:
    return round(float(value), 3) if type(value) in (int, float) and value >= 0 else None


def _row(c: dict) -> dict:
    """One table row: no quote, no reason text; the detail view has the rest."""
    return {k: c.get(k) for k in ("id", "text", "kind", "origin", "authority", "review_status", "current",
                                  "recorded_at", "quote_verified", "supersedes", "superseded_by",
                                  "valid_to", "use_count", "auto_accepted")}


def _digest_positions(rows: list[dict]) -> dict[int, int]:
    """claim id -> its 1-based place in its topic, in the order `digest` numbers a round."""
    topics: dict[str, list[dict]] = {}
    for r in rows:  # oldest first, as `digest` reads them
        topics.setdefault(r["topic"], []).append(r)
    positions = {}
    for members in topics.values():
        members.sort(key=lambda r: ledger.ITEM_ORDER.index(r["action"]))
        positions.update({r["id"]: n for n, r in enumerate(members, 1)})
    return positions


def _duplicate_target(conn, claim_id: int) -> dict | None:
    """The claim a duplicate points at, and whether it would stay (`kept_target`)."""
    row = ledger.get(conn, claim_id)
    if row is None:
        return None
    kept = ledger.kept_target(conn, claim_id)
    return {"id": row["id"], "text": row["text"][:200], "review_status": row["review_status"],
            "kept": kept is not None, "kept_id": kept["id"] if kept else None}


class MemoryViews:
    """Configured read-only sources; any of them may be absent (the view says so)."""

    def __init__(self, *, ledger_path: Path | None = None, core_dir: Path | None = None,
                 core_budget: int = 4000, sidecar_socket: Path | None = None,
                 nightly_receipts: Path | None = None, heartbeat: Path | None = None,
                 sidecar_timeout: float = 10.0):
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.core_dir = Path(core_dir) if core_dir else None
        self.core_budget = core_budget
        self.sidecar_socket = Path(sidecar_socket) if sidecar_socket else None
        self.nightly_receipts = Path(nightly_receipts) if nightly_receipts else None
        if heartbeat is None and self.nightly_receipts is not None:
            heartbeat = self.nightly_receipts.parent / "heartbeat.json"
        self.heartbeat = Path(heartbeat) if heartbeat else None
        self.sidecar_timeout = sidecar_timeout
        # One recall/episode call at a time: the sidecar has a single evidence worker
        # shared with the assistant's turns, so the WebUI must not queue work behind it.
        self.sidecar_slot = threading.BoundedSemaphore(1)

    # ------------------------------------------------------------ plumbing

    def _remote(self) -> bool:
        """Socket mode: no ledger file configured, the sidecar serves the ledger views."""
        return self.ledger_path is None and self.sidecar_socket is not None

    def sources(self) -> dict:
        return {"ledger": self.ledger_path is not None or self._remote(),
                "ledger_via": "file" if self.ledger_path is not None else ("sidecar" if self._remote() else None),
                "core_dir": self.core_dir is not None or self._remote(),
                "sidecar": self.sidecar_socket is not None,
                "nightly_receipts": self.nightly_receipts is not None,
                "heartbeat": self.heartbeat is not None}

    def _via_sidecar(self, view: str, **args) -> dict:
        out = self._sidecar("POST", "/v1/ledger-view", {"view": view, **args}, single_flight=False)
        if not isinstance(out.get("view"), dict):
            raise MemoryViewError(502, "sidecar_invalid_response")
        return out["view"]

    def _ledger(self) -> sqlite3.Connection:
        if self.ledger_path is None:
            raise MemoryViewError(503, "ledger_unconfigured")
        if not self.ledger_path.is_file():
            raise MemoryViewError(503, "ledger_unavailable")
        try:
            conn = connect_ro(self.ledger_path)
            conn.execute("SELECT 1 FROM claims LIMIT 1").fetchall()
        except sqlite3.Error:
            raise MemoryViewError(503, "ledger_unavailable") from None
        return conn

    def _sidecar(self, method: str, route: str, body: dict | None = None, *,
                 single_flight: bool = True, allow_unhealthy: bool = False) -> dict:
        if self.sidecar_socket is None:
            raise MemoryViewError(503, "sidecar_unconfigured")
        # Ledger views are cheap SQLite reads; only recall/episodes share the evidence worker.
        slot = self.sidecar_slot if single_flight else threading.BoundedSemaphore(1)
        if not slot.acquire(blocking=False):
            raise MemoryViewError(429, "busy")
        try:
            conn = _UnixConnection(str(self.sidecar_socket), self.sidecar_timeout)
            try:
                payload = None if body is None else json.dumps(body).encode()
                conn.request(method, route, body=payload,
                             headers={"Content-Type": "application/json"} if payload else {})
                response = conn.getresponse()
                raw = response.read(MAX_SIDECAR_RESPONSE + 1)
                status = response.status
            finally:
                conn.close()
        except (OSError, http.client.HTTPException):
            raise MemoryViewError(503, "sidecar_unavailable") from None
        finally:
            slot.release()
        if len(raw) > MAX_SIDECAR_RESPONSE:
            raise MemoryViewError(502, "sidecar_response_too_large")
        try:
            data = json.loads(raw)
        except ValueError:
            raise MemoryViewError(502, "sidecar_invalid_response") from None
        if not isinstance(data, dict):
            raise MemoryViewError(502, "sidecar_invalid_response")
        if allow_unhealthy and status == 503 and data.get("schema") == "quoth-sidecar/v1":
            return data  # a red sidecar still reports its full health
        if status != 200 or not data.get("ok"):
            code = data.get("error") if isinstance(data.get("error"), str) and TOKEN.fullmatch(data["error"]) \
                else "sidecar_error"
            if code == "not_found":
                raise MemoryViewError(503, "sidecar_outdated")  # a sidecar older than this route
            raise MemoryViewError(503 if status >= 500 else (status if status in (404, 429) else 400), code)
        return data

    # ------------------------------------------------------------ ledger views

    def claims(self, body: dict) -> dict:
        if self._remote():
            return self._via_sidecar("claims", filters=body)
        status, kind, authority = body.get("status"), body.get("kind"), body.get("authority")
        q = body.get("q") or ""
        offset, limit = body.get("offset", 0), body.get("limit", 50)
        if status not in (None, "", *STATUS_FILTER) or kind not in (None, "", *KIND_FILTER) \
                or authority not in (None, "", *AUTHORITY_FILTER) or not isinstance(q, str) or len(q) > 200 \
                or type(offset) is not int or not 0 <= offset <= 1_000_000 \
                or type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise MemoryViewError(400, "invalid_filter")
        where, args = [], []
        if status == "current":
            where.append(ledger._CURRENT)
        elif status:
            where.append("review_status = ?")
            args.append(status)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if authority:
            origins = [o for o, a in ledger.AUTHORITY.items() if a == authority]
            where.append(f"origin IN ({','.join('?' * len(origins))})")
            args.extend(origins)
        sql = "SELECT * FROM claims" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC"
        conn = self._ledger()
        try:
            rows = conn.execute(sql, args).fetchall()
            if q.strip():
                rows = [r for r in rows if ledger.text_matches(r["text"], q.strip())]
            page = [ledger._view(r) for r in rows[offset:offset + limit]]
            items = []
            for c in page:
                tip = ledger.current_suggestion(conn, c["id"]) if c["review_status"] == "pending" else None
                items.append({**_row(c), "suggestion": tip["action"] if tip else None})
            totals = {s: n for s, n in conn.execute(
                "SELECT review_status, count(*) FROM claims GROUP BY 1").fetchall()}
        finally:
            conn.close()
        return {"total": len(rows), "offset": offset, "limit": limit, "items": items,
                "ledger_totals": totals,
                "filters": {"status": STATUS_FILTER, "kind": KIND_FILTER, "authority": AUTHORITY_FILTER}}

    def claim(self, claim_id: int) -> dict:
        if self._remote():
            return self._via_sidecar("claim", id=claim_id)
        conn = self._ledger()
        try:
            c = ledger.get(conn, claim_id)
            if c is None:
                raise MemoryViewError(404, "claim_unknown")
            events = ledger.claim_events(conn, claim_id, detail=True)
            tip = ledger.current_suggestion(conn, claim_id)
            chain = self._chain(conn, c)
        finally:
            conn.close()
        return {"claim": c, "suggestion": tip, "events": events, "chain": chain}

    @staticmethod
    def _chain(conn, c: dict) -> list[dict]:
        """Every claim linked by supersedes/superseded_by, oldest first (bounded, cycle-safe)."""
        def brief(row):
            return {"id": row["id"], "text": row["text"][:160], "kind": row["kind"],
                    "review_status": row["review_status"], "current": row["current"]}
        back, seen, step = [], {c["id"]}, c
        while step.get("supersedes") and step["supersedes"] not in seen and len(back) < CHAIN_MAX:
            step = ledger.get(conn, step["supersedes"])
            if step is None:
                break
            seen.add(step["id"])
            back.append(brief(step))
        forward, step = [], c
        while step.get("superseded_by") and step["superseded_by"] not in seen and len(forward) < CHAIN_MAX:
            step = ledger.get(conn, step["superseded_by"])
            if step is None:
                break
            seen.add(step["id"])
            forward.append(brief(step))
        if not back and not forward:
            return []
        return [*reversed(back), {**brief(c), "self": True}, *forward]

    def queue(self) -> dict:
        if self._remote():
            return self._via_sidecar("queue")
        conn = self._ledger()
        try:
            queue = ledger.review_queue(conn, limit=QUEUE_MAX + 1)
            health = ledger.health(conn)
            summary = ledger.presort_summary(conn)
            # The guided-review plan and topics, exactly as the assistant's `digest` gives them in chat.
            plan = ledger.digest(conn, limit=1)
            positions = _digest_positions(ledger._pending_with_tips(conn))
            groups = {g: [] for g in QUEUE_GROUPS}
            for c in queue[:QUEUE_MAX]:
                tip = c["suggestion"]
                item = {**_row(c), "suggestion": tip, "position": positions.get(c["id"])}
                if tip and tip["action"] == "duplicate" and tip.get("duplicate_of"):
                    item["duplicate_target"] = _duplicate_target(conn, tip["duplicate_of"])
                groups[tip["action"] if tip else "none"].append(item)
        finally:
            conn.close()
        return {"pending": health["pending"], "oldest_pending_age_days": health["oldest_pending_age_days"],
                "status": health["status"], "reasons": health["reasons"],
                "counts": {g: summary.get(g, 0) for g in QUEUE_GROUPS}, "groups": groups,
                "truncated": len(queue) > QUEUE_MAX,
                "plan": plan["plan"], "topics": plan["topics"],
                "buckets": {b: v["count"] for b, v in plan["buckets"].items()}}

    def core(self) -> dict:
        if self._remote():
            return self._via_sidecar("core")
        published = None
        if self.core_dir is not None:
            try:
                target = (self.core_dir / "core.md")
                if target.exists() and target.resolve().stat().st_size > MAX_FILE:
                    raise MemoryViewError(503, "core_too_large")
                published = published_core(self.core_dir)
            except (OSError, UnicodeError):
                raise MemoryViewError(503, "core_unavailable") from None
        live, versions = None, []
        if self.ledger_path is not None:
            conn = self._ledger()
            try:
                live = compile_core(conn, self.core_budget)
                for r in conn.execute("SELECT id, sha256, length(content), claim_ids, budget, compiled_at"
                                      " FROM core_versions ORDER BY id DESC LIMIT ?", (CORE_VERSIONS_MAX,)):
                    try:
                        count = len(json.loads(r[3]))
                    except (ValueError, TypeError):
                        count = None
                    versions.append({"id": r[0], "sha256": r[1], "chars": r[2], "claims": count,
                                     "budget": r[4], "compiled_at": r[5]})
            finally:
                conn.close()
        if published is None and live is None:
            raise MemoryViewError(503, "core_unconfigured")
        shown = published or {"source": "live", "file": None, "text": live["text"]}
        return {**shown, "live_differs": None if published is None or live is None
                else published["text"] != live["text"],
                "live": None if live is None else {k: live[k] for k in ("claim_ids", "dropped_ids", "chars",
                                                                       "limit", "budget")},
                "versions": versions}

    def core_version(self, version_id: int) -> dict:
        if self._remote():
            return self._via_sidecar("core_version", id=version_id)
        conn = self._ledger()
        try:
            row = conn.execute("SELECT id, sha256, content, claim_ids, budget, compiled_at FROM core_versions"
                               " WHERE id = ?", (version_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise MemoryViewError(404, "core_version_unknown")
        return {"id": row[0], "sha256": row[1], "text": row[2], "budget": row[4], "compiled_at": row[5]}

    def ledger_view(self, body: dict) -> dict:
        """Sidecar side of socket mode: one named read-only view, nothing else."""
        view = body.get("view")
        if view not in LEDGER_VIEWS:
            raise MemoryViewError(400, "unknown_view")
        if view in ("claim", "core_version"):
            ident = body.get("id")
            if type(ident) is not int or not 1 <= ident <= 999_999_999_999:
                raise MemoryViewError(400, "invalid_id")
            return self.claim(ident) if view == "claim" else self.core_version(ident)
        if view == "claims":
            filters = body.get("filters", {})
            if not isinstance(filters, dict):
                raise MemoryViewError(400, "invalid_filter")
            return self.claims(filters)
        return self.queue() if view == "queue" else self.core()

    # ------------------------------------------------------------ sidecar views

    def recall(self, body: dict) -> dict:
        text = body.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2048:
            raise MemoryViewError(400, "invalid_text")
        out = self._sidecar("POST", "/v1/prefetch", {"query": text, "dry_run": True})
        detail = out.get("detail") or {}
        claims = [{**{k: c.get(k) for k in ("id", "kind", "authority", "text", "cosine")},
                   "match": "cosine" if c.get("cosine") is not None else "words"}
                  for c in detail.get("claims", []) if isinstance(c, dict)]
        return {"text": out.get("text", ""), "claims": claims,
                "evidence": [{"locator": e.get("locator"), "note_date": e.get("note_date")}
                             for e in detail.get("evidence", []) if isinstance(e, dict)],
                "evidence_status": out.get("evidence_status"),
                "episodes": [{"locator": e.get("locator"), "timestamp": e.get("timestamp"),
                              "score": e.get("score")} for e in detail.get("episodes", []) if isinstance(e, dict)],
                "episodes_status": out.get("episodes_status", "disabled")}

    def episodes(self, body: dict) -> dict:
        query, k = body.get("query"), body.get("k", 10)
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2048 or type(k) is not int \
                or not 1 <= k <= 20:
            raise MemoryViewError(400, "invalid_query")
        out = self._sidecar("POST", "/v1/episodes", {"query": query, "k": k})
        return {"answer_use": out.get("answer_use"),
                "hits": [{key: h.get(key) for key in ("locator", "timestamp", "score", "text")}
                         for h in out.get("hits", []) if isinstance(h, dict)]}

    def sidecar_health(self) -> dict:
        out = self._sidecar("GET", "/v1/health", allow_unhealthy=True)
        return {k: out.get(k) for k in ("version", "status", "ledger", "evidence", "quote_verification",
                                        "episodes", "owner_episodes")}

    # ------------------------------------------------------------ refresh history

    def refresh(self) -> dict:
        if self.nightly_receipts is None and self.heartbeat is None:
            raise MemoryViewError(503, "receipts_unconfigured")
        receipts = []
        if self.nightly_receipts is not None and self.nightly_receipts.is_dir():
            paths = sorted((p for p in self.nightly_receipts.glob("*.json") if TOKEN.fullmatch(p.stem)),
                           key=lambda p: p.name, reverse=True)[:RECEIPT_SCAN_MAX]
            for path in paths:
                projected = _nightly_receipt(path)
                if projected is not None:
                    receipts.append(projected)
            receipts.sort(key=lambda r: (r["recorded_at"], r["receipt_id"]), reverse=True)
        return {"receipts": receipts[:RECEIPTS_MAX],
                "receipts_state": "unconfigured" if self.nightly_receipts is None else
                ("ok" if self.nightly_receipts.is_dir() else "unavailable"),
                "heartbeat": _heartbeat(self.heartbeat) if self.heartbeat is not None else None}


def _read_json(path: Path):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
            return None
        return json.loads(path.read_bytes())
    except (OSError, ValueError, UnicodeError):
        return None


def _nightly_receipt(path: Path) -> dict | None:
    """Allowlisted projection of one `quoth-nightly/v1` receipt: no paths, ids of notes or text."""
    data = _read_json(path)
    if not isinstance(data, dict) or data.get("schema") != "quoth-nightly/v1":
        return None
    recorded = _iso(data.get("recorded_at"))
    if recorded is None or data.get("outcome") not in OUTCOMES:
        return None
    rid = data.get("receipt_id")
    seconds = data.get("seconds") if isinstance(data.get("seconds"), dict) else {}
    seconds = {k: _number(v) for k, v in seconds.items() if isinstance(k, str) and TOKEN.fullmatch(k)
               and _number(v) is not None}
    drift = data.get("gold_drift")
    drift = ({k: drift[k] for k in ("renamed", "stale")}
             if isinstance(drift, dict) and all(type(drift.get(k)) is int and drift[k] >= 0
                                                for k in ("renamed", "stale")) else None)
    return {"receipt_id": rid if isinstance(rid, str) and TOKEN.fullmatch(rid) else path.stem,
            "recorded_at": data["recorded_at"], "outcome": data["outcome"],
            "gate_mode": data.get("gate_mode") if data.get("gate_mode") in GATE_MODES else None,
            "seconds": seconds, "total_seconds": round(sum(seconds.values()), 1) if seconds else None,
            "reasons": _tokens(data.get("reasons")), "gold_drift": drift,
            "rollback": data.get("rollback") if isinstance(data.get("rollback"), str)
            and TOKEN.fullmatch(data["rollback"]) else None}


def _heartbeat(path: Path) -> dict:
    """Allowlisted `quoth-heartbeat/v1`: status, reasons, ages; never the serving DB path."""
    data = _read_json(path)
    if data is None:
        return {"state": "unavailable"}
    if not isinstance(data, dict) or data.get("schema") != "quoth-heartbeat/v1" \
            or data.get("status") not in ("green", "red"):
        return {"state": "invalid"}
    serving = data.get("serving") if isinstance(data.get("serving"), dict) else None
    last = data.get("last_run") if isinstance(data.get("last_run"), dict) else None

    def stamp(value):
        return value if isinstance(value, str) and _iso(value) is not None else None

    return {"state": "ok", "status": data["status"], "reasons": _tokens(data.get("reasons")),
            "checked_at": stamp(data.get("checked_at")), "fresh_as_of": stamp(data.get("fresh_as_of")),
            "fresh_age_hours": _number(data.get("fresh_age_hours")),
            "max_fresh_age_hours": _number(data.get("max_fresh_age_hours")),
            "max_run_age_hours": _number(data.get("max_run_age_hours")),
            "serving": None if serving is None else {"frozen_at": stamp(serving.get("frozen_at")),
                                                     "age_hours": _number(serving.get("age_hours"))},
            "last_run": None if last is None else {
                "receipt_id": last.get("receipt_id") if isinstance(last.get("receipt_id"), str)
                and TOKEN.fullmatch(last["receipt_id"]) else None,
                "outcome": last.get("outcome") if last.get("outcome") in OUTCOMES else None,
                "reasons": _tokens(last.get("reasons")), "recorded_at": stamp(last.get("recorded_at")),
                "age_hours": _number(last.get("age_hours"))}}
