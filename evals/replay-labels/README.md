# Replay labels

`hermetic-pr2.json` lists the 17 findings that code-check made, round after round, on
`subhiandrews/hermetic` PR 2 (a 1,200-line scaffold). Every one of them had a site in the first reviewed head
(`7ec839e`), so a single complete review could have reported all of them at once. `round` is when the PR's review
first reported it and `fixed_in` is the commit that resolved it. It is a label file only: the code stays in the clone.

```
.venv/bin/python -m codecheck.evals.replay from-git --clone ~/opensource/hermetic --repo subhiandrews/hermetic \
  --base 593ffc7ab13eeedf1e52130b884caa7f81746494 --labels evals/replay-labels/hermetic-pr2.json \
  --name hermetic-pr2 7ec839e fed3f54 2f1d453 > cases.json
.venv/bin/python -m codecheck.evals.replay run cases.json --dry-run     # rebuild and check, no model
.venv/bin/python -m codecheck.evals.replay run cases.json --budget 10   # live: costs tokens
```

The score's `by_round` shows how many of each round's findings one review found. The target is every round at the
first head. The three heads hold 17, 11 and 7 labelled findings: each later head leaves out what the PR had fixed.
