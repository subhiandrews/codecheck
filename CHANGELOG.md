# Changelog

User-visible changes per release, newest first. Earlier releases: `git log`. The top entry must match `VERSION` (`tests/test_changelog.py`).

## [1.2.0]

### Changed
- The aggregator's reasoning about a severity no longer goes into the posted comment. Aggregated findings carry an optional `rated_because` field, recorded in the hand-off and never posted. Reviewers and the aggregator are asked to lead `message` with the claim and aim for 400 characters or fewer. Codex aggregator output must now include `rated_because` (null when unused).

### Removed
- The docs reviewer's built-in 3% comment cap, with `review.routing.max_comment_percent` and `docs_min_comment_lines`. Comment volume no longer routes the docs reviewer, which now runs only when a documentation file changes. It still flags comments that restate the code or narrate the change. A repo that wants a limit writes it in its `CODE_CHECK.md`. Old config files that still set the two keys keep loading; the keys are ignored.

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
