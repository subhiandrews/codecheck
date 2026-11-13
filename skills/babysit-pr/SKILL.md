---
name: babysit-pr
description: Babysit your own pull requests. Addresses new review comments (using the address-comments skill) and fixes failing CI checks that the PR caused, pushes, and re-requests review. With no argument it works every PR of yours that has new comments or failing checks, at most five per run and one at a time. Use under /loop to keep your PRs moving.
argument-hint: "[PR number | OWNER/REPO#N | URL]"
---

# babysit-pr

Keeps one of YOUR pull requests moving: review comments first, then failing CI.

`code-check` below means `"${CLAUDE_PLUGIN_ROOT}/bin/code-check"`. Every subcommand prints one JSON document.

## With a PR argument

1. Run `code-check checks --pr <PR>` and note `failing`, `attempts` and `max_attempts`.
2. Invoke the `code-check:address-comments` skill for the PR. It stops by itself with "nothing to address" when there are no actionable threads, and it creates the workspace (`<WS>`) the CI fixes below reuse. If the PR cannot be worked on (closed, fork-based, dirty tree), STOP and report that.
3. For each failing check, in order:
   a. Run `code-check attempt --pr <PR> --check <name>`. If `exhausted` is true, record "needs a human" and move on.
   b. Run `code-check check-log --pr <PR> --check <name>` and diagnose from the log.
   c. Decide whether THIS PR caused the failure. It did if the failure points at files the PR changed or at tests those changes affect. It did not if the log shows infrastructure, network, timeout or runner errors, or a test in code the PR never touched that fails the same way on the base branch. If it did not, change nothing and record "unrelated or flaky".
   d. If it did, fix it under `<WS>` (run `code-check workspace --pr <PR>` first if you do not have `<WS>` yet), commit with `code-check --cwd <WS> commit --message "fix(ci): <summary>" --paths <files...>`, then `code-check --cwd <WS> push --pr <PR>` (tests gate the push; handle a refused push as in address-comments).
4. Report ONE line: `<PR>: comments: <summary from address-comments>; CI: <n> fixed, <m> unrelated or flaky, <k> need a human`.

## Without an argument (queue mode)

Run `code-check loop --mode babysit`. It handles each of your PRs with new comments or failing checks in its own headless process, one at a time (the per-iteration cap comes from configuration). If `skipped` is true, say the previous iteration is still running. Otherwise print one compact line per entry of `results`. Do not touch any PR yourself in this mode.

## Rules

- Never ask questions when running unattended. Never force-push. Do not retry a check whose attempts are exhausted.
- A CI-only fix does not re-request review; re-requesting happens in address-comments when threads were handled.
