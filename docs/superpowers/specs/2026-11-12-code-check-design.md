# code-check: Design Spec

Date: 2026-11-12
Status: Draft for review

## 1. Summary

`code-check` is a Claude Code plugin (Python 3.14 core, thin Node installer) that reviews code and addresses review comments on pull requests. Everything mechanical (git, provider CLIs, fetching and posting comments, diff mapping, policy, loop bookkeeping) is deterministic, tested code. Agents are used only for judgment: reviewing code, evaluating review comments, diagnosing CI failures, and writing fixes.

It will first run in **shadow mode** alongside human reviewers. Shadow mode does not change behavior. It means the tool records enough data that human feedback can later be attached to its output, to improve the skills and build a golden eval set.

It deliberately does **not** use Claude's built-in code-review skill. It ships its own review engine.

### Phases

- **Phase 1:** Claude Code install; GitHub via the `gh` CLI.
- **Phase 2:** Codex install; GitLab via `glab`; Bitbucket (adapter approach decided then, since there is no official CLI).

### Phase 1 deferrals (interfaces and schemas defined, nothing built behind them)

- Deep reviewer implementation (slot only).
- Eval replay harness (export format and a small seed set only).

## 2. Skills

```
code-check/
├── .claude-plugin/{plugin.json, marketplace.json}   # name: "code-check"
├── skills/
│   ├── review-local/SKILL.md
│   ├── review-pr/SKILL.md
│   ├── address-comments/SKILL.md
│   └── babysit-pr/SKILL.md
├── agents/        # reviewer panel, aggregator, deep-reviewer slot, pr-master
├── src/codecheck/ # providers/, vcs/, core/, shadow/, cli.py
├── installer/     # Node: install / uninstall / doctor
└── tests/, tests-node/
```

| Skill | Behavior |
|---|---|
| `review-local` | Reviews the user's branch against the base branch, or uncommitted changes. Prints findings; does not post. |
| `review-pr` | Reviews a teammate's PR (never the user's own). Posts inline comments and applies the approval policy. With no argument it works the review-requested queue (max 5 per iteration); intended to pair with `/loop`. |
| `address-comments` | For the user's own PR: pulls actionable threads, critically evaluates each claim, fixes valid ones, replies with evidence to invalid ones, commits, pushes, re-requests review. |
| `babysit-pr` | `address-comments` plus CI handling, over the user's PRs in one or more repos. Pairs with `/loop`. |

`review-local` and `review-pr` share one engine (panel, aggregator, schema, posting code). They differ only in diff source and whether comments are posted. `babysit-pr` calls `address-comments` per PR; no logic is duplicated.

## 3. Deterministic core vs agentic judgment

Code (no LLM, unit-tested): precheck, context bundling, panel selection, diff-position mapping, finding IDs and dedupe, schema validation, posting, approval policy, git operations, loop work-listing and state, CI log fetching, config, telemetry.

Agents: reviewing, aggregating, evaluating review comments, writing fixes, diagnosing CI failures.

### Review flow (`review-local`, `review-pr`)

0. `sync` (no LLM): fetch the latest remote base branch before anything else (see section 8, Base sync), so every comparison is against current upstream.
1. `precheck` (no LLM): target exists, diff non-empty, PR not draft/closed, head SHA not already reviewed (unless `--force`), files not excluded by `ignore`. If nothing to do, exit with zero tokens spent.
2. `context`: bounded bundle (diff chunks, file list, languages, PR description).
3. `panel`: architect, pragmatist, skeptic always; language experts only for languages present in the diff (by extension or shebang); deep reviewer only if configured and reachable, otherwise skipped with one log line.
4. Reviewers run in parallel. Each returns findings validated against a JSON schema: `{file, line, severity, category, message, suggestion?}`. Language experts see only their own files.
5. Aggregator (Opus) dedupes, **verifies every Critical and Warning against the actual code**, recalibrates severity, drops what a linter would catch, caps inline comments (default 25, overflow summarized in the review body). Output uses the same schema plus reviewer attribution and drop reasons.
6. `post`: maps lines to diff positions, drops or relocates findings that cannot be anchored, posts one inline review, applies the approval policy.
7. `shadow record`.

