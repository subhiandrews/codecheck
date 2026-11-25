# 0053 Feedback is consent-gated, asked once a day, and queued when unattended
Status: accepted 2026-11-25
Decision: code-check records typed failure signals and writes a scrubbed draft; the feedback skill shows it and files a GitHub issue only on an explicit post. It asks at most once a day, never in headless runs, with snooze and never.
Why: Maintainers learn of defects they cannot see, without posting user text or code anywhere unasked.
Rejected: Posting automatically; asking per signal; model-only detection, unreliable for loops.
