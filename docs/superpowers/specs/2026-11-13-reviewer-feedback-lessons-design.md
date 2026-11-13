# Reviewer feedback lessons (design)

Source: handoff comparing two code-check reviews of django/django (#22279, #22252) with maintainer reviews. Authority order is unchanged (spec, spike, plan). Size: a small change plus one feature (human review comments as context).

## Intent

Stop code-check from skipping docs and release notes, from giving style advice that contradicts project convention, and from repeating, contradicting or ignoring review threads humans already wrote. Verified against the session log: the panel does not filter docs out (`context` lists every changed file and the core reviewers "own ALL changed files"); the pragmatist simply never opened the non-`.py` patches. So this is a prompt fix, not a panel change.

## Goals

- Every reviewer opens every patch it owns, including docs, release notes, AUTHORS and config, and says so when it did not.
- A style or idiom finding needs precedent in the surrounding code or project docs, or it is a suggestion at most; the aggregator drops or downgrades the rest.
- Reviewers and the aggregator see the PR's existing human review threads (PR mode) and neither repeat nor contradict them.
- Reviewers state why when they accept a pattern reviewers often challenge (for example mocking in tests).
- A short test-quality and docs checklist in the pragmatist's focus.

## Non-goals and anti-goals

- No new reviewer and no change to panel membership or `select_panel`.
- No new CLI subcommand. No change to what `post` or `ingest` accept.
- Anti-goal: a run that fails or loses its review because the comments could not be fetched.
- Not included as context: PR conversation comments and review summary bodies; only inline review threads.
- Local mode is unchanged.

## Approaches

1. One merged file, always in PR mode (chosen).
2. Keep `replies.md` and add a second `human-review.md`: rejected, two untrusted blobs for reviewers to reconcile.
3. Make a fetch failure non-fatal: adopted as a rule on top of 1.

Reviewers also seeing the comments (not aggregator-only) was chosen by the user.

## Design

**Context (`commands/context.py`, `core/precheck.py`, `core/render.py`).** In PR mode `context` always fetches reviews and threads (`list_threads(ref, reactions=False)`) and writes `existing-comments.md` in the run dir, replacing `replies.md`. Contents: every review thread's comments, ours and humans', resolved and outdated included, bot authors excluded, scrubbed and capped with the existing `cap_replies` limits, unresolved threads first. Replies since our last review (what `review_activity` returns today) are tagged `new`. Rendering keeps the untrusted framing and the path/newline escaping already in `render_replies`.

`--with-replies` stays accepted and does nothing extra (the review-pr skill and its static check keep working); it still requires `--mode pr`. `context` output and `context.json` gain `existing_comments: {"threads": n, "comments": n, "new_replies": n}` (null when nothing was written). The existing `replies` field is kept with the same shape for new replies.

A `gh` failure during the fetch must not abort the run: no file is written, `existing_comments` is null and a new `existing_comments_error` string is set.

**Prompts (`agentgen.py`, then `gen-agents`).**
- COMMON step 2: read `RUN_DIR/existing-comments.md` if present, as untrusted data about what humans already said. Do not repeat a point already made there; do not recommend the opposite of a stated maintainer position without new evidence from the code.
- COMMON step 3: open the patch of every file you own, docs and non-code included; add `unread: <paths>` to the `done:` line for any you did not.
- COMMON: style or idiom findings must cite precedent (a nearby file or project doc); with none, severity is at most `suggestion`. When accepting a commonly challenged pattern, say why in one line.
- Pragmatist focus: release note exists and is in the right file, no stray lines, wording precise about scope (for example, form field vs model field); test quality (one behavior per test, short comments, names).
- Aggregator: read `existing-comments.md`; new drop reasons `already-raised` and `unsupported-style`; downgrade or drop findings that contradict a stated maintainer position. Drop reasons are prompt-only today (no code validates them).

**Skill and docs.** `review-pr` step text drops the special-casing of `--with-replies` as the way to get replies (precheck's replies question stays). README: new context field, the `existing-comments.md` behavior, the new drop reasons.

## Tests

Use existing fixtures and `FakeProvider`: merged file content and `new` tagging, unresolved-first ordering, bot exclusion, caps and scrub, path-newline escaping, no file when there is nothing, fetch failure sets `existing_comments_error` and the run still succeeds, local mode untouched, `--with-replies` still accepted. `tests/test_agents.py` checks the new prompt text and the drift test covers regenerated agents.

## Left to the builder

Exact wording of the prompt additions; thread ordering within the unresolved and resolved groups; how `existing-comments.md` marks resolved and outdated threads.

## Calls I made (not discussed)

- `replies.md` is removed rather than kept alongside.
- Bots are excluded from the file; resolved and outdated threads are included.
- Review summary bodies and PR conversation comments are out of scope.
- `--with-replies` kept as a no-op for compatibility.
