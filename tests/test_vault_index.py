"""Index side of 0.17: exclusion rules, purge, links, recall tiers, file facts, snapshot resume."""
import json
import os
import sqlite3
from pathlib import Path

import pytest

from quoth.db import connect_read_only, open_db
from quoth.freshness import status
from quoth.ingest import ingest_vault
from quoth.links import Resolver, extract_links
from quoth.refresh import refresh, sha, snapshot
from quoth.scopes import SCOPES, explicit_scopes, load_config, recall_tier
from quoth.vault import IgnoreRules, frontmatter_excluded, ignore_rules_sha256

from test_refresh import FakeEmbedder


def rules(text: str) -> IgnoreRules:
    return IgnoreRules(text, "x")


# --------------------------------------------------------------------------- ignore rules

def test_ignore_unanchored_anchored_and_dir_only():
    r = rules("Health/\n/Journal/private.md\n*.draft.md\n")
    assert r.ignored("Health/a.md") and r.ignored("Areas/Health/deep/b.md")
    assert not r.ignored("Healthy/a.md") and not r.ignored("Health.md")
    assert r.ignored("Journal/private.md") and not r.ignored("Old/Journal/private.md")
    assert r.ignored("x.draft.md") and r.ignored("a/b/x.draft.md")
    # A pattern with an inner slash is anchored at the root.
    a = rules("Areas/Health\n")
    assert a.ignored("Areas/Health/x.md") and not a.ignored("Old/Areas/Health/x.md")


def test_ignore_negation_last_rule_wins_and_no_reinclude_under_ignored_dir():
    r = rules("Journal/*\n!Journal/keep.md\n# a comment\n\n")
    assert r.ignored("Journal/a.md") and not r.ignored("Journal/keep.md")
    # Journal/* matches the sub folder itself: its notes cannot be re-included.
    assert r.ignored("Journal/sub/keep.md")
    d = rules("Journal/\n!Journal/keep.md\n")
    assert d.ignored("Journal/keep.md")  # as in git: the folder is ignored
    n = rules("*.md\n!Public.md\n")
    assert n.ignored("x.md") and not n.ignored("Public.md") and not n.ignored("a/Public.md")


def test_ignore_double_star_and_classes():
    r = rules("**/tmp/**\na/**/b.md\nlog[0-9].md\n")
    assert r.ignored("tmp/x.md") and r.ignored("q/tmp/z/x.md") and not r.ignored("tmpx/y.md")
    assert r.ignored("a/b.md") and r.ignored("a/x/y/b.md") and not r.ignored("c/a/b.md")
    assert r.ignored("log1.md") and not r.ignored("logx.md")


def test_builtin_rules_cannot_be_reincluded():
    r = rules("!ASSISTANT/\n!.hidden.md\n!ASSISTANT/x.md\n")
    assert r.ignored("ASSISTANT/x.md") and r.ignored(".hidden.md") and r.ignored("a/.obsidian/x.md")
    assert r.ignored("attachments/x.md")
    assert not IgnoreRules().ignored("Plan.md")


def test_frontmatter_exclude():
    assert frontmatter_excluded(b"---\nquoth: exclude\n---\nbody\n")
    assert frontmatter_excluded("﻿---\r\nquoth: \"Exclude\"\r\n---\r\n".encode())
    assert frontmatter_excluded(b"---\nquoth: [exclude]\n---\n")
    assert not frontmatter_excluded(b"---\nquoth: include\n---\n")
    assert not frontmatter_excluded(b"quoth: exclude\n")
    assert not frontmatter_excluded(b"# T\n---\nquoth: exclude\n---\n")


# --------------------------------------------------------------------------- recall tiers

def test_recall_tier_helpers():
    assert recall_tier("general") == "auto" and recall_tier("work") == "auto"
    assert recall_tier("private") == "explicit"
    assert recall_tier("not-declared") == "auto"
    assert explicit_scopes() == frozenset({"private"})
    assert set(SCOPES) == {"general", "work", "private"} == set(load_config().scopes)


