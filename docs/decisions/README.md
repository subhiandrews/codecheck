# Decision records

Why code-check is the way it is, as a timeline. `docs/ARCHITECTURE.md` and the code describe the current state; these records say why a choice was made and what was rejected. When they disagree, the code wins unless the code is the bug.

**Rules**
- One decision per record, at most **100 words** in the whole file (the `Status` line is not counted).
- Records are append-only. To reverse a decision, add a new record with `--supersedes`; never edit an accepted one.
- Evidence of how an external tool behaves lives in [`docs/spikes/`](../spikes/) and can be linked with `Evidence:`.

**Add one** (refuses more than 100 words and writes nothing):

```bash
.venv/bin/python scripts/decisions.py new --title "Short title" --decision "What we chose." --why "The reason." --rejected "What we did not."
.venv/bin/python scripts/decisions.py new ... --dry-run      # only report the word count
.venv/bin/python scripts/decisions.py check                  # validate working-tree records
.venv/bin/python scripts/decisions.py check --staged         # validate the Git index (pre-commit)
```

Records dated before 2026-11-20 were reconstructed from the original design specs, checked against the code, and carry the spec's date.

## Index
- [0001-exact-work-is-code-models-only-judge.md](0001-exact-work-is-code-models-only-judge.md): Exact work is code, models only judge
- [0002-an-opus-aggregator-verifies-a-cheap-reviewer-fan-out.md](0002-an-opus-aggregator-verifies-a-cheap-reviewer-fan-out.md): An Opus aggregator verifies a cheap reviewer fan-out
- [0003-one-headless-process-per-pr-for-unattended-runs.md](0003-one-headless-process-per-pr-for-unattended-runs.md): One headless process per PR for unattended runs
- [0004-approval-is-decided-in-code.md](0004-approval-is-decided-in-code.md): Approval is decided in code
- [0005-python-3-14-10-is-the-minimum.md](0005-python-3-14-10-is-the-minimum.md): Python 3.14.10 is the minimum
- [0006-run-in-shadow-mode-first.md](0006-run-in-shadow-mode-first.md): Run in shadow mode first
- [0007-hand-offs-are-read-from-the-session-transcript.md](0007-hand-offs-are-read-from-the-session-transcript.md): Hand-offs are read from the session transcript
- [0008-hand-off-runs-are-blind-to-existing-comments.md](0008-hand-off-runs-are-blind-to-existing-comments.md): Hand-off runs are blind to existing comments
- [0009-golden-cases-need-human-confirmed-labels.md](0009-golden-cases-need-human-confirmed-labels.md): Golden cases need human-confirmed labels
- [0010-replay-gates-every-commit-live-eval-is-manual.md](0010-replay-gates-every-commit-live-eval-is-manual.md): Replay gates every commit, live eval is manual
- [0011-skip-prs-already-approved-at-the-current-head.md](0011-skip-prs-already-approved-at-the-current-head.md): Skip PRs already approved at the current head
- [0012-the-host-not-local-state-says-what-was-reviewed.md](0012-the-host-not-local-state-says-what-was-reviewed.md): The host, not local state, says what was reviewed
- [0013-replies-alone-never-trigger-an-unattended-re-review.md](0013-replies-alone-never-trigger-an-unattended-re-review.md): Replies alone never trigger an unattended re-review
- [0014-repository-config-cannot-set-security-sensitive-keys.md](0014-repository-config-cannot-set-security-sensitive-keys.md): Repository config cannot set security-sensitive keys
- [0015-the-security-reviewer-is-a-dedicated-opt-in-agent.md](0015-the-security-reviewer-is-a-dedicated-opt-in-agent.md): The security reviewer is a dedicated opt-in agent
- [0016-code-check-md-is-read-from-the-base-branch.md](0016-code-check-md-is-read-from-the-base-branch.md): CODE_CHECK.md is read from the base branch
- [0017-only-the-user-can-turn-off-the-credit-line-or-label.md](0017-only-the-user-can-turn-off-the-credit-line-or-label.md): Only the user can turn off the credit line or label
- [0018-reviewers-see-every-existing-comment-on-the-pr.md](0018-reviewers-see-every-existing-comment-on-the-pr.md): Reviewers see every existing comment on the PR
- [0019-the-aggregator-keeps-the-highest-verified-severity.md](0019-the-aggregator-keeps-the-highest-verified-severity.md): The aggregator keeps the highest verified severity
- [0020-prompts-change-only-when-a-live-snippet-fails.md](0020-prompts-change-only-when-a-live-snippet-fails.md): Prompts change only when a live snippet fails
- [0021-hand-offs-record-the-prompt-build.md](0021-hand-offs-record-the-prompt-build.md): Hand-offs record the prompt build
- [0022-unposted-reviews-are-still-compared.md](0022-unposted-reviews-are-still-compared.md): Unposted reviews are still compared
- [0023-the-llm-judge-is-advisory-only.md](0023-the-llm-judge-is-advisory-only.md): The LLM judge is advisory only
- [0024-codex-reviewers-are-read-only-processes.md](0024-codex-reviewers-are-read-only-processes.md): Codex reviewers are read-only processes
- [0025-codex-runs-only-on-measured-platforms.md](0025-codex-runs-only-on-measured-platforms.md): Codex runs only on measured platforms
- [0026-synthetic-cases-keep-the-lesson-and-drop-the-code.md](0026-synthetic-cases-keep-the-lesson-and-drop-the-code.md): Synthetic cases keep the lesson and drop the code
- [0027-the-leak-check-exempts-only-listed-terms.md](0027-the-leak-check-exempts-only-listed-terms.md): The leak check exempts only listed terms
- [0028-routing-is-decided-in-code-from-the-diff.md](0028-routing-is-decided-in-code-from-the-diff.md): Routing is decided in code from the diff
- [0029-the-docs-reviewer-is-skeptical-and-capped-per-pr.md](0029-the-docs-reviewer-is-skeptical-and-capped-per-pr.md): The docs reviewer is skeptical and capped per PR
- [0030-each-released-version-has-a-changelog-entry.md](0030-each-released-version-has-a-changelog-entry.md): Each released version has a changelog entry
- [0031-the-aggregator-confirms-the-docs-cap-against-computed-metrics.md](0031-the-aggregator-confirms-the-docs-cap-against-computed-metrics.md): The aggregator confirms the docs cap against computed metrics
- [0032-deepeval-metrics-are-advisory-and-optional.md](0032-deepeval-metrics-are-advisory-and-optional.md): DeepEval metrics are advisory and optional
- [0033-the-docs-reviewer-has-no-built-in-comment-cap.md](0033-the-docs-reviewer-has-no-built-in-comment-cap.md): The docs reviewer has no built-in comment cap
- [0034-the-aggregator-s-reasoning-goes-in-rated-because-not-the-posted-message.md](0034-the-aggregator-s-reasoning-goes-in-rated-because-not-the-posted-message.md): The aggregator's reasoning goes in rated_because, not the posted message
- [0035-missing-findings-files-are-retried-once-not-treated-as-empty.md](0035-missing-findings-files-are-retried-once-not-treated-as-empty.md): Missing findings files are retried once, not treated as empty
- [0036-a-bot-keeps-the-ci-code-check-pin-current.md](0036-a-bot-keeps-the-ci-code-check-pin-current.md): A bot keeps the CI code-check pin current
- [0037-nested-code-check-md-files-are-chosen-by-the-changed-paths.md](0037-nested-code-check-md-files-are-chosen-by-the-changed-paths.md): Nested CODE_CHECK.md files are chosen by the changed paths
- [0038-guidance-learning-is-opt-in-local-and-never-commits.md](0038-guidance-learning-is-opt-in-local-and-never-commits.md): Guidance learning is opt-in, local and never commits
- [0039-review-pr-without-an-argument-reviews-the-repo.md](0039-review-pr-without-an-argument-reviews-the-repo.md): review-pr without an argument reviews the repository's open PRs
- [0040-loops-discover-clones-under-the-working-directory.md](0040-loops-discover-clones-under-the-working-directory.md): Loops discover clones under the working directory
- [0041-review-pr-proposes-guidance-as-a-patch-for-a-separate-pr.md](0041-review-pr-proposes-guidance-as-a-patch-for-a-separate-pr.md): review-pr proposes guidance as a patch for a separate PR
- [0042-report-repeatable-patterns-once-with-every-site.md](0042-report-repeatable-patterns-once-with-every-site.md): Report repeatable patterns once with every site
- [0043-check-that-a-fix-covers-the-whole-failure.md](0043-check-that-a-fix-covers-the-whole-failure.md): Check that a fix covers the whole failure
- [0044-sweep-broken-invariants-by-route-and-fix-them-at-one-boundary.md](0044-sweep-broken-invariants-by-route-and-fix-them-at-one-boundary.md): Sweep broken invariants by route and fix them at one boundary
- [0045-late-review-rounds-post-suggestions-only-on-new-lines.md](0045-late-review-rounds-post-suggestions-only-on-new-lines.md): Late review rounds post suggestions only on new lines
- [0046-a-shared-bot-login-is-ours-only-with-our-marker-at-the-end.md](0046-a-shared-bot-login-is-ours-only-with-our-marker-at-the-end.md): A shared bot login is ours only with our marker at the end
- [0047-address-comments-reviews-its-own-fixes-once-before-pushing.md](0047-address-comments-reviews-its-own-fixes-once-before-pushing.md): address-comments reviews its own fixes once before pushing
- [0048-address-comments-records-the-findings-its-threads-show.md](0048-address-comments-records-the-findings-its-threads-show.md): Address-comments records the findings its threads show
