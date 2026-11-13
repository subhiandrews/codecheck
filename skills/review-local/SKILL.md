---
name: review-local
description: Review your own branch (committed and uncommitted changes) against the freshly fetched remote base using the code-check reviewer panel, and print the findings. Use when asked to review local changes, review my branch, or self-review before opening a PR. Never posts anything. Safe under /loop because it costs nothing while the work is unchanged.
argument-hint: "[--force] [security]"
---

# review-local

Reviews the current branch against `<remote>/<base>` (fetched first) and prints findings. It never posts to any pull request. Under `/loop` it spends no tokens while your work is unchanged.

`code-check` below means `"${CLAUDE_PLUGIN_ROOT}/bin/code-check"`. Every subcommand prints one JSON document.

## Steps

1. Run `code-check precheck --mode local` (add `--force` only if the user passed `--force`). If `proceed` is false, say the `reason` in one sentence and STOP. Do not read files or start agents.
2. Run `code-check context --mode local --skill review-local` (add `--security` only if the user passed `security`). Note `run_id`, `run_dir`, `panel.members`, `panel.languages` and `chunks`. If `base_warning` is set, tell the user the base may be stale.
3. Dispatch the panel in parallel: one message containing one Agent call per member of `panel.members`, with `subagent_type` `code-check:<member>`. Each prompt is only `RUN_DIR=<run_dir>`; for a `lang-<language>` member also add `Files you own: <panel.languages[language], comma-separated>`. If `chunks` has more than one entry, repeat this step once per chunk, one chunk after another, restricting each member to the files of that chunk.
4. Run `code-check ingest --run <run_id>`. If `incomplete` is true, remember which reviewers failed and continue.
5. Dispatch one Agent call with `subagent_type` `code-check:aggregator` and the prompt `RUN_DIR=<run_dir>`.
6. Run `code-check post --run <run_id>` and show its `text` field to the user verbatim. If `incomplete` is true, add one line naming `failed_reviewers`.
7. Run `code-check cleanup --run <run_id>` to remove the run directory, even when an earlier step failed.

## Rules

- Never ask the user questions mid-run, never edit files, and never post anywhere.
- Keep your own commentary to the final report. The reviewers' output lives on disk, not in this conversation.
