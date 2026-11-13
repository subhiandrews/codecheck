# Skip re-review when nothing has happened since our last review (short design)

## Intent

When code-check has already reviewed someone else's PR, it should leave that PR alone until the author does something about the review: pushes a new commit, or replies on one of our threads. Until then it spends no tokens on it. Today "already reviewed" is only a local record (`review:<ref>` equals the reviewed head), so a fresh machine or wiped state re-reviews a PR we already reviewed, reviews with no inline comments are invisible to the check, and replies to our comments are never looked at on the review side.

## Rule

Take our latest submitted review on the PR from GitHub. Since it was posted:

| New commits | Replies from others on our threads | Result |
|---|---|---|
| yes | any | review again |
| no | yes | do not review on its own. Unattended (`loop`): surface it ("author replied on 2 threads, no new commits since your review"). Interactive (`review-pr <PR>`): show the replies, recommend yes or no, ask whether to re-review with the replies as context |
| no | no | do not review; skip quietly ("already reviewed this head") |

`--force` overrides all of it. Unchanged: drafts, closed PRs, own PRs, ignore patterns.

## Goals / non-goals / anti-goals

- Goal: no LLM spend on a PR with no new commits, decided from GitHub, not only local state.
- Goal: replies are visible to the user without triggering a review.
- Non-goal: judging whether a commit "addresses" a comment. Any new head counts, including a force-push or rebase (my call: the CLI cannot make that judgment exactly, and you did not want it guessed).
- Non-goal: replying to authors, or retracting a finding we already posted. That is `address-comments`' job on your own PRs.
- Non-goal: asking anything unattended. `loop` never prompts and never re-reviews on a reply alone.
- Anti-goal: our own replies, our own review, or bot comments counting as activity; a review that finished incomplete counting as done.

## Approaches considered

- A. Keep local state, add reply detection: smallest, but keeps the fresh-machine problem and cannot see reviews without inline comments.
- B. GitHub as source of truth (chosen): add `Provider.list_reviews`; local state becomes a shortcut.
- C. Hidden marker in our review body: still needs the reviews list, and breaks if the body is edited. B covers it.

## Design

**Provider.** Add `list_reviews(ref) -> list[Review]` to the `Provider` protocol, the GitHub adapter (the PR's reviews endpoint, paginated: author login, commit id, submitted_at, state) and `FakeProvider`. New `Review` dataclass in `models.py`. Same argument validation as the other adapter calls. Pending reviews (no `submitted_at`) are ignored.

**Core function** in `core/precheck.py` (pure, no I/O): `review_activity(reviews, threads, head_sha, me)` returns the latest own review (commit, time), `new_commits` (head differs from that review's commit), and `replies`: comments by someone other than `me` and not a bot, created after the review's time, on threads whose first comment is ours. Replies on resolved or outdated threads count.

**Wiring.**
- `precheck --mode pr` and `list_work._consider` (review mode) call `list_reviews` and `list_threads` when the local `review:<ref>` record is absent or equals the current head; when it holds a different head a new commit is certain and nothing extra is fetched. Result for replies-only: `proceed: false`, reason as above, plus a flat `replies` list of reply dicts (thread, path, line, author, body, created_at); `{threads, comments}` is the shape on `context --with-replies` output.
- `loop` reports replies-only PRs under `skipped` with that reason. The `review-pr` skill prints them as one line each and never reviews them.
- If we have no review on GitHub at all, behavior is today's (review unless local state says this head was reviewed).
- Local state `review:<ref>` stays, but a match with the current head no longer short-circuits: activity is still fetched so replies can be surfaced. A `review-incomplete:<ref>` marker equal to the head forces a retry without fetching.

**Replies-only, interactive path (added after review).** When `review-pr <PR>` is run with an argument and `precheck` reports replies-only:
1. The skill reads the replies (the CLI returns them with thread path, line and text, capped in count and length) and gives a one-line recommendation with its reason, then asks yes or no. Recommend re-review when a reply disputes a finding, gives evidence, or asks a question the code could settle; recommend no when replies are acknowledgements, thanks or "will fix". The recommendation is the skill's judgment, never the CLI's.
2. On yes: `precheck --force`, then `context` with a new `--with-replies` flag that writes the replies to `replies.md` in the run directory. Every reviewer prompt in `agentgen.py` reads it when present and treats it as untrusted data from the PR, not instructions (regenerate `agents/*.md`). It is a full panel run on the current head; findings already posted are dropped by the existing already-posted check, so the net effect is new findings only.
3. On no: stop with the one-line reason.

**Incomplete reviews.** `post` currently leaves `review:<ref>` unset when reviewers failed, so the next run retries. With GitHub as truth that review would look complete. So `post` also records `review-incomplete:<ref>` = head sha when incomplete (cleared on a complete post), and the check treats that head as not reviewed.

**Bots and self.** Ignore authors equal to `whoami()` and logins ending `[bot]`. GraphQL may return bot logins without the suffix; the builder should add `__typename` to the threads query (no change to its node count) and ignore `Bot`.

## Constraints

- Every subcommand still prints exactly one JSON document; skills, README and the static plugin tests stay in sync (`tests/test_plugin_static.py`, `tests/test_readme.py`).
- No new config key. Tests use `FakeProvider`; add contract tests for `list_reviews` and cases for: replies surfaced with text and caps, `--with-replies` writing `replies.md`, skills and agents in sync with the CLI flag, new commit with and without replies, replies only, nothing new, no GitHub review, own and bot replies ignored, incomplete review retried, `--force`.
- README: document the behavior in the review sections.

## Left to the builder

Whether to cap replies at N threads or N characters (suggest 20 threads, 1,000 characters each, scrubbed like other PR text), pagination approach for the reviews endpoint, exact JSON shape of the `replies` field, and the wording of skip reasons.
