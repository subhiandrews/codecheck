# 0003 One headless process per PR for unattended runs
Status: accepted 2026-11-12
Decision: `run-pr` starts one headless `claude -p` process per PR, one after another. The user's session sees only a short summary.
Why: Each PR gets a fresh context, and remote agents use the same path.
Rejected: An in-session master subagent. Nesting works to depth 2, but a single code path won.
Evidence: docs/spikes/2026-11-12-headless-and-plugin-mechanics.md
