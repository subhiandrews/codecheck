# code-check

Review code and deal with review comments on pull requests, from inside Claude Code.

`code-check` is a plugin with four skills on top of a deterministic Python CLI. Everything mechanical (git, the `gh` CLI, fetching and posting comments, diff anchoring, the approval policy, loop bookkeeping) is ordinary tested code. Agents are used only for judgment: reviewing code, weighing review comments, diagnosing CI failures, writing fixes.

| Skill | What it does |
|---|---|
| `review-local` | Reviews your own branch (committed and uncommitted) against the freshly fetched remote base and prints the findings. Never posts. |
| `review-pr` | Reviews a teammate's PR and posts one inline review. With no argument it works your review-requested queue, at most 5 PRs per run, one at a time. |
| `address-comments` | On your own PR: checks each unresolved thread critically, fixes what is valid, pushes back with evidence on what is not, pushes behind a test gate, replies, resolves fixed threads, re-requests review. |
| `babysit-pr` | `address-comments` plus failing CI checks, across your PRs, designed to run under `/loop`. |

Findings are **Critical**, **Warning** or **Suggestion** and are posted inline on the changed lines. A panel of reviewers works on each change: up to 15 language experts (only for languages present in the diff), an architecture reviewer, a pragmatic reviewer and a skeptic, with an optional slot for a "deep" reviewer backed by your own knowledge sources. An Opus aggregator verifies every Critical and Warning against the code before anything is posted.

```
precheck (no LLM) → fetch latest base → context → reviewer panel (parallel) → ingest → aggregator → post (inline) → shadow record
```

If there is nothing to review, the run stops after `precheck` and spends zero tokens.

## Install

Prerequisites: Python **3.14.10 or later** (`doctor` warns below this, and the installer refuses to run on anything older than 3.14), `git`, the GitHub CLI `gh` (logged in with `gh auth login`), and Claude Code. `uv` is used if present.

### 1. From the plugin marketplace (recommended)

```text
/plugin marketplace add OWNER/REPO
/plugin install code-check@code-check
```

The plugin is enabled on install. On the next session start a hook creates a private virtual environment and installs the CLI into it (silent when already current).

### 2. Without going through Claude's marketplace UI (Node installer)

Three equivalent ways to run the installer. Use the first once the package is published, the second to try it straight from GitHub, the third from a clone of this repository:

```bash
npx code-check install                    # published package
npx github:OWNER/REPO install             # straight from GitHub, no clone needed
node installer/cli.js install             # from a clone (needs only Node 20+)
```

Each takes the same subcommands:

```bash
npx code-check install        # stage files, create the runtime, register the plugin with `claude plugin ...`
npx code-check doctor         # check prerequisites at any time
npx code-check uninstall      # add --purge-data to also delete shadow data and state
```

If the `claude` command is not on the PATH, or its plugin commands fail, the installer falls back to **copy mode**: it copies the skills and agents into `~/.claude/skills` and `~/.claude/agents`. Copy mode never overwrites anything that already exists there. If a skill or agent of the same name is present it is left untouched, a warning is printed and that item is skipped. The installer writes a receipt (`receipt.json` in the data directory) listing exactly what it created; `uninstall` removes only what the receipt recorded, plus the staged plugin and the runtime it owns. Restart Claude Code afterwards.

Overrides: `CODE_CHECK_HOME` (data directory, default `~/.local/share/code-check`) and `CLAUDE_CONFIG_DIR` (default `~/.claude`).

### 3. Remote agents and CI

```bash
pip install git+https://github.com/OWNER/REPO.git     # or a container image built from this repo
export GH_TOKEN=...            # gh authentication, no interactive login
export ANTHROPIC_API_KEY=...   # for the headless `claude -p` processes
code-check loop --mode review      # one iteration of the review loop
code-check loop --mode babysit     # one iteration of the babysit loop
code-check run-pr --mode review --pr OWNER/REPO#123
```

The `claude` binary must be installed and on the PATH (`run_pr.claude_bin`). Unattended runs have nobody to answer prompts, so they use `--permission-mode dontAsk --permission-prompts none` together with the allow-list in `run_pr.allowed_tools`. There is no `--max-turns` limit; `run_pr.timeout_seconds` (default 1800) is the bound on a single run.

Mount a volume at the data directory (`state_dir` and `shadow.store` in the config) so state and shadow data survive between runs. Nothing in the CLI prompts interactively.

## Quick start

```text
/code-check:review-local                      # review the branch you are on
/code-check:review-pr 123                     # review PR 123 of this repository
/code-check:review-pr                         # review everything awaiting your review (max 5)
/code-check:address-comments 456              # work through the comments on your PR 456
/code-check:babysit-pr                        # comments and CI for all your PRs with something new
```

