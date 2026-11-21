# 0042 Memory evals are advisory and report retrieval and effect separately
Status: accepted 2026-11-21
Decision: eval --memory-retrieval (no model) scores recall@5 and MRR per retriever, eval --memory-effect pairs live snippets with memory off and on, and memory report compares shadow outcomes; none gates anything or enters the history.
Why: Retrieval quality, effect on findings and field outcomes answer different questions, are noisy and small, and a gate would reward tuning to the cases (decision 0023).
Rejected: Gating commits or CI on memory scores; one blended memory score; random assignment of live reviews to a memory arm.
