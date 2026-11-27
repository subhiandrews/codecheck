# 0060 Address-comments merges the base and fixes failing CI
Status: accepted 2026-11-26
Decision: address-comments merges the PR's base branch into its worktree first (never a rebase) and fixes or reruns failing CI checks itself; babysit-pr only loops over it.
Why: One PR worked on its own got no CI fixes (#129), and reviewing code that conflicts with its base wastes the round. A rebase needs a force-push, which the tool never does.
Rejected: Keeping CI fixing in babysit-pr (a single PR could not be fixed without the loop); rebasing the branch (force-push).
