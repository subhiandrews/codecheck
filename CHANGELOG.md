# Changelog

User-visible changes per release, newest first. Earlier releases: `git log`. The top entry must match `VERSION` (`tests/test_changelog.py`).

## [1.3.0]

### Added
- Opt-in review memory (`memory.*`, off by default): lessons learned from how past reviews fared (human-caught misses, confirmed and disputed findings) are collected at cleanup and consolidated into a local store.
- With `memory.read` on, reviewers get misses and the aggregator gets confirm and dispute lessons as one framed run file; blind (hand-off) runs never read it, and evals run with memory off.
- A team can share lessons through `code-check memory serve` (stdlib HTTP, per-contributor tokens with a role ceiling, purge on revoke), `memory sync` and `memory status`.
- Optional semantic retrieval with a local Ollama model (`memory.embed_model`), falling back to exact keys on any failure; the result and `memory status` say why (`semantic: fallback: <reason>`).
- Advisory evals: `eval --memory-retrieval`, `eval --memory-effect` and `memory report`; none writes the eval history or gates anything.

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
