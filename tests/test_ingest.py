from pathlib import Path

from quoth.ingest import ingest_vault


def _current(conn):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT s.path, v.content_hash FROM sources s JOIN source_versions v"
        " ON v.source_id = s.id AND v.is_current = 1")}


def test_first_and_second_run(conn, vault):
    st = ingest_vault(conn, vault)
    assert (st.new, st.changed, st.unchanged, st.removed) == (12, 0, 0, 0)
    paths = set(_current(conn))
    assert not any("zanzibar" in r[0] for r in conn.execute("SELECT text FROM chunks"))
    assert not any(p.startswith((".obsidian", ".trash", ".git")) or "attachments/" in p for p in paths)
    n_chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    st2 = ingest_vault(conn, vault)
    assert (st2.new, st2.changed, st2.unchanged, st2.removed, st2.chunks_added) == (0, 0, 12, 0, 0)
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == n_chunks
    assert conn.execute("SELECT count(*) FROM source_versions").fetchone()[0] == 12


def test_scopes_and_metadata(conn, vault):
    ingest_vault(conn, vault)
    scopes = dict(conn.execute("SELECT path, scope FROM sources"))
    # Every area of the vault is plain general metadata
    assert scopes["HEALTH/Bloodwork.md"] == "general"
    assert scopes["ME/dreams/2026-01-01.md"] == "general"
    assert scopes["WORK/Quarterly plan.md"] == "work"
    assert scopes["ME/Profile.md"] == "general"
    import json
    meta = json.loads(conn.execute(
        "SELECT v.metadata FROM source_versions v JOIN sources s ON s.id=v.source_id"
        " WHERE s.path='Vault Conventions.md'").fetchone()[0])
    assert meta["tags"] == ["meta", "conventions"] and meta["status"] == "active"
    assert meta["created"] == "2026-04-21" and meta["updated"] == "2026-09-12"


def test_edit_creates_new_version_old_chunks_kept(conn, vault):
    ingest_vault(conn, vault)
    p = vault / "PROJECTS/Garden.md"
    old_ids = [r[0] for r in conn.execute(
        "SELECT c.id FROM chunks c JOIN source_versions v ON v.id=c.source_version_id"
        " JOIN sources s ON s.id=v.source_id WHERE s.path='PROJECTS/Garden.md'")]
    p.write_text("# Garden\n\nCucumbers replaced the tomatoes.\n", encoding="utf-8")
    st = ingest_vault(conn, vault)
    assert (st.new, st.changed, st.unchanged, st.removed) == (0, 1, 11, 0)
    vers = conn.execute(
        "SELECT v.is_current FROM source_versions v JOIN sources s ON s.id=v.source_id"
        " WHERE s.path='PROJECTS/Garden.md' ORDER BY v.id").fetchall()
    assert [r[0] for r in vers] == [0, 1]
    # old chunks still exist (never deleted) but are gone from FTS
    for cid in old_ids:
        assert conn.execute("SELECT 1 FROM chunks WHERE id=?", (cid,)).fetchone()
        assert not conn.execute("SELECT 1 FROM chunks_fts WHERE rowid=?", (cid,)).fetchone()
        assert not conn.execute("SELECT 1 FROM chunks_fts_trigram WHERE rowid=?", (cid,)).fetchone()


def test_delete_and_reappear(conn, vault):
    ingest_vault(conn, vault)
    p = vault / "PROJECTS/Garden.md"
    body = p.read_text(encoding="utf-8")
    p.unlink()
    st = ingest_vault(conn, vault)
    assert (st.new, st.changed, st.unchanged, st.removed) == (0, 0, 11, 1)
    assert "PROJECTS/Garden.md" not in _current(conn)
    assert conn.execute("SELECT count(*) FROM sources WHERE path='PROJECTS/Garden.md'").fetchone()[0] == 1
    st = ingest_vault(conn, vault)
    assert st.removed == 0  # removal is not re-counted
    p.write_text(body, encoding="utf-8")
    st = ingest_vault(conn, vault)
    assert (st.new, st.removed) == (1, 0)
    assert conn.execute("SELECT count(*) FROM sources WHERE path='PROJECTS/Garden.md'").fetchone()[0] == 1


def test_rename_is_removed_plus_new(conn, vault):
    ingest_vault(conn, vault)
    src = vault / "KNOWLEDGE/Медитация.md"
    dst = vault / "KNOWLEDGE/Практики/Медитация утром.md"
    dst.parent.mkdir(parents=True)
    src.rename(dst)
    st = ingest_vault(conn, vault)
    assert (st.new, st.changed, st.unchanged, st.removed) == (1, 0, 11, 1)
    cur = _current(conn)
    assert "KNOWLEDGE/Практики/Медитация утром.md" in cur and "KNOWLEDGE/Медитация.md" not in cur


def test_rename_rederives_scope_metadata(conn, vault):
    ingest_vault(conn, vault)
    (vault / "HEALTH").mkdir(exist_ok=True)
    (vault / "PROJECTS/Garden.md").rename(vault / "HEALTH/Garden.md")
    ingest_vault(conn, vault)
    # Scope is metadata only; HEALTH/ is general
    assert conn.execute("SELECT scope FROM sources WHERE path='HEALTH/Garden.md'").fetchone()[0] == "general"


def test_vault_is_not_written(conn, vault):
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in vault.rglob("*") if p.is_file()}
    ingest_vault(conn, vault)
    after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in vault.rglob("*") if p.is_file()}
    assert before == after


def test_hidden_folders_and_notes_are_never_read(conn, vault):
    from quoth.refresh import inventory

    for rel in (".stversions/Garden~20260101.md", ".claude/commands/review.md",
                "PROJECTS/.draft.md", "PROJECTS/.cache/x.md", "ASSISTANT/summary.md"):
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("hidden zanzibar\n", encoding="utf-8")
    st = ingest_vault(conn, vault)
    assert st.new == 12
    assert set(_current(conn)) == set(inventory(vault))
    assert not any("zanzibar" in r[0] for r in conn.execute("SELECT text FROM chunks"))


def test_byte_order_mark_does_not_hide_frontmatter(conn, tmp_path):
    import json

    vault = tmp_path / "bom"
    vault.mkdir()
    (vault / "2026-03-14.md").write_bytes(
        "﻿---\r\ncreated: 2026-03-14\r\n---\r\n# Day\r\nPlanted basil.\r\n".encode())
    ingest_vault(conn, vault)
    meta = conn.execute("SELECT metadata FROM source_versions").fetchone()[0]
    assert json.loads(meta) == {"created": "2026-03-14"}
    text, start = conn.execute("SELECT text, line_start FROM chunks").fetchone()
    assert text == "# Day\nPlanted basil." and start == 4
