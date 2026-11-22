# 0041 Outcomes are collected at cleanup and a verdict takes its thread's role
Status: accepted 2026-11-21
Decision: With memory.collect on, cleanup collects PRs 1, 3, 7 and 14 days after review (3 PRs, 10 seconds per call, stop 7 days after close) and consolidates. A verdict takes the best-weighted role among the thread's replies, self when none.
Why: Cleanup ends every skill. Verdicts carried no role, so disputes never activated. Collection reads no memory, so blind runs may do it.
Rejected: A daemon or loop.py hook; a role field on verdict records, which old records lack.