A PR can be given as `123` (current repository), `OWNER/REPO#123` or a PR URL.

The CLI can also be used directly; every subcommand prints one JSON document:

```bash
code-check precheck --mode local          # is there anything to review? (no LLM)
code-check list-work --mode babysit       # which of my PRs have something new?
code-check doctor                         # prerequisites
```

## Configuration

Settings are layered, highest precedence first: environment (`CODE_CHECK_<SECTION>__<KEY>`, for example `CODE_CHECK_REVIEW__AUTO_APPROVE=true`), your `~/.config/code-check/config.toml`, the repository's `.code-check.toml`, then the defaults below. Your own settings therefore win over a repository's. Unknown keys and wrong types in files are errors.

A repository's `.code-check.toml` may **not** set the security-sensitive keys `state_dir`, anything under `[run_pr]` or `[shadow]`, `review.auto_approve` or `loop.review_forks`: a repository you review is not trusted to choose what an unattended run may do, where state is written, or whether reviews approve. Such a key in the repository file is an error that names the key; set it in your user file or the environment instead. Model names must match `[A-Za-z0-9._:[\]-]+`, `run_pr.permission_mode` must be one of `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`, and allowed-tool entries and prompts must not start with `-`.

```toml
provider = "github"            # only GitHub in Phase 1
base_branch = "auto"           # auto = the repository's default branch
remote = "origin"
state_dir = "~/.local/share/code-check/state"

[review]
auto_approve = false           # true: approve only when there are no Critical or Warning findings
max_files = 80                 # larger changes are split into chunks, never skipped
max_chunk_chars = 120000
max_inline_comments = 25       # the rest goes into the review summary
ignore = ["**/*.lock", "dist/**"]   # plain glob patterns, see below

[loop]
max_prs_per_iteration = 5
repos = []                     # empty = every repository you have access to
review_forks = false           # the unattended review loop skips fork-based PRs unless true

[models]
aggregator = "opus"
language_experts = "haiku"
reviewers = "sonnet"           # architect, pragmatist, skeptic
deep = "sonnet"
[models.per_language]          # for example: rust = "sonnet"

[deep_reviewer]
enabled = false                # the slot exists; if it is off or unreachable the review is unaffected
command = ""

[shadow]
enabled = true
store = "~/.local/share/code-check/shadow"

[address]
test_command = ""              # run before every push; a failure blocks the push
resolve_fixed_threads = true
max_ci_attempts = 3

[run_pr]
claude_bin = "claude"
timeout_seconds = 1800
permission_mode = "dontAsk"
add_state_dir = true           # pass `--add-dir <state_dir>` (worktrees and run directories live there)
review_allowed_tools = ["Read", "Grep", "Glob", "Write", "Agent", "Bash"]
babysit_allowed_tools = ["Read", "Grep", "Glob", "Write", "Edit", "Agent", "Bash"]
# review_prompt and babysit_prompt are the prompts given to each headless process; {ref} is the PR.
# See "Prompt injection and unattended runs" before relying on these defaults.
```

