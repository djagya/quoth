"""Phase 0 freshness: vector reuse, retired-vector pruning, explicit index rebase, heartbeat."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from quoth import heartbeat
from quoth.assets import eval_dir, eval_path
from quoth.db import connect_read_only, open_db
from quoth.embed.store import embed_chunks
from quoth.ingest import ingest_vault
from quoth.refresh import (REBASE_CONTRACT_FILES, code_digests, index_code_digests,
                                 refresh, validate_code_compatibility)

from test_refresh import source


class CountingEmbedder:
    """Deterministic content-dependent vectors; records every encoded passage."""
    spec = SimpleNamespace(name="bge-m3", dim=1024)

    def __init__(self):
        self.encoded = []

    def embed_passages(self, texts):
        self.encoded.extend(texts)
        out = np.zeros((len(texts), 1024), dtype="<f4")
        for i, text in enumerate(texts):
            out[i, hash(text) % 1024] = 1
        return out


LONG = "\n".join(f"Paragraph {i} " + "word " * 60 for i in range(12))


def test_changed_note_reuses_vectors_of_unchanged_chunks(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text(f"# One\n{LONG}\n\n# Two\nSecond section.\n")
    conn = open_db(tmp_path / "m.db")
    ingest_vault(conn, vault)
    first = CountingEmbedder()
    embed_chunks(conn, first)
    before = {r[0]: r[1] for r in conn.execute(
        "SELECT c.heading_path || c.text, e.vector FROM embeddings e JOIN chunks c ON c.id=e.chunk_id")}
    (vault / "a.md").write_text(f"# One\n{LONG}\n\n# Two\nSecond section, edited.\n")
    ingest_vault(conn, vault)
    second = CountingEmbedder()
    stats = embed_chunks(conn, second)
    assert stats.embedded == 1 and stats.reused == len(first.encoded) - 1
    assert len(second.encoded) == 1 and second.encoded[0].endswith("Second section, edited.")
    after = {r[0]: r[1] for r in conn.execute(
        "SELECT c.heading_path || c.text, e.vector FROM embeddings e JOIN chunks c ON c.id=e.chunk_id"
        " JOIN source_versions v ON v.id=c.source_version_id AND v.is_current=1")}
    assert all(after[key] == before[key] for key in after if key in before)
    # Same text under a different path is a different embedder input: no reuse.
    (vault / "b.md").write_text("# Two\nSecond section.\n")
    ingest_vault(conn, vault)
    third = CountingEmbedder()
    assert embed_chunks(conn, third).reused == 0 and len(third.encoded) == 1


def test_reuse_can_be_disabled(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text(f"# One\n{LONG}\n")
    conn = open_db(tmp_path / "m.db")
    ingest_vault(conn, vault)
    embed_chunks(conn, CountingEmbedder())
    (vault / "a.md").write_text(f"# One\n{LONG}\nTail.\n")
    ingest_vault(conn, vault)
    embedder = CountingEmbedder()
    stats = embed_chunks(conn, embedder, reuse=False)
    assert stats.reused == 0 and stats.embedded == len(embedder.encoded) > 1


def test_refresh_prunes_retired_vectors_only(tmp_path):
    src, vault = source(tmp_path)
    (vault / "a.md").write_text("# Alpha\nChanged text.\n")
    (vault / "deleted.md").unlink()
    target = tmp_path / "target.db"
    result = refresh(src, target, vault, embedder=CountingEmbedder())
    assert result["pruned_retired_vectors"] == 2
    with connect_read_only(target) as db:
        retired = db.execute(
            "SELECT count(*) FROM embeddings e JOIN chunks c ON c.id=e.chunk_id"
            " JOIN source_versions v ON v.id=c.source_version_id WHERE v.is_current=0").fetchone()[0]
        kept_chunks = db.execute(
            "SELECT count(*) FROM chunks c JOIN source_versions v ON v.id=c.source_version_id"
            " WHERE v.is_current=0").fetchone()[0]
    assert retired == 0 and kept_chunks == 2  # history stays; only derived vectors go


def _manifest_with(changed: set[str]) -> dict:
    code, index = dict(code_digests()), dict(index_code_digests())
    for name in changed:
        if name in code:
            code[name] = "0" * 64
        if name in index:
            index[name] = "0" * 64
    return {"code_digests": code, "index_code_digests": index}


@pytest.mark.parametrize("name", sorted(REBASE_CONTRACT_FILES))
def test_rebase_allows_only_declared_files(name):
    manifest = _manifest_with({name})
    with pytest.raises(ValueError):
        validate_code_compatibility(manifest, code_digests())
    validate_code_compatibility(manifest, code_digests(), allow_index_contract_rebase=True)


# db.py is rebase-declared in v0.4.0 only (package rename); scopes.toml stays data-shaping.
@pytest.mark.parametrize("name", ["chunker.py", "ingest.py", "embed/backends.py",
                                  "scopes.toml", "entities.py", "migrations/0001_evidence.sql"])
def test_rebase_never_covers_data_shaping_files(name):
    manifest = _manifest_with({name, "embed/store.py"})
    with pytest.raises(ValueError):
        validate_code_compatibility(manifest, code_digests(), allow_index_contract_rebase=True)


def test_eval_assets_resolve_in_checkout_and_are_packaged():
    assert eval_path("models-manifest.json").is_file()
    assert eval_dir().name == "eval"
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert '"eval" = "quoth/_eval"' in pyproject


# ------------------------------------------------------------------ heartbeat

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _receipt(directory, hours_ago, outcome, reasons=()):
    when = NOW - timedelta(hours=hours_ago)
    ident = when.strftime("%Y%m%dT%H%M%SZ") + "-" + outcome.lower()
    (directory / (ident + ".json")).write_text(json.dumps({
        "receipt_id": ident, "outcome": outcome, "reasons": list(reasons),
        "recorded_at": when.isoformat().replace("+00:00", "Z")}))


def _layout(tmp_path):
    db = tmp_path / "staging" / "nightly-x.db"
    db.parent.mkdir()
    db.write_bytes(b"x")
    db.with_suffix(".manifest.json").write_text("{}")
    pointer = tmp_path / "staging" / "serving.db"
    pointer.symlink_to(db)
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    return pointer, receipts


def test_heartbeat_green_after_recent_switch_or_skip(tmp_path):
    pointer, receipts = _layout(tmp_path)
    _receipt(receipts, 30, "SWITCH")
    _receipt(receipts, 6, "SKIP")
    beat = heartbeat.check(pointer, receipts, now=NOW)
    assert beat["status"] == "green" and beat["reasons"] == []
    assert beat["fresh_age_hours"] == 6 and beat["last_run"]["outcome"] == "SKIP"


def test_heartbeat_red_on_todays_stuck_state(tmp_path):
    # A real install's shape: last SWITCH 7 days ago, then HOLD/INCOMPLETE nights.
    pointer, receipts = _layout(tmp_path)
    _receipt(receipts, 7 * 24, "SWITCH")
    _receipt(receipts, 4 * 24, "HOLD", ["synthetic_lost_all_gold"])
    _receipt(receipts, 30, "HOLD", ["private_mrr@10_regressed"])
    _receipt(receipts, 9, "INCOMPLETE", ["internal_deadline"])
    beat = heartbeat.check(pointer, receipts, now=NOW)
    assert beat["status"] == "red"
    assert beat["reasons"] == ["last_run_incomplete", "freshness_unconfirmed"]
    assert beat["fresh_age_hours"] == 168


def test_heartbeat_red_on_age_even_when_last_run_looks_ok(tmp_path):
    pointer, receipts = _layout(tmp_path)
    _receipt(receipts, 40, "SWITCH")
    beat = heartbeat.check(pointer, receipts, now=NOW)
    assert beat["reasons"] == ["last_run_overdue", "freshness_unconfirmed"]


@pytest.mark.parametrize("outcome", ["HOLD", "FAIL", "INCOMPLETE"])
def test_heartbeat_red_on_bad_last_run_even_if_recently_fresh(tmp_path, outcome):
    pointer, receipts = _layout(tmp_path)
    _receipt(receipts, 20, "SWITCH")
    _receipt(receipts, 2, outcome)
    beat = heartbeat.check(pointer, receipts, now=NOW)
    assert beat["reasons"] == ["last_run_" + outcome.lower()]


def test_heartbeat_red_without_receipts_or_pointer(tmp_path):
    beat = heartbeat.check(tmp_path / "missing", tmp_path / "none", now=NOW)
    assert beat["reasons"] == ["serving_pointer_invalid", "no_receipts", "freshness_unconfirmed"]


def test_heartbeat_ignores_unreadable_receipts_and_writes_atomically(tmp_path):
    pointer, receipts = _layout(tmp_path)
    _receipt(receipts, 3, "SWITCH")
    (receipts / "zzzz.json").write_text("{not json")
    (receipts / "zzzy.json").write_text(json.dumps({"outcome": "FAIL"}))  # no timestamp
    out = tmp_path / "beat" / "heartbeat.json"
    beat = heartbeat.write_heartbeat(out, heartbeat.check(pointer, receipts, now=NOW))
    assert beat["status"] == "green"
    assert json.loads(out.read_text()) == beat
    assert oct(out.stat().st_mode & 0o777) == "0o644"
    assert [p.name for p in out.parent.iterdir()] == ["heartbeat.json"]


def test_heartbeat_cli_exit_codes(tmp_path, capsys):
    from quoth.cli import main
    pointer, receipts = _layout(tmp_path)
    assert main(["heartbeat", "--pointer", str(pointer), "--receipts", str(receipts)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "red"
    _receipt(receipts, 0, "SKIP")
    # Real clock: a receipt recorded "now" relative to NOW may be in the past; write a fresh one.
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    (receipts / "fresh.json").write_text(json.dumps(
        {"receipt_id": "fresh", "outcome": "SKIP", "reasons": [], "recorded_at": now}))
    out = tmp_path / "hb.json"
    assert main(["heartbeat", "--pointer", str(pointer), "--receipts", str(receipts),
                 "--output", str(out)]) == 0
    assert json.loads(out.read_text())["status"] == "green"
