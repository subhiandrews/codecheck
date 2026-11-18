# Testing code-check on GitLab

This is the manual test plan for the GitLab provider and for installing the plugin in the three host setups: **GitHub only**, **GitLab only** and **both**. The unit tests cannot cover it: they run against fixtures written from GitLab's documented API, and the real `glab` CLI has never been run against them. Part 4 lists exactly what is still unverified.

Everything here is safe to repeat. The sandbox is a throwaway public project, and every install test uses its own temporary data and Claude config directories, so your real installation is not touched.

## 0. What you need

| Need | Why |
|---|---|
| Two GitLab.com accounts, **A** and **B** | `review-pr` skips merge requests you opened yourself, so account B has to open the ones A reviews. A runs code-check. B only opens merge requests and leaves comments. |
| `glab`, logged in as A (`glab auth login`) | The provider shells out to it. |
| A personal access token for B with scopes `api` and `write_repository`, in `GITLAB_TOKEN` | B acts through the token only. Do not `glab auth login` as B: that would replace A's login. |
| `claude`, Node 20+, Python 3.14.10+, `git`, and a git identity (`user.name`, `user.email`) | The installer, the CLI and the seed commits. |
| A GitHub repository you can read | For the GitHub regression checks. This repository works. |

Review runs cost real tokens: budget a few dollars for Part 3.

## 1. Create the sandbox

Run these from the repository root. The script changes nothing on GitLab until you run `create`.

```bash
S=scripts/gitlab-sandbox/sandbox.sh
bash $S create                         # as A: public project, seed code, issues #1 and #2, four branches
bash $S add-member <B's username>      # as A: B becomes a Developer
bash $S open-mrs owner                 # as A: opens the MRs for address-comments and babysit-pr
GITLAB_TOKEN=<B's token> SANDBOX_PROJECT=<A>/code-check-sandbox bash $S open-mrs reviewer          # as B
GITLAB_TOKEN=<B's token> SANDBOX_PROJECT=<A>/code-check-sandbox bash $S comment-as-reviewer        # as B
bash $S status
```

What you get:

| Merge request | Opened by | Used for | What is in it |
|---|---|---|---|
| `feature/discount` | B | `review-pr` | `apply_discount` with a mutable default argument, a bare `except` that returns 0, no validation and float money |
| `feature/clean-docs` | B | `review-pr` with auto-approve | a one-line README change; nothing to find |
| `feature/report-export` | A | `address-comments` | CSV export; B leaves three threads: a valid one (`newline=""`), a nit (no header row) and a **wrong claim** (`sum` of ints "raises a TypeError") |
| `fix/failing-ci` | A | `babysit-pr` | `//` instead of `/` in `average`, so the pipeline fails; B leaves a thread about it |
| issue #1 | | the issue-fix scenario | `total_between` leaves out the end index |

Wait for the first pipelines to finish (a minute or two). `main`, `feature/discount`, `feature/clean-docs` and `feature/report-export` should be green and `fix/failing-ci` red; the repository's tests assert the same on the seed.