def test_recall_tier_rejects_unknown_tier(monkeypatch):
    import quoth.scopes as scopes

    class Fake:
        def joinpath(self, _):
            return self

        def read_text(self, _):
            return ('default = "general"\ndefault_recall = ["general"]\n'
                    '[scope.general]\nrecall = "sometimes"\n')
    monkeypatch.setattr(scopes, "files", lambda _pkg: Fake())
    scopes.load_config.cache_clear()
    try:
        with pytest.raises(ValueError, match="recall tier"):
            scopes.load_config()
    finally:
        scopes.load_config.cache_clear()


# --------------------------------------------------------------------------- ingest + purge

SECRET = "Quuxsecretword"


def _all_rows_text(conn: sqlite3.Connection) -> str:
    return "\n".join(conn.iterdump())


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "Health").mkdir(parents=True)
    (vault / "Journal").mkdir()
    (vault / "ASSISTANT").mkdir()
    (vault / "Plan.md").write_text("# Plan\nSee [[Note]] and [[Diagnosis]].\n")
    (vault / "Note.md").write_text("# Note\nPlain text.\n")
    (vault / "Health" / "Diagnosis.md").write_text(f"# Diagnosis\n{SECRET} one.\n")
    (vault / "Journal" / "Day.md").write_text(f"# Day\n{SECRET} two.\n")
    (vault / "Journal" / "Public.md").write_text("# Public\nShared.\n")
    (vault / "Hidden.md").write_text(f"---\nquoth: exclude\n---\n# Hidden\n{SECRET} three.\n")
    (vault / "ASSISTANT" / "Mine.md").write_text(f"# Mine\n{SECRET} four.\n")
    (vault / ".quothignore").write_text("Health/\nJournal/*\n!Journal/Public.md\n")
    return vault


