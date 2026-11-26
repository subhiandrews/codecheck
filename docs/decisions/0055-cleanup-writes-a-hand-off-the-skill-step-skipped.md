# 0055 cleanup writes a hand-off the skill step skipped
Status: accepted 2026-11-25
Decision: cleanup builds the run's hand-off itself when shadow.handoff is on and none exists. The skills keep their step before cleanup; it now just writes the record earlier.
Why: The step says to ignore its output, and models drop it: 26 of 97 review-local runs recorded no hand-off, and cleanup deletes its only source.
Rejected: Removing the skill step: a model that runs it still gets the record earlier. Stronger wording: that step is what failed.
