# 0001 Exact work is code, models only judge
Status: accepted 2026-11-12
Decision: A deterministic Python CLI does git and host calls, diff parsing, finding validation, anchoring, approval policy and state. Models only review, aggregate and weigh comments.
Why: What must be exact stays tested code and cannot drift with a prompt.
Rejected: Claude's built-in code-review skill; this ships its own engine.
