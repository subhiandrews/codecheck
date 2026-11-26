# 0058 The skeptic checks that tests reach the changed code
Status: accepted 2026-11-26
Decision: One sentence in the skeptic prompt: for each added or changed test, name the line where setup reaches the changed code, and any assertion another already guarantees.
Why: On 16 commits the existing skeptic and language experts together caught 4 of 23 human test comments (2 by the skeptic alone); with the sentence the skeptic caught 5 in one sample, with 45 findings against 37. Precision was not checked.
Rejected: A separate test-reviewer agent; its recall was not clearly better than the existing panel.
