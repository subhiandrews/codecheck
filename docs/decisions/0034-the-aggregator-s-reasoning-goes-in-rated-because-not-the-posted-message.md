# 0034 The aggregator's reasoning goes in rated_because, not the posted message
Status: accepted 2026-11-21
Decision: Aggregator findings carry an optional rated_because field, recorded in the hand-off and never posted. Step 4 sends severity reasoning there, and message reads as a reviewer's comment: claim first, about 400 characters, never truncated by code. No regex backstop; the scorecard measures process voice.
Why: Downgrade notes and first-person reasoning leaked into posted comments (2 of 13 kept messages, median length 517 against a human median of 165).
Rejected: Rewriting or rejecting messages by regex; truncating in code; a field on reviewer findings too
