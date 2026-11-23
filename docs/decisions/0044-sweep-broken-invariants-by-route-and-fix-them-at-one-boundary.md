# 0044 Sweep broken invariants by route and fix them at one boundary
Status: accepted 2026-11-23
Decision: Reviewers list every route to a sink for a broken invariant and report it once. address-comments fixes it at one boundary with a test through every route, and declines suggestions whose fix adds its own logic.
Why: hermetic PR 8 took 13 rounds: 30 security findings were one rule (no secret in stored output), found one channel per round; suggestion fixes spawned new findings.
Rejected: More reviewer samples.
