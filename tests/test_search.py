import json
import subprocess
import sys

import pytest

from quoth.cli import main
from quoth.ingest import ingest_vault
from quoth.search import search

KEYS = ["chunk_id", "path", "heading_path", "line_start", "line_end", "scope", "score", "snippet", "retriever"]


@pytest.fixture
def indexed(conn, vault):
    ingest_vault(conn, vault)
    return conn


def test_contract_shape(indexed):
    hits = search(indexed, "tomatoes basil", k=10)
    assert hits and hits[0]["path"] == "PROJECTS/Garden.md"
    for h in hits:
        assert list(h) == KEYS
        assert isinstance(h["chunk_id"], int) and isinstance(h["score"], float)
        assert h["line_start"] <= h["line_end"]
    assert hits[0]["heading_path"] == "Garden"
    assert hits[0]["retriever"] in {"bm25", "trigram", "bm25+trigram"}


def test_rrf_fusion_scores(indexed):
    hits = search(indexed, "tomatoes", k=5)
    top = hits[0]
    assert top["retriever"] == "bm25+trigram"
    assert top["score"] == pytest.approx(2 / 61, abs=1e-6)
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)


def test_russian_inflection_via_trigram(indexed):
    # "медитацией" (instrumental) is not a token in the note; trigram stem matches.
    hits = search(indexed, "медитацией", k=5)
    assert hits and hits[0]["path"] == "KNOWLEDGE/Медитация.md"
    assert "trigram" in hits[0]["retriever"]
    # exact Russian token via unicode61
    hits = search(indexed, "сосредоточиться", k=5)
    assert hits[0]["path"] == "KNOWLEDGE/Медитация.md" and "bm25" in hits[0]["retriever"]


def test_sensitive_returned_by_default(indexed):
    # Full retrieval: every area of the vault comes back by default
    hits = search(indexed, "platypus", k=50)
    paths = {h["path"] for h in hits}
    assert paths == {
        "ME/Profile.md", "HEALTH/Bloodwork.md", "JOURNAL/Session 1.md", "PRIVATE/notes.md",
        "ME/Diary.md", "ME/dreams/2026-01-01.md", "ME/morning pages/day1.md",
        "ME/Boundaries.md",
    }


def test_scope_metadata_is_inert_in_read_path(indexed):
    # WORK/ note is still retrievable without any scope argument
    hits = search(indexed, "pipeline")
    assert {h["path"] for h in hits} == {"WORK/Quarterly plan.md"}
    assert hits[0]["scope"] == "work"  # kept as metadata only


def test_edited_content_not_returned(indexed, vault):
    (vault / "PROJECTS/Garden.md").write_text("# Garden\n\nCucumbers now.\n", encoding="utf-8")
    ingest_vault(indexed, vault)
    assert search(indexed, "tomatoes") == []
    assert search(indexed, "cucumbers")[0]["path"] == "PROJECTS/Garden.md"


def test_deleted_content_not_returned(indexed, vault):
    (vault / "PROJECTS/Garden.md").unlink()
    ingest_vault(indexed, vault)
    assert search(indexed, "basil") == []


def test_punctuation_and_quotes_safe(indexed):
    for q in ['"', "AND OR NOT", "(tomatoes", "a*b", "", "💥", "NEAR(x y)"]:
        search(indexed, q)


def test_cli_end_to_end(vault, db_path, capsys):
    assert main(["ingest", "--vault", str(vault), "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "new=12 changed=0 unchanged=0 removed=0" in out
    assert main(["ingest", "--vault", str(vault), "--db", str(db_path)]) == 0
    assert "new=0 changed=0 unchanged=12 removed=0" in capsys.readouterr().out
    assert main(["search", "медитация", "-k", "3", "--json", "--db", str(db_path)]) == 0
    hits = json.loads(capsys.readouterr().out)
    assert hits and list(hits[0]) == KEYS and hits[0]["path"] == "KNOWLEDGE/Медитация.md"
    assert main(["search", "platypus", "--top-k", "50", "--json", "--db", str(db_path)]) == 0
    assert {h["path"] for h in json.loads(capsys.readouterr().out)} == {
        "ME/Profile.md", "HEALTH/Bloodwork.md", "JOURNAL/Session 1.md", "PRIVATE/notes.md",
        "ME/Diary.md", "ME/dreams/2026-01-01.md", "ME/morning pages/day1.md",
        "ME/Boundaries.md",
    }
    for removed in (["--source-kind", "vault"], ["--scope", "work"]):
        with pytest.raises(SystemExit) as usage:
            main(["search", "x", *removed, "--db", str(db_path)])
        assert usage.value.code == 2


def test_console_script_installed(vault, db_path):
    r = subprocess.run(["quoth", "ingest", "--vault", str(vault), "--db", str(db_path), "--json"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["new"] == 12


def test_cli_db_default_resolves_from_quoth_home_at_call_time(vault, tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("QUOTH_HOME", str(home))
    assert main(["ingest", "--vault", str(vault), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["new"] == 12
    assert (home / "memory.db").is_file()
    with pytest.raises(SystemExit) as usage:
        main(["ingest"])  # --vault has no default
    assert usage.value.code == 2


def test_search_cli_reads_a_frozen_snapshot_without_writing(vault, db_path, capsys):
    import os
    import stat
    assert main(["ingest", "--vault", str(vault), "--db", str(db_path)]) == 0
    capsys.readouterr()
    os.chmod(db_path, stat.S_IRUSR)
    try:
        assert main(["search", "Profile", "--db", str(db_path), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)
    finally:
        os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)
