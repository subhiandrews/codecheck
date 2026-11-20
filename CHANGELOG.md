# Changelog

User-visible changes per release, newest first. Earlier releases: `git log`. The top entry must match `VERSION` (`tests/test_changelog.py`).

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
