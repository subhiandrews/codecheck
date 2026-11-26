# 0054 Reviewers are not told again to anchor on changed lines
Status: accepted 2026-11-25
Decision: Keep the reviewer prompts as they are: they already say file and line must be a changed line. No extra prompt pushing findings onto changed lines.
Why: The 12% of findings that cannot anchor mostly concern unchanged code the change affects, and they still appear in the review summary. No harm was measured (evals/deepeval/notes.md, F6).
Rejected: A stronger anchoring instruction: it would trade away findings about unchanged code to move a count that has not been shown to hurt authors.
