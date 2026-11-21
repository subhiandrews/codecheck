# Changelog

User-visible changes per release, newest first. Earlier releases: `git log`. The top entry must match `VERSION` (`tests/test_changelog.py`).

## [1.2.2]

### Fixed
- A reviewer that found nothing could end without writing its findings file, which `ingest` reports as `missing`. The reviewer prompts now make that write the last tool call and say a missing file counts as a failed reviewer, and the `review-pr` and `review-local` skills re-dispatch each `missing` reviewer once and run `ingest` again (#64). A second `ingest` keeps the reviewers the first one found invalid instead of forgetting them.

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
