# 0002 An Opus aggregator verifies a cheap reviewer fan-out
Status: accepted 2026-11-12
Decision: Language experts (Haiku), architect, pragmatist and skeptic (Sonnet) review in parallel. An Opus aggregator verifies every Critical and Warning against the code before posting.
Why: Fan-out is cheap and the aggregator filters false positives. The risk is recall: it cannot recover a bug nobody reported.
Rejected: Deciding Haiku recall up front. Shadow data decides; models are configurable per role and language.
