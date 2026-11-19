# Working on code-check

Read [CLAUDE.md](CLAUDE.md) for the architecture and invariants. They apply to Codex as well.

Use the Python 3.14 virtual environment in `.venv`. Run these checks before committing:

```sh
.venv/bin/pytest -q
.venv/bin/ruff check .
node --test tests-node/*.test.js
.venv/bin/python -m codecheck eval --replay
```

The replay must report `"passed": true`. Never hand-edit generated files. Change their sources and run the generator, for example `.venv/bin/python -m codecheck gen-agents --out agents`.