def _paths(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT path FROM sources")}


def test_ingest_applies_every_exclusion(tmp_path):
    vault = _make_vault(tmp_path)
    conn = open_db(tmp_path / "db.sqlite")
    stats = ingest_vault(conn, vault)
    assert _paths(conn) == {"Plan.md", "Note.md", "Journal/Public.md"}
    assert stats.excluded == 4 and stats.purged == 0
    assert SECRET not in _all_rows_text(conn) and SECRET.lower() not in _all_rows_text(conn)


def test_newly_excluded_notes_are_purged_with_history(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Health").mkdir(parents=True)
    (vault / "Plan.md").write_text("# Plan\nLink to [[Old]] and [[Diagnosis]].\n")
    (vault / "Health" / "Diagnosis.md").write_text(f"# Diagnosis\n{SECRET} v1.\n")
    (vault / "Old.md").write_text(f"# Old\n{SECRET} old.\n")
    conn = open_db(tmp_path / "db.sqlite")
    ingest_vault(conn, vault)
    from quoth.embed.store import embed_chunks
    embed_chunks(conn, FakeEmbedder())
    (vault / "Health" / "Diagnosis.md").write_text(f"# Diagnosis\n{SECRET} v2.\n")
    ingest_vault(conn, vault)  # a retired version now exists too
    assert conn.execute("SELECT count(*) FROM source_versions").fetchone()[0] == 4
    # Exclude one note by .quothignore, one by frontmatter.
    (vault / ".quothignore").write_text("Health/\n")
    (vault / "Old.md").write_text(f"---\nquoth: exclude\n---\n# Old\n{SECRET} old.\n")
    stats = ingest_vault(conn, vault)
    assert stats.purged == 2 and stats.removed == 0
    assert _paths(conn) == {"Plan.md"}
    for tbl in ("source_versions", "chunks", "embeddings", "entities", "links",
                "chunks_fts", "chunks_fts_trigram"):
        assert conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0] > 0 or tbl == "embeddings"
    dump = _all_rows_text(conn)
    assert SECRET not in dump and SECRET.lower() not in dump and "Diagnosis.md" not in dump
    assert conn.execute("SELECT count(*) FROM purge_sources").fetchone()[0] == 0
    # The links stay, as written, but name no excluded path.
    links = conn.execute("SELECT target, status, target_path FROM links ORDER BY target").fetchall()
    assert [tuple(r) for r in links] == [("Diagnosis", "excluded", None), ("Old", "excluded", None)]
    # Deleting a note (not excluding it) still keeps its retired history.
    (vault / "Plan.md").unlink()
    assert ingest_vault(conn, vault).removed == 1 and _paths(conn) == {"Plan.md"}


def test_append_only_still_holds_outside_a_purge(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\ntext\n")
    conn = open_db(tmp_path / "db.sqlite")
    ingest_vault(conn, vault)
    for sql in ("DELETE FROM chunks", "DELETE FROM source_versions", "DELETE FROM sources"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)


def test_file_facts_for_staleness(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "a.md"
    note.write_text("# A\nfirst\n")
    conn = open_db(tmp_path / "db.sqlite")
    ingest_vault(conn, vault)

    def facts():
        return tuple(conn.execute(
            "SELECT s.path, v.content_hash, v.mtime_ns, v.size_bytes FROM sources s"
            " JOIN source_versions v ON v.source_id = s.id AND v.is_current = 1").fetchone())
    st = note.stat()
    assert facts() == ("a.md", sha(note), st.st_mtime_ns, st.st_size)
    os.utime(note, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    stats = ingest_vault(conn, vault)
    assert stats.unchanged == 1 and facts()[2] == note.stat().st_mtime_ns
    note.write_text("# A\nsecond, longer\n")
    ingest_vault(conn, vault)
    assert facts() == ("a.md", sha(note), note.stat().st_mtime_ns, note.stat().st_size)


# --------------------------------------------------------------------------- links

def test_extract_links_forms():
    text = ("---\nrelated: \"[[Front]]\"\n---\n# T\n"
            "A [[Target]] and [[Other|alias]] and [[Deep#Heading|x]] and ![[Pic.png]].\n"
            "Markdown [t](sub/Page%20One.md#Part) and [u](<a b.md>) and [w](https://x.org/a.md)\n"
            "and [img](pic.png) and `[[Code]]` and [[#Local]]\n"
            "```\n[[Fenced]]\n```\n")
    got = [(ln.kind, ln.target, ln.heading, ln.line) for ln in extract_links(text)]
    assert got == [
        ("wikilink", "Target", None, 5), ("wikilink", "Other", None, 5),
        ("wikilink", "Deep", "Heading", 5), ("embed", "Pic.png", None, 5),
        ("markdown", "sub/Page%20One.md", "Part", 6), ("markdown", "a b.md", None, 6),
        ("wikilink", "", "Local", 7),
    ]


def test_resolver_exact_basename_ambiguous_missing_excluded():
    r = Resolver({"Plan.md", "Projects/Alpha.md", "A/Dup.md", "B/Dup.md", "Sub/Page One.md",
                  "Sub/Inner/Leaf.md"}, {"Health/Secret.md", "Other/Twin.md"})
    assert r.resolve("wikilink", "Plan", "x.md") == ("resolved", "Plan.md")
    assert r.resolve("wikilink", "plan.md", "x.md") == ("resolved", "Plan.md")
    assert r.resolve("wikilink", "Alpha", "x.md") == ("resolved", "Projects/Alpha.md")
    assert r.resolve("wikilink", "Projects/Alpha", "x.md") == ("resolved", "Projects/Alpha.md")
    assert r.resolve("wikilink", "Inner/Leaf", "x.md") == ("resolved", "Sub/Inner/Leaf.md")
    assert r.resolve("wikilink", "Dup", "x.md") == ("ambiguous", None)
    assert r.resolve("wikilink", "A/Dup", "x.md") == ("resolved", "A/Dup.md")
    assert r.resolve("wikilink", "Nowhere", "x.md") == ("missing", None)
    assert r.resolve("embed", "Pic.png", "x.md") == ("missing", None)
    assert r.resolve("wikilink", "Secret", "x.md") == ("excluded", None)
    assert r.resolve("wikilink", "Health/Secret", "x.md") == ("excluded", None)
    assert r.resolve("wikilink", "", "Plan.md") == ("resolved", "Plan.md")
    assert r.resolve("markdown", "Page%20One.md", "Sub/x.md") == ("resolved", "Sub/Page One.md")
    assert r.resolve("markdown", "../Plan.md", "Sub/x.md") == ("resolved", "Plan.md")
    assert r.resolve("markdown", "../../Plan.md", "Sub/x.md") == ("resolved", "Plan.md")
    assert r.resolve("markdown", "../Health/Secret.md", "Sub/x.md") == ("excluded", None)


def test_resolver_excluded_twin_makes_a_name_ambiguous():
    # An excluded note of the same name keeps a bare link from resolving to the other one.
    r = Resolver({"Notes/Twin.md"}, {"Health/Twin.md"})
    assert r.resolve("wikilink", "Twin", "x.md") == ("ambiguous", None)


def test_links_table_follows_the_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "A").mkdir(parents=True)
    (vault / "Plan.md").write_text("# Plan\nSee [[Dup]], [[Later]] and [x](A/Dup.md#Top).\n")
    (vault / "A" / "Dup.md").write_text("# Dup\none\n")
    conn = open_db(tmp_path / "db.sqlite")
    ingest_vault(conn, vault)

    def rows():
        return [tuple(r) for r in conn.execute(
            "SELECT l.target, l.heading, l.line, l.kind, l.status, l.target_path FROM links l"
            " ORDER BY l.kind, l.target")]
    assert rows() == [("A/Dup.md", "Top", 2, "markdown", "resolved", "A/Dup.md"),
                      ("Dup", None, 2, "wikilink", "resolved", "A/Dup.md"),
                      ("Later", None, 2, "wikilink", "missing", None)]
    # Unchanged Plan.md is re-resolved as other notes come and go.
    (vault / "B").mkdir()
    (vault / "B" / "Dup.md").write_text("# Dup\ntwo\n")
    (vault / "Later.md").write_text("# Later\nthree\n")
    ingest_vault(conn, vault)
    assert rows() == [("A/Dup.md", "Top", 2, "markdown", "resolved", "A/Dup.md"),
                      ("Dup", None, 2, "wikilink", "ambiguous", None),
                      ("Later", None, 2, "wikilink", "resolved", "Later.md")]
    # Only current chunks keep rows.
    (vault / "Plan.md").write_text("# Plan\nNo links now.\n")
    ingest_vault(conn, vault)
    assert rows() == []


# --------------------------------------------------------------------------- snapshots

def test_snapshot_never_stores_excluded_notes_and_refresh_purges(tmp_path):
    vault = _make_vault(tmp_path)
    (vault / ".quothignore").write_text("Health/\n")  # Journal still indexed at first
    first = tmp_path / "snap" / "s1.db"
    m1 = snapshot(first, vault, embedder=FakeEmbedder())
    assert m1["ignore_rules_sha256"] == sha(vault / ".quothignore")
    assert m1["built_at"].endswith("Z") and "T" in m1["built_at"]
    raw = first.read_bytes()
    assert b"Health/Diagnosis" not in raw and SECRET.encode() + b" one" not in raw
    assert b"Mine.md" not in raw and b"three" not in raw
    with connect_read_only(first) as db:
        assert _paths(db) == {"Plan.md", "Note.md", "Journal/Day.md", "Journal/Public.md"}
    assert status(first, vault)["state"] == "fresh"
    # A rule change alone makes the snapshot stale.
    (vault / ".quothignore").write_text("Health/\nJournal/*\n!Journal/Public.md\n")
    st = status(first, vault)
    assert st["state"] == "stale" and "ignore_rules_changed" in st["reasons"]
    second = tmp_path / "snap" / "s2.db"
    m2 = refresh(first, second, vault, embedder=FakeEmbedder())
    assert m2["ingest"]["purged"] == 1
    raw = second.read_bytes()
    assert SECRET.encode() not in raw and SECRET.lower().encode() not in raw
    assert b"Journal/Day.md" not in raw
    with connect_read_only(second) as db:
        assert _paths(db) == {"Plan.md", "Note.md", "Journal/Public.md"}
        dump = _all_rows_text(db)
        assert SECRET not in dump and "Journal/Day" not in dump
    assert status(second, vault)["state"] == "fresh"
    # An unchanged rules file keeps it fresh; removing it makes it stale again.
    (vault / ".quothignore").unlink()
    assert ignore_rules_sha256(vault) is None
    assert status(second, vault)["reasons"] == ["inventory_changed", "ignore_rules_changed"]


class Killed(BaseException):
    pass


def test_snapshot_resume_after_kill_mid_embedding(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    for i in range(6):
        (vault / f"n{i}.md").write_text(f"# Note {i}\n" + "word " * (i + 1) + "\n")
    whole = snapshot(tmp_path / "a" / "whole.db", vault, embedder=FakeEmbedder(), batch_size=1)

    calls = []

    def die():
        calls.append(1)
        if len(calls) == 3:
            raise Killed()
    target = tmp_path / "b" / "resumed.db"
    with pytest.raises(Killed):
        snapshot(target, vault, embedder=FakeEmbedder(on_embed=die), batch_size=1)
    checkpoint = target.with_suffix(".checkpoint.json")
    assert checkpoint.is_file() and not target.with_suffix(".manifest.json").exists()
    db = sqlite3.connect(target)
    assert db.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 2
    db.close()
    with pytest.raises(ValueError, match="collision"):
        snapshot(target, vault, embedder=FakeEmbedder())
    # The checkpoint must match: a different vault is refused.
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="resumable checkpoint"):
        snapshot(target, other, embedder=FakeEmbedder(), resume=True)
    resumed = snapshot(target, vault, embedder=FakeEmbedder(), batch_size=1, resume=True)
    assert resumed["embed"]["embedded"] == 4 and not checkpoint.exists()
    for key in ("counts", "source_inventory_sha256", "code_digests", "index_code_digests",
                "model_manifest_sha256", "ignore_rules_sha256", "vault_root", "status"):
        assert resumed[key] == whole[key], key

    def content(db_path):
        with connect_read_only(db_path) as db:
            return [tuple(r) for r in db.execute(
                "SELECT s.path, c.heading_path, c.line_start, c.line_end, c.text, e.vector"
                " FROM chunks c JOIN source_versions v ON v.id = c.source_version_id"
                " JOIN sources s ON s.id = v.source_id JOIN embeddings e ON e.chunk_id = c.id"
                " ORDER BY c.id")]
    assert content(target) == content(tmp_path / "a" / "whole.db")
    assert status(target, vault)["state"] == "fresh"
    with pytest.raises(ValueError, match="collision"):
        snapshot(target, vault, embedder=FakeEmbedder(), resume=True)


def test_snapshot_resume_without_checkpoint_is_refused(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ValueError, match="resumable checkpoint"):
        snapshot(tmp_path / "x.db", vault, embedder=FakeEmbedder(), resume=True)
    cp = tmp_path / "y.checkpoint.json"
    cp.write_text(json.dumps({"source_sha256": None}))
    with pytest.raises(ValueError, match="resumable checkpoint"):
        snapshot(tmp_path / "y.db", vault, embedder=FakeEmbedder(), resume=True)


def test_rows_left_under_a_builtin_exclusion_are_purged(tmp_path):
    # An index that still holds rows under ASSISTANT/ (as older releases kept retired
    # history there) loses them, current and historical, on the next ingest.
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\ntext\n")
    conn = open_db(tmp_path / "db.sqlite")
    sid = conn.execute(
        "INSERT INTO sources(path, scope) VALUES ('ASSISTANT/x.md', 'general')").lastrowid
    for current in (0, 1):
        vid = conn.execute("INSERT INTO source_versions(source_id, content_hash, mtime_ns,"
                           " size_bytes, is_current) VALUES (?, ?, 1, 1, ?)",
                           (sid, f"h{current}", current)).lastrowid
        conn.execute("INSERT INTO chunks(source_version_id, line_start, line_end, text)"
                     " VALUES (?, 1, 1, ?)", (vid, f"{SECRET} {current}"))
    stats = ingest_vault(conn, vault)
    assert stats.purged == 1 and _paths(conn) == {"a.md"}
    assert SECRET not in _all_rows_text(conn) and "ASSISTANT" not in _all_rows_text(conn)