### Severity and approval

Severities: `critical`, `warning`, `suggestion`. Comments are posted inline.

Approval is `review.auto_approve == true and no critical and no warning`. A draft PR is never approved. When not approving, the review is submitted as a comment. This logic lives in code, not prompts.

## 4. Reviewer panel and models

| Role | Model |
|---|---|
| Aggregator | Opus |
| Architect, pragmatist, skeptic | Sonnet |
| Language experts (15) | Haiku 4.5 |
| Deep reviewer (slot) | configurable |

Language experts: Python, JavaScript, TypeScript, Java, C#, C++, C, Go, Rust, PHP, Ruby, Swift, Kotlin, SQL, Shell.

Rationale for Haiku on language experts: fan-out is cheap and fast, and the Opus aggregator verifies Critical/Warning findings before posting, filtering false positives. The risk is recall, since the aggregator cannot recover a bug nobody reported. Mitigations: the Sonnet architect and skeptic read the whole diff; models are per-role and per-language configurable; shadow data records the producing model per finding so per-language recall and precision can be measured and a language bumped up a tier.

The installer writes models from config into agent frontmatter (Claude Code reads models from frontmatter).

## 5. `address-comments` protocol

1. `threads` returns only actionable threads: unresolved, not outdated, and not where the user replied last.
2. For each thread the agent classifies (bug, valid suggestion, preference, incorrect, question, out of scope), **verifies the claim against the code and runs relevant tests**, and returns a verdict with evidence.
3. Action: valid → fix; incorrect or partial → reply with evidence, no code change; unclear → ask; preference → follow repo conventions and say so.
4. Code then: commits in logical groups; runs the configured test command (failure blocks the push); pushes (never force); replies to each thread with the commit SHA or rationale; resolves only threads that were fixed (pushback threads are left for the reviewer); re-requests review.

Guardrails: refuses to run on a dirty tree it did not create; operates in a worktree where possible.

## 6. `babysit-pr` and CI

A failing check is a work item. Code fetches the failing check's log and extracts the failure. The agent diagnoses and fixes only if the PR caused it, then pushes. Flaky or unrelated failures (infra errors, tests the PR did not touch) are reported and left alone. After N failed attempts on the same check it stops and flags the PR for the user.

## 7. Loops and context hygiene

- Per iteration: at most 5 PRs (`loop.max_prs_per_iteration`), processed one at a time.
- `list-work` consults a state file (per PR: head SHA, last-seen comment ID, check state) and returns only PRs with something new. An idle iteration costs zero LLM tokens.
- **Decision:** `pr-master` is a headless `claude -p` process per PR, launched by `run-pr`. Each PR gets a fresh context and the user's session sees only a short JSON summary. This is also the remote-agent path (one code path).
- **Verify first (plan step 1):** Claude Code subagents are believed unable to spawn subagents, which would break an in-session master agent that fans out reviewers. A spike confirms this and the headless-per-PR mechanism before it is locked in. If the spike shows nesting works, the in-session master subagent is an acceptable alternative.

### Local loop (`review-local` under `/loop`)

`review-local` takes no PR number, so it can run in a loop on whatever is checked out. `precheck` computes a **fingerprint** of the work: merge-base SHA with the freshly fetched base, `HEAD` SHA, and a hash of the full diff including uncommitted changes. If it matches the last reviewed fingerprint, the iteration exits with zero LLM tokens. (This replaces the "head SHA already reviewed" check for `review-local`, which would miss uncommitted edits.) Findings have stable IDs; on re-review, previously shown findings are suppressed or marked unchanged so each tick shows only what is new. An optional settle check (fingerprint unchanged for N seconds) avoids reviewing half-written code. Local mode never posts and has no approval step; it prints findings or writes them to a file. With no base branch to diff against, or on the base branch with a clean tree, `precheck` reports "nothing to review".