**`review.ignore` is not gitignore.** Each pattern is matched against the whole repository-relative path with plain glob semantics (Python's `PurePosixPath.full_match`): `*` does not cross a `/`, `**` matches any number of directories, there is no negation (`!`), no trailing-slash directory rule and no anchoring rules. `*.lock` therefore matches only a top-level file; use `**/*.lock` for any depth and `dist/**` for everything below `dist`. Ignored files are left out of the review entirely.

After changing `[models]`, re-run `npx code-check install` (or `code-check gen-agents --out agents` in a checkout) so the agent definitions pick up the new models.

| Role | Default model |
|---|---|
| Aggregator | Opus |
| Architect, pragmatist, skeptic | Sonnet |
| 15 language experts | Haiku 4.5 |

Haiku keeps the language-expert fan-out cheap, and the Opus aggregator verifies Critical and Warning findings before they are posted. The risk is recall: the aggregator cannot recover a bug that nobody reported. Use `models.per_language` to move a language up a tier, and use shadow data to see which ones need it.

## Loops

Run the skills under Claude Code's `/loop`:

```text
/loop 15m /code-check:review-pr          # teammates' PRs awaiting your review
/loop 10m /code-check:babysit-pr         # your PRs: new comments and failing checks
/loop 2m /code-check:review-local        # your working tree, only reviewed again when it changes
```

Each iteration lists the PRs with something new (no LLM), takes at most `loop.max_prs_per_iteration`, and runs each one in its **own headless `claude -p` process, one after another**. Your session only sees a one-line summary per PR, so its context does not fill up. An iteration holds a per-mode lock file under `state_dir/locks`; an iteration that starts while the previous one is still running is skipped, and a lock older than two hours is treated as stale and reclaimed.

Every PR that was attempted is recorded as processed for its current fingerprint (head commit plus new threads and check states), whether the run succeeded or not. That includes transient failures such as a failed clone, a missing `claude` binary or a timeout: **there is no automatic retry**. The PR is looked at again only after something changes on it (a push, a new comment, a check changing state). To retry sooner, run `code-check run-pr --mode review --pr OWNER/REPO#123` by hand.

State lives in `state_dir/state.json`. `StateStore` does plain read-modify-write with no locking of its own, so any process that writes to it concurrently with another must hold a `FileLock` first; the loop does this for its iteration, and the per-PR runs it spawns execute one at a time inside that lock. A corrupt state file is moved aside to `state.corrupt` and treated as empty rather than crashing the loop.

## Safety

- Never force-pushes: the git layer has no force option. Pushes happen only after the configured `address.test_command` passes (when set) and only when the remote branch has no commits you lack.
- Draft PRs are never posted to and never approved. A PR whose head moved while the review ran is not posted either; re-run the review.
- The loop never handles more than `loop.max_prs_per_iteration` PRs per iteration, one at a time.
- Unattended runs can use the tools in `run_pr.allowed_tools`, which includes plain `Bash` by default (see Configuration). Narrow it if that is more than you want to grant.
- Refuses to run `address-comments` on a working tree with uncommitted changes, and works in an isolated worktree when you are not on the PR branch.
- Fork-based PRs are reviewed but cannot be pushed to in Phase 1.
- A thread you pushed back on is never resolved by the tool; the reviewer resolves it.
- A review is never approved if the PR is a draft, if any reviewer failed or returned invalid output, or if the PR head moved while the review ran. If GitHub rejects an inline anchor, the findings are posted in the review body instead of being lost.
- Every run first fetches the latest remote base and compares against `<remote>/<base>`, not your local base branch. If the fetch fails the output says "base may be stale".

## Shadow mode

Use it first alongside human reviewers. It does not change behavior; it records what the tool did so human feedback can improve the skills and become a golden evaluation set.

Everything is local: nothing leaves your machine. It is written as JSONL under `shadow.store`: the run, every reviewer finding, which ones the aggregator kept, what was posted, the decision, and the verdict for every comment `address-comments` evaluated.

Secrets are scrubbed before anything is written (private keys, GitHub, Anthropic, AWS, Slack, Stripe and Google tokens, bearer and authorization headers, JWTs, and `password=`/`token=`-style assignments). The scrub is pattern matching and therefore **best-effort**: it cannot guarantee that every secret is removed, so treat the shadow directory as sensitive.

```bash
code-check shadow collect --all                       # later: record what humans did with each comment
code-check shadow export-golden --out golden.jsonl    # auto-labelled examples (confirmed, disputed, misses)
```

`collect` records resolved or outdated threads, human replies and reactions, and human comments the tool never raised. Findings nobody reacted to are false-positive candidates; human comments with no matching finding are misses. Labels are marked `auto` for a human to review.

## Extending

**A new code host.** Implement the `Provider` protocol in `src/codecheck/providers/base.py` (about a dozen methods that return the dataclasses in `models.py`), register it in `providers/__init__.py`, and run the shared contract suite against it by subclassing `ProviderContract` in `tests/providers/contract.py`. Nothing outside `providers/` needs to change. GitHub uses the `gh` CLI; GitLab (`glab`) and Bitbucket are planned for Phase 2.

**The deep reviewer.** Enable `[deep_reviewer]` and give the `deep-reviewer` agent access to your MCP tools, memory or retrieval system. If it is disabled, missing or fails, the panel runs without it.

**Agents.** Prompts and defaults live in `src/codecheck/agentgen.py`; `agents/` is generated from it and a test fails if they drift.

## Troubleshooting

Start with `code-check doctor` (or `npx code-check doctor`).

| Symptom | Fix |
|---|---|
| `gh auth` fails | `gh auth login`, or set `GH_TOKEN` for unattended runs |
| "no changes against base" | Nothing differs from `<remote>/<base>`; commit or edit something, or check `base_branch` |
| "already reviewed" / "unchanged since last review" | Nothing changed since the last run; pass `--force` to `precheck` to review anyway |
| "base may be stale" | The fetch failed (offline or no access); results compare against the last fetched base |
| "run this from a clone of OWNER/REPO" | `review-pr` needs a local clone of the PR's repository |
| A loop does nothing | It only acts on PRs with something new; check `code-check list-work --mode review` |
| Python is older than 3.14.10 | Upgrade (`brew upgrade python@3.14`, `uv python install 3.14`) |
