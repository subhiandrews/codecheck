# Contributing to code-check

Thanks for helping. Bug reports, fixes, docs and new features are all welcome.

## License of contributions

code-check is under the [Apache License 2.0](LICENSE). By submitting a pull request you agree that your contribution is licensed under the same terms (Apache-2.0, section 5). There is no CLA to sign.

## Set up

You need Python 3.14 (3.14.10 or newer is recommended) and Node 20 or newer.

```
python3.14 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Before you open a pull request

```
.venv/bin/ruff check .                       # must pass
.venv/bin/pytest -q                          # Python tests
node --test tests-node/*.test.js             # installer tests
```

CI runs the same three checks. `main` is protected: changes land through a pull request with one approving review.

## Guidelines

- **Tests must be able to fail.** A test has to break when the behaviour it covers breaks. Compare against literal expected values. Avoid `assert True`, asserting a value against the expression the code just returned, and mocks that only echo the test's own setup. See [CODE_CHECK.md](CODE_CHECK.md), which code-check applies to its own reviews.
- **Generated files.** `agents/*.md` are generated from `src/codecheck/agentgen.py`. After changing the generator or the default models, run `.venv/bin/python -m codecheck gen-agents --out agents` and commit the result; a test fails if they drift.
- **Skills and docs.** A flag or subcommand used in a skill must exist in the CLI, and every config key must appear in the README; tests check both.
- **Security-sensitive input.** Anything that comes from a pull request, a repository's config or reviewer output must never act as a command-line option, must stay inside the directory it is meant for, and must never be force-pushed. Read `CLAUDE.md` for the invariants.
- Keep changes focused, and explain the why in the pull request description.

## Reporting security problems

Please do not open a public issue for a vulnerability; see [SECURITY.md](SECURITY.md).