## 8. Provider interface

CLI-first: Phase 1 uses the `gh` CLI only (no MCP). Phase 2 uses `glab`; Bitbucket decided then.

`providers/base.py` defines a `Protocol`:

| Method | Purpose |
|---|---|
| `list_prs(scope)` | Review-requested queue, or the user's open PRs, across one or more repos |
| `get_pr(ref)` | Metadata, base/head SHA, draft/closed state, author |
| `get_diff(ref)` | Diff normalized into hunks with provider-independent positions |
| `list_threads(ref)` | Threads: resolved, outdated, comments, authors |
| `post_review(ref, comments, decision)` | Inline comments plus approve/comment, atomically |
| `reply(thread, body)`, `resolve(thread)` | Replies and resolution |
| `request_review(ref, users)` | Re-request review after pushing |
| `get_checks(ref)`, `get_check_log(check)` | CI status and failure logs |
| `whoami()` | Current user (own vs teammate's PR) |

Adapters return only our dataclasses (`PullRequest`, `Thread`, `Finding`, `Check`); provider JSON never leaks past the adapter. Each adapter takes an injected `CommandRunner` (thin subprocess wrapper) so tests replay recorded output and assert on exact commands. Auth is whatever `gh auth` provides, or `GH_TOKEN` remotely; no credentials are stored. A shared contract test suite runs against every provider.

**Base sync.** Every skill run, local or remote, first fetches the latest remote base branch (`git fetch <remote> <base>`, plus the PR head ref for `review-pr`; fetch only, never pull or merge) and compares against `<remote>/<base>`, not the local base branch, using the merge-base. The user's local branches and working tree are never modified by a sync. Loops fetch every iteration (cheap, and the merge-base is part of the fingerprint, so a moved base can trigger a re-review). If the fetch fails (offline, auth), the run continues against the last-fetched ref but marks the output "base may be stale" with the ref's age; it never silently pretends to be current. `address-comments` and `babysit-pr` also check whether the PR branch is behind or conflicts with the fresh base and report it; they do not rebase or merge automatically.

`vcs/` handles base-branch resolution, merge-base diff, PR checkout into a worktree, commit, push, and dirty-tree/conflict detection. It never force-pushes.

## 9. Configuration

Merged in order: repo `.code-check.toml`, then `~/.config/code-check/config.toml`, then `CODE_CHECK_*` env vars.

```toml
provider = "github"
base_branch = "auto"
[review]
auto_approve = false
max_files = 80
ignore = ["**/*.lock", "dist/**"]
[loop]
max_prs_per_iteration = 5
repos = []
[models]
aggregator = "opus"
language_experts = "haiku"
reviewers = "sonnet"
[deep_reviewer]
enabled = false
[shadow]
enabled = true
store = "~/.local/share/code-check/shadow"
```

## 10. Shadow mode data

Shadow mode does not change behavior. It records an append-only, schema-versioned JSONL store, one file per run:

- `run`: run ID, skill, repo, PR, head SHA, panel members, models, tool version, config hash, timings, token counts where available.
- `finding`: stable ID (hash of file, normalized line context, message), reviewer attribution, severity, kept/dropped by aggregator, posted comment ID, diff snippet.
- `decision`: the approval decision and reason.
- `outcome` (written by `shadow collect`): per finding, resolved or not, author reply text, later code change at that location, reactions, whether a human independently raised the same issue. Unraised findings are false-positive candidates; human comments we never raised are misses.
- For `address-comments`: each comment, verdict, reasoning, action, commit SHA.

`shadow export-golden` turns collected outcomes into an eval set (diff plus human-confirmed expected findings, with labeled status). Secrets are scrubbed before storage; the store stays local unless moved.

## 11. Installation (Claude Code only in Phase 1)

1. **Marketplace (primary):** `/plugin marketplace add <owner>/code-check`, then `/plugin install code-check@code-check`. A `SessionStart` hook ensures the Python runtime (venv under the plugin data dir, via `uv` if present, else `python -m venv`) and installs the package. Auto-enabled.
2. **Outside Claude (npm):** `npx code-check install` places skills and agents under `~/.claude/`, sets up the venv, writes models into agent frontmatter, enables the plugin in `settings.json`. `uninstall` reverses it. `doctor` checks Python ≥ 3.14.10 (warns, does not block, below that; see section 15), `gh`, `gh auth`, `claude` on PATH, config, store location. The Node package does only filesystem placement and prerequisite checks.
3. **Remote/CI:** `pip install code-check` or a container image. `code-check run-pr --repo X --pr N` and `code-check loop --mode review|babysit` under `claude -p`. Auth via `GH_TOKEN` and `ANTHROPIC_API_KEY`; no interactive prompts; state and shadow store directories configurable (mount a volume).

## 12. Testing

- **Python (pytest):**
  - Base sync: fetch-before-compare ordering, comparison against the remote ref (not local base), fetch failure marking output stale, local branches untouched. Local-loop fingerprint: unchanged work is a no-op, uncommitted edits or a moved base change it.
  - Unit tests for policy and decision code: approval rules, severity handling, panel selection, loop skipping and capping.
  - Diff-position mapping: renames, deletions, multi-hunk files, missing trailing newline.
  - Finding IDs and dedupe, config merging, telemetry schemas.
  - `FakeRunner` replaying recorded `gh` output and asserting on exact commands.
  - Real git against temp repos.
  - Provider contract suite.
  - Property tests (hypothesis) for diff mapping and finding IDs.
- **Node (node:test):** installer in a fake `$HOME`: install, idempotent re-install, uninstall, doctor failure modes, settings.json merge without clobbering.
- **Skill/agent static checks (no LLM):** valid frontmatter, pinned models match config, every CLI command a skill references exists.
- **Evals (LLM, opt-in, not default CI):** replay harness deferred; export format and seed set only.
- CI runs pytest and node:test on every push, on Python 3.14.10 or later.

## 13. README outline

1. What it is, with a short workflow diagram.
2. Install: marketplace, npm, remote/CI (copy-paste blocks; prerequisites Python ≥ 3.14.10, `gh`, `claude`).
3. Quick start for the four skills with a realistic example each.
4. Configuration reference: full `.code-check.toml`, env vars, model-per-role table.
5. Loops (`/loop` examples) and headless/remote.
6. Safety and approval behavior: no force-push, tests gate pushes, pushbacks not auto-resolved.
7. Shadow mode: what is recorded, where, how to collect feedback and export the golden set.
8. Extending: adding a provider (contract suite), filling the deep-reviewer slot.
9. Troubleshooting via `doctor`.

## 14. Open items

- Spike result on subagent nesting vs headless `claude -p` per PR (plan step 1).
- Bitbucket adapter approach (Phase 2).
- Whether language-expert Haiku recall is acceptable (decided by shadow data, not up front).

## 15. Python version floor

Minimum supported Python is **3.14.10** (released 2026-10-25). Python 3.14.8 was itself an expedited security release (TLS and archive CVEs), but later patch releases fix further issues: 3.14.9 fixed a `tempfile.TemporaryDirectory` cleanup race (symlink attacks) and hardened tarfile filters; 3.14.10 fixed a `tarfile.tar_filter()` link-handling bypass, rejects compromised eMudhra intermediate CA keys in `ssl`, and ignores `http.cookiejar` cookies with negative `Version`. Source: python.org changelog (CVE IDs as reported there, not independently verified against NVD). `doctor` warns below the floor rather than blocking, since OS or CI images may lag. Revisit the floor when new 3.14.x releases ship.
