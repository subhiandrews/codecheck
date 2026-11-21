# DeepEval metrics for code-check reviews (advisory)

Deterministic metrics that answer "did a change make reviews better or worse?" on stored review runs. They call no model, cost no tokens, and are **advisory**: nothing here is a gate, runs in CI, or ships in the plugin. `eval --replay` and `eval --live` (see `docs/Evals.md`) stay the gates.

## Install and run

```
python3.14 -m venv ~/.venvs/codecheck-evals       # any venv outside the repo works
~/.venvs/codecheck-evals/bin/pip install -e ".[evals]" pytest ruff
~/.venvs/codecheck-evals/bin/python -B evals/deepeval/scorecard.py --source golden
~/.venvs/codecheck-evals/bin/python -B -m pytest -q -p no:cacheprovider evals/deepeval
```

`--source golden` uses the committed `tests/golden` cases and needs nothing else. `--source store` (the default) reads your own shadow store (`CODE_CHECK_SHADOW_STORE`, default `~/.local/share/code-check/shadow`); it needs the `*.raw.json` files that only runs made with `shadow.handoff = true` write. `--by-version` groups runs by tool version, `--misses` lists each run that misses a target, and `--json FILE` saves the rows.

## What is here

| File | What it does |
|---|---|
| `anchoring.py` | Classifies every reviewer finding as on an added line, a context line, relocated, unanchored or in a file outside the diff, using the tool's own `anchor_finding`. No model. |
| `metrics.py` | Six DeepEval `BaseMetric`s: anchoring share, no aggregator process notes, message length, no comment-cap findings, findings file written, aggregator accounting. |
| `scorecard.py` | Runs those metrics over each run as an `LLMTestCase` in an `EvaluationDataset`. |
| `f1_baseline.py` | For each human comment a golden case's panel missed, reports the furthest pipeline stage that held a finding at the same place (kept, dropped, raw only, same file, not raised). Location matching only, so a nearby finding is a candidate, not proof. Reads `tests/golden`; no model. |
| `_env.py` | Sets DeepEval's telemetry opt-out before it is imported. |
| `test_*.py` | Tests for the three modules above. |

`FAILURE-MODES.md` records the failure modes these metrics were built for, how well each is supported, and what did not work. `notes.md` is the longer working record: the failure taxonomy (F1 to F7), what each measurement showed, and what was later corrected.

## Read the numbers carefully

- A metric with a target (process notes, comment-cap findings, findings file written) reports pass or fail per run. The others only report.
- Versions are not a controlled comparison: the pull requests differ and most versions have few runs.
- A finding that is not on an added line is not necessarily wrong. A finding about a deletion, or about unchanged code the change breaks, can legitimately sit elsewhere.
- The scorecard measures format and bookkeeping, not whether a finding is true or worth raising. For that, see the judge results in `FAILURE-MODES.md`: the model judges tried so far were not reliable enough to use as metrics.
