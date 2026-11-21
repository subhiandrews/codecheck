# 0036 Memory is measured before it is built and stays advisory
Status: accepted 2026-11-21
Decision: A read-only command, memory measure, replays the shadow store in time order and counts how often earlier lessons would have applied. Any later memory is advisory data for reviewers, never code that drops or re-rates a finding.
Why: The store has few disputes and no roles, so the payoff is unknown. A wrong suppression is invisible; a wrong advisory lesson can be overruled.
Rejected: Building a memory service first; suppression in code; model-written lessons (the judge failed calibration, 0023).
Evidence: src/codecheck/memory/measure.py
