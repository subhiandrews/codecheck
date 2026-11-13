# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue or pull request.

Use GitHub's private reporting: open the repository's **Security** tab and choose **Report a vulnerability**. Include what you found, how to reproduce it, and the version or commit you tested.

You can expect an acknowledgement within a few days. Once a fix is ready we will credit you in the release notes unless you prefer not to be named.

## What counts

code-check reads untrusted pull-request content and runs `git`, `gh` and `claude` on your behalf, so the most important problems are the ones that let a pull request, a repository's configuration or a reviewer's output:

- run a command or pass an option to `git`, `gh` or `claude` that was not intended,
- read or write files outside the directories code-check is meant to use,
- change the rules a pull request is reviewed under (for example through `CODE_CHECK.md` on the pull request's own branch),
- cause a secret or personal data to be printed in a review comment or stored unscrubbed,
- push, approve or post when it should not (force-pushes, draft pull requests, your own pull requests).

## Supported versions

Only the latest release on `main` is supported while the project is at version 0.x.
