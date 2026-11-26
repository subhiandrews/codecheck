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
| `outcomes.py` | Reads a `posted-reviews-*.json` and asks GitHub (GET only) what happened to each posted finding: replies, reactions, later commits near the cited line, PR state. Prints candidates, not labels. No model. |
| `review_sheets.py` | Builds blind labelling sheets (dropped and kept findings, drawn at random with a seed) from your shadow store, with the diff around each finding and the aggregator's reason hidden until you choose. Sheets and the answer key are written outside the repo because they hold finding text. No model. |
| `routing_ab.py` | Runs the reviewer routing at a git ref and the checkout's over every distinct stored diff and lists where the decision flipped. No model. See "Routing A/B" below. |
| `_env.py` | Sets DeepEval's telemetry opt-out before it is imported. |
| `test_*.py` | Tests for the modules above. |

`FAILURE-MODES.md` records the failure modes these metrics were built for, how well each is supported, and what did not work. `notes.md` is the longer working record: the failure taxonomy (F1 to F7), what each measurement showed, and what was later corrected.

## Read the numbers carefully

- A metric with a target (process notes, comment-cap findings, findings file written) reports pass or fail per run. The others only report.
- Versions are not a controlled comparison: the pull requests differ and most versions have few runs.
- A finding that is not on an added line is not necessarily wrong. A finding about a deletion, or about unchanged code the change breaks, can legitimately sit elsewhere.
- The scorecard measures format and bookkeeping, not whether a finding is true or worth raising. For that, see the judge results in `FAILURE-MODES.md`: the model judges tried so far were not reliable enough to use as metrics.

## What we do not evaluate

One maintainer, no paid raters, and labels on repos or languages the maintainer does not read are weak. So these are not measured, and effort on them is not planned:

- Whether a finding is worth posting, or precision at scale: one reader and no second rater.
- A model judge as a metric (same-concern, claim-validity): measured against hand labels and it missed its bar. See `notes.md`.
- Recall against human review comments on arbitrary PRs: few PRs, some human comments are AI-assisted, and one review finds only about half of what several runs find.
- Whether authors act on reviews: there is no user population to sample.
- Prompt-wording effects smaller than run-to-run noise: measured, no effect beyond noise.

What is measured instead needs no judgement beyond what the code or git history decides: the deterministic scorecard, the replay gate, routing A/B, and replays of PRs against findings that were later fixed in the repository. Add a golden case only when its label comes from that kind of source, and record it in `labelled_by`.

## Routing A/B

`routing_ab.py` runs the reviewer routing at a git ref (`--base-ref`, default `origin/main`) and the checkout's routing over every distinct diff in your shadow store, and prints summary counts (the performance decision old against new, and how many diffs changed the architect or docs decision) and the number of performance flips. `--json FILE` writes the summary, the flips (run ids and routing reasons), every row that could be routed and the first error. No model, no tokens. Only `core/routing.py` comes from the ref; the modules it imports (`doccheck`, `config`, `diffmap`) are the checkout's, so a change there shows up on both sides and never as a flip. Both sides use the default routing settings, not your `review.routing` configuration. If any diff cannot be routed it prints the first error type to stderr and exits non-zero, and an unusable ref exits with a one-line message. It says whether routing changed, not whether the change was right: that needs the reviewers' findings on those runs. The counts depend on your store.
