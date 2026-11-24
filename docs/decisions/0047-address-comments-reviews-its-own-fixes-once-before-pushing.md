# 0047 address-comments reviews its own fixes once before pushing
Status: accepted 2026-11-24
Decision: address-comments runs review-local once on the committed fixes, acts only on findings on lines the fixes changed, fixes valid ones in the same push, and never repeats it.
Why: Fixes can introduce defects the next round finds. Of 221 later-round threads, 15% sat on fix-changed lines; a replay flagged about 60-70% of them early, at about 1 USD per run.
Rejected: Reviewing after every thread, or looping until clean: costs more and never ends. Not measured: churn from extra findings.
