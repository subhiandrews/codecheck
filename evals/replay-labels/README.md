# Replay labels

`hermetic-pr2.json` lists the 17 findings that code-check made, round after round, on
`subhiandrews/hermetic` PR 2 (a 1,200-line scaffold). Every one of them had a site in the first reviewed head
(`7ec839e`), so a single complete review could have reported all of them at once. `round` is when the PR's review
first reported it and `fixed_in` is the commit that resolved it. `hermetic-pr6.json` does the same for PR 6 (26 findings in seven rounds, base `1b9acf3031254df3461d919eef6a0f3022f211f2`, heads `8495805 650b862 ae6e19d 02db36b ce3a32d b49a08f ee7be3b`). The `kind` calls (`tail` in particular) are the maintainer's judgement. It is a label file only: the code stays in the clone. The `kind` values and how the score reads them are in the "PR replay" section of `docs/MAINTAINING.md`.

```
.venv/bin/python -m codecheck.evals.replay from-git --clone ~/opensource/hermetic --repo subhiandrews/hermetic \
  --base 593ffc7ab13eeedf1e52130b884caa7f81746494 --labels evals/replay-labels/hermetic-pr2.json \
  --name hermetic-pr2 7ec839e fed3f54 2f1d453 > cases.json
.venv/bin/python -m codecheck.evals.replay run cases.json --dry-run     # rebuild and check, no model
.venv/bin/python -m codecheck.evals.replay run cases.json --budget 10   # live: costs tokens
```

`--raw-dir` and the `funnel` it adds are described in `docs/MAINTAINING.md`.

The target is every round at the first head (`by_round` in the score). The three heads hold 17, 11 and 7 labelled findings: each later head leaves out what the PR had fixed.
