# 0010 Replay gates every commit, live eval is manual
Status: accepted 2026-11-13
Decision: `eval --replay` runs on every commit and in CI. `eval --live` runs only on request, via `scripts/gates.sh --pr`, and is skipped when no model is reachable.
Why: Replay is free and deterministic. Live runs cost tokens and vary, so they match tolerantly against a recall bar.
Rejected: Live eval in CI: runners may lack model access and fork PRs get no secrets.
