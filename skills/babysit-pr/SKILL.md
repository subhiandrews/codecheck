---
name: babysit-pr
description: Babysit your own pull requests. Runs the address-comments skill (which merges the base, addresses new review comments and fixes failing CI checks the PR caused). With no argument it works your open PRs in the repository you are in (or in every clone directly under the current directory) that have new comments or failing checks, at most five per run and one at a time. Use under /loop to keep your PRs moving.
argument-hint: "[PR number | OWNER/REPO#N | URL]"
---

# babysit-pr

Keeps one of YOUR pull requests moving: runs `address-comments` for it, and in repository mode loops over your PRs with the round limits below.

`code-check` below means `"${CLAUDE_PLUGIN_ROOT}/bin/code-check"`. Every subcommand prints one JSON document.

## With a PR argument

1. Invoke the `code-check:address-comments` skill for the PR. It merges the base branch into the PR branch when it is behind, addresses the review threads, and fixes or reruns failing CI checks (with the attempt limit). If the PR cannot be worked on (closed, fork-based, dirty tree), STOP and report that.
2. Report ONE line: its summary for the PR.

## Without an argument (repository mode)

Run `code-check loop --mode babysit` with the Bash tool's `run_in_background` option: an iteration runs up to `loop.max_prs_per_iteration` headless reviews of up to 30 minutes each, far longer than a foreground Bash call is allowed to take, and a killed iteration would leave work half done. Wait for the background command to finish, then read its JSON output. Never run a second `loop` while one is still running. It looks at your open PRs in the repository you are in, or in every clone directly under the current directory, and handles each one with new comments or failing checks in its own headless process, one at a time (the per-iteration cap comes from configuration). If `skipped` is the boolean `true`, say the previous iteration is still running. Otherwise print one compact line per entry of `results`, then one line per entry of the `skipped` list (`pr`, `reason`). Do not touch any PR yourself in this mode.

## Rules

- Rounds are counted per PR by the CLI (`loop.max_babysit_rounds`, default 5, one round per run that pushes). Unattended, `loop` leaves a PR at the limit to you, and also leaves one that has had a round and now has fewer than 3 open threads, all code-check Suggestions, with no failing check: it is listed under `skipped` ("ready for you to merge"). Report those lines as they are; never merge, and never retry a PR the loop skipped. A PR you name yourself (`babysit-pr <PR>`) is not limited: someone is watching.
- Never ask questions when running unattended. Never force-push. Do not retry a check whose attempts are exhausted.

## Feedback

After your final report, when a person is in the session, invoke the `code-check:feedback` skill with no argument: it asks at most once a day and is silent otherwise. If something in this run looked like a defect in code-check itself, record it first as that skill describes. Do none of this when `CODE_CHECK_UNATTENDED` or `CODE_CHECK_HEADLESS` is set.
