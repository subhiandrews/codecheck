# 0045 The memory service ships before its payoff is measured
Status: accepted 2026-11-22
Decision: The memory server, sync, levels and semantic retrieval ship now, narrowing 0036's rejection of building a service first. Memory stays advisory and never drops or re-rates a finding.
Why: The owner chose to build it. The server is opt-in and off by default, and the effect evals are advisory. The PR calls the benefit unproven; the measurement tooling ships with it.
Rejected: Waiting for a measured payoff; default-on memory.
Evidence: src/codecheck/memory/measure.py
