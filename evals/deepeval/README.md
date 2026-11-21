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
| `_env.py` | Sets DeepEval's telemetry opt-out before it is imported. |
| `test_*.py` | Tests for the three modules above. |

`FAILURE-MODES.md` records the failure modes these metrics were built for, how well each is supported, and what did not work.

## Read the numbers carefully

- A metric with a target (process notes, comment-cap findings, findings file written) reports pass or fail per run. The others only report.
- Versions are not a controlled comparison: the pull requests differ and most versions have few runs.
- A finding that is not on an added line is not necessarily wrong. A finding about a deletion, or about unchanged code the change breaks, can legitimately sit elsewhere.
- The scorecard measures format and bookkeeping, not whether a finding is true or worth raising. For that, see the judge results in `FAILURE-MODES.md`: the model judges tried so far were not reliable enough to use as metrics.

## Judge experiments (kept as a record)

Model-judge tools that were tried and measured. They cost tokens and read `ANTHROPIC_API_KEY` from the environment of the command you run (never from Claude Code's own environment). None is a metric you should trust, and none is part of the scorecard.

| File | What it does | Measured |
|---|---|---|
| `judge.py` | An Anthropic judge for DeepEval (handles a leading thinking block). | Only a building block. |
| `same_concern.py` | A "do these two comments raise the same concern?" metric with rubrics `STEPS` (v1) and `STEPS_V2`. | On 16 hand-labelled pairs, v1 matched the label exactly on 8 and v2 on 7. Useful only as a filter: every pair v1 scored 0.2 or lower was labelled "different". |
| `pairs.py`, `pairs.json` | 11 hand-built pairs of a human comment and a finding. `my_label` and `my_reason` are the assistant's; `owner_label` is the maintainer's label, whose provenance is not independently verified, and P2 is unlabelled. | Calibration data for the judge only. |
| `handoff_pairs.py` | Judges every human comment against every kept and dropped finding in real hand-offs. Output stays outside the repo. | Not a result by itself. |
| `compare_rubrics.py` | Runs both rubrics on every labelled pair. | See `same_concern.py`. |
| `code_validity.py` | A judge that decides whether a finding's claim is true of the code at the PR head commit, given the cited file and the PR diff. Pass bar fixed before the single run (8 of 9 correct, including 3 of the 4 true items). | **Missed its bar: 5 of 9**, no better than an always-"false" judge (5 of 9). It never gave a confident wrong answer, but 4 items came back UNCERTAIN, 3 of them because the deciding code was outside its context. Dropped; a subagent that reads the repo with `git show` at the head commit remains the only method that worked. |
