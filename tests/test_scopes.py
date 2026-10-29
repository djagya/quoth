import pytest

from quoth.scopes import load_config, scope_for

CASES = [
    # no sensitive scope exists: private-looking folders are general
    ("PRIVATE/anything.md", "general"),
    ("PRIVATE/deep/nested.md", "general"),
    ("JOURNAL/Session 1.md", "general"),
    ("HEALTH/Bloodwork.md", "general"),
    ("ME/Diary.md", "general"),
    ("ME/dreams/2026-01-01.md", "general"),
    ("ME/morning pages/day1.md", "general"),
    ("ME/Boundaries.md", "general"),
    ("WORK/Quarterly plan.md", "work"),
    ("WORK/sub/x.md", "work"),
    # everything else -> general, including near-misses
    ("ME/Profile.md", "general"),
    ("PROJECTS/WORK/x.md", "general"),
    ("KNOWLEDGE/Медитация.md", "general"),
    ("Vault Conventions.md", "general"),
]


@pytest.mark.parametrize("path,expected", CASES)
def test_scope_for(path, expected):
    assert scope_for(path) == expected


def test_config_invariants():
    cfg = load_config()
    assert cfg.default == "general"
    assert "sensitive" not in cfg.default_recall
    # no rule maps any path to a sensitive scope (it no longer exists)
    assert all(scope == "work" for scope, _ in cfg.rules)
    assert "ASSISTANT" in cfg.exclude_dirs
    for d in (".obsidian", ".git", ".trash", ".stversions", ".claude", "ASSISTANT", "attachments"):
        assert cfg.skip_dir(d)
    for d in ("JOURNAL", "WORK", "obsidian-notes", "Assistant notes"):
        assert not cfg.skip_dir(d)
    assert cfg.is_note("Plan.md") and not cfg.is_note(".draft.md") and not cfg.is_note("a.txt")
