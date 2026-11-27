---
name: babysit-pr
description: Babysit your own pull requests. Runs the address-comments skill (which merges the base, addresses new review comments and fixes failing CI checks the PR caused).
argument-hint: "[PR number | OWNER/REPO#N | URL]"
---

# babysit-pr

Keeps one of YOUR pull requests moving: runs `address-comments` for it, and in repository mode loops over your PRs with the round limits below.

`code-check` below means `"@CODE_CHECK_BIN@"`. Every subcommand prints one JSON document.

## With a PR argument

1. Invoke the `$address-comments` skill for the PR. It merges the base branch into the PR branch when it is behind, addresses the review threads, and fixes or reruns failing CI checks (with the attempt limit). If the PR cannot be worked on (closed, fork-based, dirty tree), STOP and report that.
2. Report ONE line: its summary for the PR.

## Without an argument

Ask for a PR reference and stop until one is provided.

## Rules

- Rounds are counted per PR by the CLI (`loop.max_babysit_rounds`, default 5, one round per run that pushes). Unattended, `loop` leaves a PR at the limit to you, and also leaves one that has had a round and now has fewer than 3 open threads, all code-check Suggestions, with no failing check: it is listed under `skipped` ("ready for you to merge"). Report those lines as they are; never merge, and never retry a PR the loop skipped. A PR you name yourself (`babysit-pr <PR>`) is not limited: someone is watching.
- Never ask questions when running unattended. Never force-push. Do not retry a check whose attempts are exhausted.
