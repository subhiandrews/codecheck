# 0053 Feedback is consent-gated, asked once a day, and queued when unattended
Status: accepted 2026-11-25
Decision: code-check records typed failure signals and writes a scrubbed draft; the feedback skill shows it and files a GitHub issue only on an explicit post. It asks at most once a day, never in headless runs, with snooze and never.
Why: Maintainers cannot see these defects. A public issue is permanent, so the user reads it first. A prompt every run is noise: once a day; headless runs have nobody to answer, so they queue.
Rejected: Posting automatically; asking per signal; model-only detection, unreliable for loops.
