# 0051 The address worktree is named per clone
Status: accepted 2026-11-24
Decision: workspace names its worktree addr-<repo>-<n>-<hash of the clone path>, so an interactive clone and the managed clone an unattended run uses never share one.
Why: A worktree left by one clone looked ahead of the PR head when the other clone ran workspace, which refused and stopped babysit before its CI step (#106).
Rejected: Fetching into the worktree's own repository and reusing it: it ties two clones together and still fails when either is gone.
