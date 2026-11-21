# 0032 DeepEval metrics are advisory and optional
Status: accepted 2026-11-21
Decision: Keep deterministic DeepEval metrics in evals/deepeval behind an optional evals extra. They are no gate, not in CI and not in the plugin payload. Judge-based metrics stay out: the same-concern judge ordered pairs at 59-61% against 50% chance, and the code-validity judge scored 5 of 9 against a bar of 8.
Why: Replay and live remain the gates. The scorecard sees format drift the snippets cannot, and the judges failed bars fixed before the runs.
Rejected: Gating on the scorecard; shipping the judges; committing weak labels as a benchmark
