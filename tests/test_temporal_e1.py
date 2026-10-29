"""The control lab's E1 scorer on synthetic data (no external model)."""

from datetime import date

from quoth.control.lab_suites import baseline_caller, validate, validated_note_date
from quoth.ingest import ingest_vault


def write_vault(root, notes):
    for rel, body in notes.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


def test_note_date_requires_filename_equal_created():
    assert validated_note_date("A/2026-09-24.md", '{"created": "2026-09-24"}') == date(2026, 9, 24)
    assert validated_note_date("A/2026-09-24.md", '{"created": "2026-09-25"}') is None
    assert validated_note_date("A/2026-09-24.md", None) is None  # mtime is never a fallback
    assert validated_note_date("A/notes.md", '{"created": "2026-09-24"}') is None
    assert validated_note_date("A/2026-02-30.md", '{"created": "2026-02-30"}') is None


def _journal(tmp_path, n=8):
    notes = {}
    for i in range(1, n + 1):
        d = f"2026-09-{i:02d}"
        body = "walked the dog in the park" if i % 2 else "cooked soup at home"
        notes[f"J/{d}.md"] = f"---\ncreated: {d}\n---\n# {d}\n\nToday I {body}.\n"
    notes["J/2026-09-30.md"] = "---\ncreated: 2026-09-29\n---\nToday I walked the dog in the park.\n"
    return write_vault(tmp_path / "v", notes)


def test_eval_validate_and_baseline_caller(conn, tmp_path):
    ingest_vault(conn, _journal(tmp_path))
    items = [dict(id="x", category="temporal_order", series="J/", evidence_regex="dog",
                  op="last", lo=None, hi=None, gold_path="J/2026-09-07.md", expect_abstain=False),
             dict(id="y", category="abstain_temporal", series="J/", evidence_regex="piano",
                  op="last", lo=None, hi=None, gold_path=None, expect_abstain=True),
             dict(id="z", category="temporal_order", series="J/", evidence_regex="dog",
                  op="first", lo=None, hi=None, gold_path="J/2026-09-03.md", expect_abstain=False)]
    assert [p[:2] for p in validate(conn, items)] == [("z", "gold is not the chronological extreme")]
    hit = lambda p, r: dict(path=p, rank=r, locator=f"{p}:1-2", line_start=1, line_end=2)
    bundle = {"hits": [hit("J/2026-09-03.md", 1), hit("J/2026-09-05.md", 2), hit("K/2026-09-09.md", 3)]}
    assert baseline_caller(bundle, items[0])["path"] == "J/2026-09-05.md"
    assert baseline_caller({"hits": []}, items[0]) == {"status": "insufficient"}
