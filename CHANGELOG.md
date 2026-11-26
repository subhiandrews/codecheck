# Changelog

User-visible changes per release, newest first. Earlier releases: `git log`. The top entry must match `VERSION` (`tests/test_changelog.py`).

## [1.7.0]

### Added
- The babysit loop now limits rounds per PR. `loop.max_babysit_rounds` (default 5) counts the unattended runs that pushed to a PR; at the limit the PR is listed under `skipped` and left to you. After a round, a PR with fewer than 3 open threads, all code-check Suggestions, and no failing check is also skipped as ready for you to merge. `address-comments` now declines multi-line Suggestion fixes once a round has happened.

## [1.6.3]

### Fixed
- With `shadow.handoff` on, `cleanup` now writes the run's hand-off when the skill's own `handoff` step was skipped. About a quarter of `review-local` runs had lost their record that way, because `cleanup` deletes the run directory the hand-off is built from.

## [1.6.2]

### Fixed
- `shadow promote --run` on a local review now lists the findings its hand-off kept, so they can be labelled. Before, the draft had none.

## [1.6.1]

### Fixed
- `rerun-check` on GitHub no longer reruns a job that did not fail or time out (passed, cancelled or still running).

## [1.6.0]

### Added
- Consent-gated feedback: the new `feedback` skill files a GitHub issue about code-check only after you read the exact text and choose to post. It asks at most once a day and never when unattended; `/code-check:feedback <text>` reports on demand. Settings: `[feedback]` (user config only).

## [1.5.4]

