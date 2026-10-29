## What and why

<!-- What this changes, and the reason for it. Link the issue if there is one. -->

## Checklist

- [ ] Full test suite run locally (`uv run pytest -q`); result:
- [ ] Tests added or updated for the changed behaviour
- [ ] `version` bumped in `pyproject.toml` and a line added to `CHANGELOG.md` (if the change ships in the wheel)
- [ ] No file in `refresh.py` `CODE_FILES` touched, or the PR explains the index-contract change
- [ ] No private data: fixtures stay synthetic, no corpus, snapshots, chat history or model files
