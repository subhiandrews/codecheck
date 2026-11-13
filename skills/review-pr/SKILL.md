---
name: review-pr
description: Review a teammate's pull request with the code-check reviewer panel and post one inline review (Critical, Warning, Suggestion), approving only when configured and clean. With no argument it works the queue of PRs awaiting your review, at most five per run and one at a time. Use for "review PR 123", or under /loop to keep reviewing teammates' PRs.
argument-hint: "[PR number | OWNER/REPO#N | URL]"
---

# review-pr

Reviews a teammate's PR (never your own) and posts a single review with inline comments. Approval is decided by the CLI from your configuration, never by you.

`code-check` below means `"${CLAUDE_PLUGIN_ROOT}/bin/code-check"`. Every subcommand prints one JSON document.

## With a PR argument

1. Run `code-check precheck --mode pr --pr <PR>`. If `proceed` is false, say the `reason` in one sentence and STOP.
2. Run `code-check context --mode pr --pr <PR> --skill review-pr`. Note `run_id`, `run_dir`, `panel`, `chunks`. If `base_warning` is set, mention it in your report.
3. Dispatch the panel in parallel: one message containing one Agent call per member of `panel.members`, with `subagent_type` `code-check:<member>`. Each prompt is only `RUN_DIR=<run_dir>`; for a `lang-<language>` member also add `Files you own: <panel.languages[language], comma-separated>`. With more than one chunk, repeat per chunk, one after another.
4. Run `code-check ingest --run <run_id>`.
5. Dispatch one Agent call with `subagent_type` `code-check:aggregator` and the prompt `RUN_DIR=<run_dir>`.
6. Run `code-check post --run <run_id>`. If `stale` is true the PR moved while you were reviewing: clean up (step 7) and run this skill once more from step 1; if it is stale a second time, stop and report that.
7. Always run `code-check cleanup --run <run_id>`, even when an earlier step failed.
8. Report ONE line: `<PR>: <decision>, <inline> inline comments, <unlisted> in the summary`, plus `(incomplete: <failed_reviewers>)` when applicable.

## Without an argument (queue mode)

Run `code-check loop --mode review`. It reviews each PR awaiting your review in its own headless process, one at a time (the per-iteration cap comes from configuration), so nothing accumulates in this session. If `skipped` is true, say the previous iteration is still running. Otherwise print one compact line per entry of `results` (`pr`, `ok`, `summary`). Do not review anything yourself in this mode.

## Rules

- Never ask questions when running unattended; the CLI refuses your own PRs, drafts, closed PRs and already-reviewed heads.
- Do not edit the PR's code; this skill only reviews.
