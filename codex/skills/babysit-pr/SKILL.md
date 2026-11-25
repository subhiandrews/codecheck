---
name: babysit-pr
description: Babysit your own pull requests. Addresses new review comments (using the address-comments skill) and fixes failing CI checks that the PR caused, pushes, and re-requests review.
argument-hint: "[PR number | OWNER/REPO#N | URL]"
---

# babysit-pr

Keeps one of YOUR pull requests moving: review comments first, then failing CI.

`code-check` below means `"@CODE_CHECK_BIN@"`. Every subcommand prints one JSON document.

## With a PR argument

1. Run `code-check checks --pr <PR>` and note `failing`, `attempts` and `max_attempts`.
2. Invoke the `$address-comments` skill for the PR. It stops by itself with "nothing to address" when there are no actionable threads; that stop does not end this run, so go on to step 3. It creates the workspace (`<WS>`) the CI fixes below reuse. If the PR cannot be worked on (closed, fork-based, dirty tree), STOP and report that.
3. For each failing check, in order:
   a. Run `code-check attempt --pr <PR> --check <name>`. If `exhausted` is true, record "needs a human" and move on.
   b. Run `code-check check-log --pr <PR> --check <name>` and diagnose from the log.
   c. Decide whether THIS PR caused the failure. It did if the failure points at files the PR changed or at tests those changes affect. It did not if the log shows infrastructure, network, timeout or runner errors, or a test in code the PR never touched that fails the same way on the base branch. If it did not, change no code: run `code-check rerun-check --pr <PR> --check <name>` once, so a merge rule that needs green CI is not stuck on a flake, and record "unrelated or flaky" (plus "rerun" when `rerun` is true, or the `reason` when it is false; never retry a refused rerun).
   d. If it did, fix it under `<WS>` (run `code-check workspace --pr <PR>` first if you do not have `<WS>` yet, and read its guidance as address-comments step 2 says), commit with `code-check --cwd <WS> commit --message "fix(ci): <summary>" --paths <files...>`, then `code-check --cwd <WS> push --pr <PR>` and allow up to 10 minutes for the configured tests to finish, as in address-comments.
4. Report ONE line: `<PR>: comments: <summary from address-comments>; CI: <n> fixed, <m> unrelated or flaky (<r> rerun), <k> need a human`.

## Without an argument

Ask for a PR reference and stop until one is provided.

## Rules

- Never ask questions when running unattended. Never force-push. Do not retry a check whose attempts are exhausted.
- A CI-only fix does not re-request review; re-requesting happens in address-comments when threads were handled.
