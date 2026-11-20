# 0012 The host, not local state, says what was reviewed
Status: accepted 2026-11-13
Decision: Our latest submitted review on the host decides whether a head was already reviewed. Local state is only a shortcut.
Why: A fresh machine or wiped state would otherwise re-review, and reviews without inline comments were invisible.
Rejected: Local state with reply detection added; a hidden marker in the review body.