### Added
- `babysit-pr` reruns a failing check it judges unrelated or flaky, once, so a PR is not left red for a merge rule that needs green CI. New command `rerun-check` (GitHub reruns that check's job, GitLab retries the job); a refused rerun is reported, not retried (#111).

## [1.5.3]

### Fixed
- An unattended `babysit-pr` run no longer stops at "worktree has local commits not in the PR head" because another clone of the repository left its address worktree behind: the worktree is now named per clone. Worktrees left under the old name (`addr-<repo>-<n>`) are not reused; remove them by hand (#106).
- A check that ran for two events (pull_request and push) is listed once in `checks` and counted once by `list-work`, so it no longer spends two of the three CI attempts (#106).
- `babysit-pr` no longer stops when `address-comments` has nothing to address, so a PR with only a failing check is still worked on (#106).
- `address-comments` now names failing checks in its report (#106).

## [1.5.2]

### Fixed
- Reviewers now write findings to their own named file, so language reviewers no longer lose findings to a misnamed file (#105).

### Added
- `ingest` lists `stray` findings files that belong to no panel member, so a misnamed file is visible.

## [1.5.1]

### Added
- `guidance evidence` prints `stats`: findings by outcome (confirmed, disputed, replied, no signal) and the dispute rate, overall and per category, as a baseline for judging a learned rule.

## [1.5.0]

### Added
- `threads` (the start of `address-comments`) records the code-check findings it sees in a PR's threads, including those a CI review posted as `review.ci_login` (new, user config only). Your verdicts on them now count as `guidance evidence` for your own repositories.

## [1.4.7]

### Changed
- `guidance evidence` now gives a `reason` when there is nothing to propose, for example when no `review-pr` run of the repository is recorded (local reviews and address-comments runs carry no evidence).

## [1.4.6]

### Changed
- `address-comments` (and `babysit-pr`) now runs `review-local` once on its fixes before pushing, fixes the valid findings in the same push and reports how many it fixed.

## [1.4.5]

### Added
- `review.seed_comments` (user config only): a file of earlier findings that a local review is shown, so an eval can replay a later round of a PR.

### Fixed
- In CI (`github-actions[bot]`), later review rounds now see their own earlier findings and replies.
- In CI, a finding already posted inline is no longer posted again.
- In CI, a PR the bot opened itself is no longer reviewed.

## [1.4.4]

### Changed
- When one rule is broken in several unlike places (a secret reaching output through a URL, a message and a header), reviewers report it once with every route, and `address-comments` fixes it at one boundary with a test through each route.
- From the third review of a PR, `review-pr` posts new Suggestions only on lines changed since its last review; Critical and Warning findings are still posted anywhere. Set `review.quiet_after_round = 0` to turn this off.
- `address-comments` declines suggestions whose fix adds its own logic or state unless the change is one line, so a fix gives the next review less to find.

### Fixed
- PR replay scoring credits a route a finding names by a short path, such as `plugin.py:41`.

## [1.4.3]

### Changed
- Reviewers report a repeatable problem once, naming every place it occurs, and `address-comments` checks that a fix covers the whole failure and adds no new gap of its own.

### Added
- Maintainer tooling to replay a real PR through the review flow and score the findings against what people and later review rounds found (`python -m codecheck.evals.replay`; see `docs/MAINTAINING.md`).

## [1.4.2]

### Fixed
- A failed `post` now shows GitHub's error message, not just "Unprocessable Entity (HTTP 422)".

## [1.4.1]

### Added
- `review-pr` can propose `CODE_CHECK.md` improvements learned from past reviews as a patch file for a separate PR. Off by default (`review.learn_guidance`, user config only).

## [1.4.0]

### Changed
- The loops find the clones at or directly under the directory you run from, so `~/opensource` works as well as `~/opensource/tokio`.
- `babysit-pr` with no argument now works on your open PRs in those clones instead of searching all your PRs on the host, and skips a clone with uncommitted changes.
- `review-pr` with no argument reviews the open PRs of the repository, newest first, three per run; a PR already reviewed waits for a new commit.
- Reviewers, `address-comments` and `babysit-pr` also read the repository's `CLAUDE.md`.

### Added
- New `review-prs` and `babysit-prs` skills: the loop forms of `review-pr` and `babysit-pr`.
- `review-local` can propose improvements to `CODE_CHECK.md` from what past reviews learned: findings you disputed in several PRs and human comments the panel missed. Off by default (`review.learn_guidance` in your user config); it writes the working tree only and never commits. New command `guidance evidence|apply`.

## [1.3.0]

### Added
- In a monorepo, a `CODE_CHECK.md` in a directory above a changed file is read as well as the root one, and applies only to files under that directory. `guidance` in the `context` and `workspace` output now lists the `files` read and any `skipped` for lack of room.
- The credit line in a posted review now names the code-check version that made it, for example `Reviewed with code-check v1.3.0 · Apache-2.0`.

## [1.2.3]

### Changed
- The performance reviewer is routed for what its prompt asks it to check: test files no longer earn a review, and blocking calls in async code, awaits in loops and list-membership tests in loops now count.
- Hand-off comparisons no longer count a human comment on a file no reviewer was given as a miss.

## [1.2.2]

### Fixed
- A reviewer that wrote no findings file is now re-dispatched once by `review-pr` and `review-local` instead of leaving the review incomplete (#64).
- A second `ingest` keeps the reviewers the first one found invalid.

## [1.2.1]

### Changed
- The aggregator prompt now says "the step 6 cap" where it said "the cap", so it no longer reads like the removed comment cap.

## [1.2.0]

### Changed
- The aggregator's severity reasoning is no longer posted in the review comment.
- Reviewers and the aggregator now lead with the claim and aim for about 400 characters.
- Codex aggregator output must include `rated_because` (null when unused).

### Removed
- The docs reviewer's built-in 3% comment cap, with `review.routing.max_comment_percent` and `docs_min_comment_lines`. Comment volume no longer routes the docs reviewer, which now runs only when a documentation file changes. It still flags comments that restate the code or narrate the change. Old config files that still set the two keys keep loading; the keys are ignored.

## [1.1.2]

### Changed
- Hand-offs: each finding now carries its scrubbed message (capped at 300 characters) and `handoff.md` shows the first 160, so a round can judge a finding without the raw run.

## [1.1.1]

### Changed
- Hand-offs: each human comment in the comparison now carries its scrubbed text, and a missed thread is listed with it in `handoff.md`.

## [1.1.0]

### Added
- Reviewer routing: `context` skips the architect for a clearly small change and adds the performance reviewer when the diff shows loops, queries in loops, unbounded reads or growth. The reasons are in `panel.routing`. Tune it with `[review.routing]`; `mode = "off"` restores the fixed panel.
- `full` argument on `review-pr` and `review-local` (`--full` on `context` and `run-pr`, `review.full`): every reviewer, security and performance included, with no routing.
- `doc-skeptic` reviewer (`models.docs`): holds added comments to 3% of added lines per PR and questions new or verbose documentation. Only an explicit rule in the base branch's `CODE_CHECK.md` overrides it.

### Changed
- The aggregator confirms the comment-cap finding against `docs-metrics.json` instead of recounting.
