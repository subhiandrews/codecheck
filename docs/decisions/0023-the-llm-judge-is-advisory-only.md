# 0023 The LLM judge is advisory only
Status: accepted 2026-11-15
Decision: The judge runs only through `eval --calibrate` and gates nothing.
Why: Its first calibration agreed with humans on 7 of 14 findings (kappa -0.32) against a bar of 0.8.
Rejected: Building the A/B comparison on the diff-only "worth raising" question.
