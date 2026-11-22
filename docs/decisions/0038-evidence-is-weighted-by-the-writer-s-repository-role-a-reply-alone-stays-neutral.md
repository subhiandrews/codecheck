# 0038 Evidence is weighted by the writer's repository role; a reply alone stays neutral
Status: accepted 2026-11-21
Decision: Shadow records store pr_author, a role per reply (maintainer, author, contributor, self, unknown) and resolved_by_role. Role comes from the host's write-access check, else author association; the PR author is always author.
Why: A dispute from the PR author or a drive-by commenter is not independent evidence. Decision 0009 holds: a reply alone is neutral.
Rejected: Association only: MEMBER does not imply write access. Storing logins in lessons: roles carry the weight.
