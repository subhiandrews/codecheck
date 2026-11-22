---
name: review-local
description: Review your own branch (committed and uncommitted changes) against the freshly fetched remote base using the code-check reviewer panel, and print the findings. Use when asked to review local changes, review my branch, or self-review before opening a PR. Never posts anything.
argument-hint: "[--force] [full] [security] [performance]"
---

# review-local

Reviews the current branch against `<remote>/<base>` (fetched first) and prints findings. It never posts to any pull request.

`code-check` below means `"@CODE_CHECK_BIN@"`. Every subcommand prints one JSON document.

## Arguments

`full`, `security`, `performance` and `--force` are independent keywords, accepted in either order; none is a path or PR reference.

Without `full`, `code-check context` routes the optional reviewers itself: it drops the architect for a clearly small change and adds the performance and docs reviewers when the diff calls for them (`panel.routing` says why). Never decide that yourself. `full` puts every reviewer on the panel, security and performance included, and skips routing.

## Steps

1. Run `code-check precheck --mode local` (add `--force` only if the user passed `--force`). If `proceed` is false, say the `reason` in one sentence and STOP. Do not read files or start agents.
2. Run `code-check context --mode local --skill review-local` (add `--full` only if the user passed `full`, `--security` only if the user passed `security`, and `--performance` only if the user passed `performance`). Note `run_id`. If `base_warning` is set, tell the user the base may be stale.
3. Run `code-check panel run --run <run_id>` and wait for its JSON result. If `failed_members` includes `aggregator` or the command errors, report the failure and go straight to cleanup. Otherwise continue and report any failed reviewers.
4. Run `code-check post --run <run_id>` and show its `text` field to the user verbatim. If `incomplete` is true, add one line naming `failed_reviewers`.
5. If the `context` output contains `handoff: true`, run `code-check handoff --run <run_id>` and ignore its output; otherwise skip this step.
6. Run `code-check guidance evidence`. If `enabled`, `proposable` and `changed` are not all true, skip this step. Otherwise improve the guidance for the directory it names, using only its `disputed` categories (findings the author's own verdicts called incorrect in several PRs) and `misses` (human review comments the panel did not match to a finding). Read `current` (null when there is no file yet) and write the whole new `CODE_CHECK.md`: keep every existing rule unless the evidence contradicts it, and add or sharpen at most a few rules, each a plain instruction to the reviewers (for a disputed category, say when it does not apply here; for a miss, say what to look for). Never put code, names, PR numbers, secrets or the evidence itself in the text. Pipe it into `code-check guidance apply --dir <dir> --digest <digest>`, then add one line to your report: `Updated <written> (review the change and commit it yourself)`. If `apply` errors, give its reason in one line and carry on.
7. Run `code-check cleanup --run <run_id>` to remove the run directory, even when an earlier step failed.

## Rules

- Never ask the user questions mid-run, never edit files (the one exception is `CODE_CHECK.md` through `code-check guidance apply` in step 8), and never post anywhere.
- Keep your own commentary to the final report. The reviewers' output lives on disk, not in this conversation.
