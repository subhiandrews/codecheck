# Review attribution footer and `code-check` label

Status: approved in conversation 2026-11-13. Size: small change.

## Intent
Make code-check reviews identifiable without being noisy. The top-level review comment carries one small credit line naming the tool, its licence and the repo. PRs that received a review get a `code-check` label so they can be filtered.

## Goals
- Summary body (`core/render.py::summary_body`) ends with a visible line, just before the hidden run marker:
  `<sub>Reviewed with [code-check](https://github.com/subhiandrews/codecheck) · Apache-2.0</sub>`
- Added on every posted review, including re-reviews at a new head sha (each is a separate comment).
- After a successful post, add label `code-check` to the PR. Idempotent: re-adding is a no-op.
- Config (user-level only): `review.attribution = true`, `review.label = "code-check"`. `attribution = false` removes the footer; empty `label` disables labeling.

## Non-goals / anti-goals
- No credit in inline threaded comments (`comment_body` unchanged), the local-mode report, or address/babysit replies.
- No badge or image.
- A label or footer failure must never fail or alter the posting of the review.

## Constraints
- `review.attribution` and `review.label` are added to `REPO_FORBIDDEN` so a repo's `.code-check.toml` cannot suppress the credit or make the tool apply arbitrary labels. The label name is validated like other argv-bound provider inputs (no leading `-`, restricted charset).
- Labeling is skipped when nothing is posted: draft PRs, stale head, own PR, local mode.
- Labeling is best-effort (the reviewer may lack triage rights); `post` output gains `labeled: true|false`.
- Skills and README must stay in sync with config keys and CLI output (`tests/test_readme.py`, `tests/test_plugin_static.py`).

## Approaches considered
1. **Footer in `summary_body` + new `Provider.add_label` via `gh api` (chosen).** Smallest surface; follows the existing provider/fake pattern.
2. Put the credit into `post_review` in the provider. Rejected: rendering belongs in `core/render`, and it would also touch the body-only fallback path inconsistently.
3. Label from the skill via a new subcommand. Rejected: adds a command and a second step the LLM could skip; `post` already owns the posting side effects.

## Left to the builder
- Exact `gh` call (`POST repos/{repo}/issues/{n}/labels` with `labels[]`), error handling, and the `FakeProvider` addition.
- Wording of README and config documentation; tests for render, post (posted/draft/stale/label failure), config validation and repo-forbidden keys.