If `git push` asks for a password, set `GITLAB_TOKEN` (A's token) or `SANDBOX_USE_SSH=1`.

## 2. Plugin install: three cases, three routes

### 2.1 Isolation

Do not test against your real install. In a fresh shell:

```bash
export CODE_CHECK_HOME="$(mktemp -d)" CLAUDE_CONFIG_DIR="$(mktemp -d)"
STUBS="$(mktemp -d)"
printf '#!/bin/sh\necho "not found" >&2\nexit 127\n' > "$STUBS/missing" && chmod +x "$STUBS/missing"
```

To make a CLI look absent, put a stub with its name first on `PATH`:

```bash
hide() { for t in "$@"; do ln -sf "$STUBS/missing" "$STUBS/$t"; done; PATH="$STUBS:$PATH"; }
```

`hide glab` gives the GitHub-only machine, `hide gh` the GitLab-only one, and `hide gh glab claude` forces copy mode. Open a new shell between cases.

### 2.2 The cases

| | Machine has | Installer | Repository you review |
|---|---|---|---|
| **A: GitHub only** | `gh`, no `glab` | `install` (the default is `--provider github`) | a GitHub clone |
| **B: GitLab only** | `glab`, no `gh` | `install --provider gitlab` | the sandbox clone |
| **C: both** | `gh` and `glab` | `install --provider both` | one of each |

### 2.3 Routes

1. **Node installer, plugin mode**: `node installer/cli.js install [--provider ...]` with `claude` on `PATH`.
2. **Node installer, copy mode**: the same with `hide claude`. Skills and agents are copied into `$CLAUDE_CONFIG_DIR`.
3. **Marketplace**: `/plugin marketplace add subhiandrews/codecheck` then `/plugin install code-check@code-check` inside Claude Code, with `CLAUDE_CONFIG_DIR` set. This route has no `--provider` option: only the CLI of the host you review on is needed. **It installs the released version, so a version bump (`VERSION`, `.claude-plugin/`) has to be released before PRs #38 to #40 can be tested this way.**

### 2.4 Checks for every case and route

| # | Step | Expected |
|---|---|---|
| 1 | `node installer/cli.js doctor [--provider ...]` | A: lines for `python, git, gh, gh auth, claude` and none for glab. B: `glab, glab auth` and none for gh. C: all of them. Exit 0 |
| 2 | Run `install` | Prints `installed (plugin mode)` or `(copy mode)`, plus `, gitlab` or `, both` for those cases. `$CODE_CHECK_HOME/receipt.json` has `"provider"` set to the chosen value |
| 3 | Run `install` a second time | Succeeds and leaves one install (no duplicate registration, no duplicate files) |
| 4 | `claude plugin list` (plugin mode) or `ls $CLAUDE_CONFIG_DIR/skills $CLAUDE_CONFIG_DIR/agents` (copy mode) | `code-check` enabled, or the four skills and the agents present; nothing outside `code-check`'s own names added |
| 5 | In a clone, `"$CODE_CHECK_HOME/plugin/bin/code-check" doctor` | The checks match the clone's host (see 2.5) |
| 6 | `"$CODE_CHECK_HOME/plugin/bin/code-check" context --mode local --skill review-local` on a branch with changes, then `cleanup` | Works, and the JSON mentions no host-specific error |
| 7 | Case switch: reinstall with a different `--provider` | Receipt updated; still one install |
| 8 | `node installer/cli.js uninstall` | Plugin unregistered or copied files removed, receipt gone, anything you added to `$CLAUDE_CONFIG_DIR` left alone |
| 9 | `node installer/cli.js install --provider nope` and `--provider` with no value | Exits 1 with `--provider must be one of github, gitlab, both`, nothing created |

### 2.5 Expected `code-check doctor` by case and clone

| Clone is on | A: GitHub only | B: GitLab only | C: both |
|---|---|---|---|
| GitHub | pass | **fails on `gh`** (required): expected | pass |
| gitlab.com | **fails on `glab`** (required): expected | pass | pass |

The failure message must name the missing tool, not just say "failed". Confirm that provider selection follows the remote: `code-check doctor` in the GitLab clone prints `glab` checks even though you never passed a provider. Then set `provider = "github"` in `~/.config/code-check/config.toml` (in the isolated `$HOME` if you use one) and confirm the same clone now checks `gh`, which proves an explicit choice is never overridden.

### 2.6 What this plan cannot show

- The marketplace route needs a published release, as noted above.
- Self-hosted GitLab is out of scope: it is neither detected nor tested.

## 3. Scenarios on the sandbox

Run these as account A, inside a clone of the sandbox project (`glab repo clone <A>/code-check-sandbox`), with the plugin installed. After each scenario, check the result on the merge request page **and** with the API calls shown, since the provider reads the API.

### S1. `review-pr` on a teammate's merge request

1. In Claude Code: `/code-check:review-pr <URL of the feature/discount MR>`.
2. **Expect:** inline discussions on `sandbox/pricing.py` (at least the mutable default and the bare `except`), one summary note ending in a hidden `<!-- code-check:review:<sha> -->`, no approval, and a label if `review.label` is set.
3. API check: `glab api projects/<A>%2Fcode-check-sandbox/merge_requests/<iid>/notes` shows the summary note; `.../discussions` shows `DiffNote` threads anchored to the added lines.
4. Run it again without changing anything. **Expect:** it skips with "already reviewed" and posts nothing.
5. As B, push a new commit to `feature/discount`. Run again. **Expect:** a new review of the new head, and the old threads are not duplicated.

### S1b. A clean merge request with auto-approve

`CODE_CHECK_REVIEW__AUTO_APPROVE=true`, then review the `feature/clean-docs` MR. **Expect:** a "no findings" summary and **A approves the head**: `.../approvals` lists A. A second run skips. Then withdraw the approval in the web UI and run again: it must review again (this checks that "unapproved" is read).

### S1c. Things that must be skipped

| Setup | Expected |
|---|---|
| Review A's own MR (`feature/report-export`) | "own PR: use address-comments instead" |
| Mark B's MR as draft, then review | skipped as a draft; nothing posted |
| Close the MR, then review | skipped as not open |

### S2. `address-comments`

Pre-condition: B ran `comment-as-reviewer`. Run `/code-check:address-comments <URL of feature/report-export MR>`.
**Expect:**
- It lists three unresolved threads.
- `newline=""` is fixed in a commit, and the thread is replied to and resolved.
- The header-row nit is either fixed or answered with a reason.
- **The wrong claim is not "fixed".** The reply pushes back with evidence (`sum` of ints is fine), and that thread stays unresolved.
- The push goes through only if the tests pass, and it is never a force-push.
- Review is re-requested from B: `.../merge_requests/<iid>` shows B in `reviewers`.
- Replies carry the `<!-- code-check:reply -->` marker.

### S3. `babysit-pr`

Run `/code-check:babysit-pr <URL of fix/failing-ci MR>`.
**Expect:** it reads the failed job's log (`glab api projects/<id>/jobs/<job>/trace` is what it fetches), restores `/`, pushes, the pipeline goes green, the thread is answered and resolved, and a second run finds nothing to do. A job marked `allow_failure` must not trigger a fix.

### S4. Fixing an issue end to end

In the clone, tell Claude: "Fix issue #1 from GitLab, add a test, push a branch and open a merge request." (Claude will use `glab issue view 1` and `glab mr create`.)
**Expect:** a branch with the fix and a test that fails before it, an MR whose description says `Closes #1`, and a green pipeline. Run `/code-check:review-local` before pushing and confirm it reports nothing it should have caught.

### S5. Unattended run

```bash
export GITLAB_TOKEN=<A's token>            # no interactive login, as on a server
code-check list-work --mode review --repos <A>/code-check-sandbox   # B's open MRs appear; A's own do not
code-check run-pr --mode review --pr <URL of feature/discount MR> --dry-run
code-check loop --mode review --repos <A>/code-check-sandbox --dry-run   # then once without --dry-run, with an isolated state_dir
```

**Expect:** correct work items, a dry run that prints the `claude -p` command without running it, and one loop iteration that reviews at most one MR at a time and records it under `loop:review:<ref>` in the state file. A merge request from a fork (B forks the project and opens one) is skipped unless `loop.review_forks` is on.

### S6. GitHub still works (regression)

In a clone of a GitHub repository (this one):

1. `code-check doctor`: the `gh` checks, as before.
2. `code-check precheck --mode pr --pr <number of an open PR that is not yours>`: proceeds or skips for the same reasons as on `main`.
3. `/code-check:review-local` on a branch: a normal review, nothing GitLab-related in the output.
4. `/code-check:review-pr <that PR URL>` posts one review to GitHub, exactly as before. Do this only on a PR where a review is welcome.

## 4. Assumptions to confirm against real `glab` and GitLab

The provider was written without being able to run `glab`. Each row says what to check and where it lives; anything that differs gets a failing test first (use `bash $S capture-fixtures tests/fixtures/gitlab <iid>` to save the real responses), then a fix in `src/codecheck/providers/gitlab.py`.

| # | Assumption | How to confirm | If it is wrong |
|---|---|---|---|
| 1 | `glab api` accepts `--header`, `--input -` and `--paginate` | S1 posts without an error | change `_send` / `_get` |
| 2 | `--paginate` prints either one array per page or one merged array | `capture-fixtures`: open `diffs.json` | `_docs` handles both; extend it if a third shape appears |
| 3 | `glab repo clone <repo> <dir>` clones into `<dir>` | S2 from a directory that is not a clone, or `code-check workspace` | `clone_argv` |
| 4 | GitLab writes `approved this merge request` and `unapproved this merge request` system notes | S1b: look at `notes.json` after approving and withdrawing | `_VOTES` |
| 5 | Diff versions carry `created_at` and `head_commit_sha`, newest first or not | `versions.json` | `list_reviews` (it sorts, so only the field names matter) |
| 6 | A discussion with only `new_path`, `old_path` and `new_line` anchors to an added line | S1 inline threads appear on the diff, not in the summary | `post_review` position fields |
| 7 | A refused anchor returns text matching `400`, `422`, `line_code` or `position` | force one (comment on a line outside the diff) and read the error | `_POSITION_REJECTED` |
| 8 | `POST .../approve` with `{"sha": ...}` approves only that head | S1b | `post_review` |
| 9 | `GET .../merge_requests/N/pipelines` lists pipelines of this MR | S3 finds the failing job | `get_checks` |
| 10 | `add_labels` creates a missing label | set `review.label` to a new name | `add_label` |
| 11 | `PUT` with `reviewer_ids` replaces the list (the provider sends the union) | S2 | `request_review` |
| 12 | `GITLAB_TOKEN` alone authenticates `glab api` | S5 on a machine with no `glab auth login` | docs and `doctor` |
| 13 | Resolving a discussion with `PUT .../discussions/<id>` and `{"resolved": true}` | S2 | `resolve` |
| 14 | `glab auth status` exits non-zero when logged out | run it logged out | `doctor` |

## 5. Clean up

```bash
SANDBOX_CONFIRM_DELETE=<A>/code-check-sandbox bash scripts/gitlab-sandbox/sandbox.sh teardown   # deletes the project
node installer/cli.js uninstall --purge-data                                                     # inside the isolated shell
```

Revoke B's token and delete the temporary directories. Record the result of each row of Parts 2 to 4 (pass, fail and a link or note) on the pull request that introduces the change.
