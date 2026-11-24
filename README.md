# code-check

Review pull requests and deal with review comments from inside Claude Code or Codex, on GitHub or GitLab.

`code-check` is a set of four skills (plus two loop aliases for Claude Code) on top of a deterministic Python CLI. The CLI does everything that must be exact: git and host calls, diff parsing, anchoring comments to lines, the approval policy, state. Models do only the judgment: reviewing code, weighing review comments, fixing what is valid.

| | GitHub (`gh`) | GitLab (`glab`) |
|---|---|---|
| **Claude Code** (plugin) | all four skills, `loop`, `run-pr` | all four skills, `loop`, `run-pr` |
| **Codex** (skills) | all four skills, interactive only | all four skills, interactive only |

Codex runs a read-only reviewer panel and needs macOS today; unattended loops (`loop`, `run-pr`) use Claude Code. GitLab means gitlab.com merge requests; self-hosted GitLab is untested. See [docs/INSTALL-CODEX.md](docs/INSTALL-CODEX.md) and [docs/gitlab-testing.md](docs/gitlab-testing.md).

## Install

Prerequisites: Python **3.14.10+**, `git`, Node 20+ (installer only), and the CLI of your host, logged in: `gh auth login` for GitHub, `glab auth login` for GitLab, both if you use both. Which host a repository uses is detected per repository (`provider = "auto"`: a `gitlab.com` remote means GitLab, anything else GitHub), so one install serves both.

| You use | Install |
|---|---|
| **Claude Code** | `/plugin marketplace add subhiandrews/codecheck` then `/plugin install code-check@code-check` |
| **Claude Code**, without the marketplace | `npx github:subhiandrews/codecheck install` (or `node installer/cli.js install` from a clone) |
| **Codex** | `npx github:subhiandrews/codecheck install --target codex` (or `node installer/cli.js install --target codex`) |

Add `--provider github|gitlab|both` (default `github`) to the Node installer to choose which host CLI it requires. `doctor` checks prerequisites at any time, `uninstall` removes only what the installer created, and `code-check doctor` checks the CLI itself. Restart Claude Code or Codex afterwards. The package is not on npm yet, so do not run a bare `npx code-check`.

CI and remote agents: `pip install git+https://github.com/subhiandrews/codecheck.git`, set `GH_TOKEN` or `GITLAB_TOKEN` and `ANTHROPIC_API_KEY`, and install the plugin for that `claude` too. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#installer-and-unattended-setup).

## Quick start

In Claude Code the skills are `/code-check:<skill>`; in Codex they are `$<skill>`.

```text
/code-check:review-local                 # review the branch you are on (prints, never posts)
/code-check:review-pr 123                # review PR 123 and post one inline review
/code-check:review-prs                   # review the open PRs here, newest first (max 3)
/code-check:address-comments 456         # work through the comments on your PR 456
/code-check:babysit-prs                  # comments and failing CI on all your PRs here
```

A PR is `123` (current repository), `OWNER/REPO#123` or a URL; on GitLab a merge request is `7`, `group/subgroup/project#7` or a URL. Add `security` or `performance` after the PR to include that optional reviewer, or `full` to use every reviewer and skip routing.

## Skills

| Skill | What it does |
|---|---|
| `review-local` | Reviews your own branch (committed and uncommitted) against the freshly fetched remote base and prints findings. Never posts. |
| `review-pr` | Reviews a teammate's PR and posts one inline review with Critical, Warning and Suggestion findings. Approves only when `review.auto_approve` is on and nothing Critical or Warning remains. Never posts to drafts or to your own PRs. From the third review of a PR it posts new Suggestions only on lines changed since its last review (`review.quiet_after_round`). |
| `address-comments` | On your own PR: checks each unresolved thread against the code, fixes what is valid, reviews the fixes once and fixes what that finds, pushes back with evidence on what is not, pushes behind your test gate, replies, resolves fixed threads, re-requests review. |
| `babysit-pr` | `address-comments` plus failing CI checks, across your PRs. Meant for `/loop`. |
| `review-prs`, `babysit-prs` | Aliases of `review-pr` and `babysit-pr` with no PR argument: the loop forms. Run them in a clone, or in a directory that holds clones (`~/opensource`): every clone directly under it is covered, found by its remote; nothing depends on a review-request queue. |

If there is nothing new to do, a run stops after the no-LLM `precheck` and spends no tokens.

## Reviewer panel (subagents)

Reviewers run in parallel per chunk of the diff and write findings files. The CLI validates and anchors them; an aggregator then verifies every Critical and Warning against the code.

| Agent | Role | Default model |
|---|---|---|
| `code-check:lang-<name>` | One per language in the diff: c, cpp, csharp, go, java, javascript, kotlin, php, python, ruby, rust, shell, sql, swift, typescript | Haiku |
| `architect`, `pragmatist`, `skeptic` | Design, simplicity and adversarial review; the architect is skipped for a clearly small change | Sonnet |
| `security` (opt-in) | Secrets, PII, injection, auth, crypto, risky dependencies | Opus |
| `performance` (routed) | Complexity, N+1, unbounded growth, blocking calls | Sonnet |
| `doc-skeptic` (routed) | Skeptical of restating comments and of new or verbose documentation | Sonnet |
| `deep-reviewer` (optional slot) | Your own knowledge sources via MCP; off by default | Sonnet |
| `aggregator` | Merges duplicates, verifies, drops noise | Opus |

`context` routes `architect`, `performance` and `doc-skeptic` from the diff, in code, and records why in `panel.routing`; `review.routing` tunes it. Force a reviewer with `--security` / `--performance` on `context` and `run-pr`, the `security` / `performance` skill argument, or `review.security` / `review.performance`. `--full`, the `full` skill argument or `review.full` runs every reviewer, security and performance included, with no routing. Codex uses its own model table (`[codex_models]`).

## CLI

Every subcommand prints exactly one JSON document, errors included. Run `code-check --help` or `code-check <command> --help` for flags.

| Group | Commands |
|---|---|
| Review | `precheck`, `sync`, `context [--mode local\|pr] [--pr] [--full] [--security] [--performance]`, `panel run` (Codex), `ingest`, `post`, `cleanup`, `guidance evidence\|apply` |
| Address comments | `status`, `workspace`, `threads`, `commit`, `push`, `reply`, `resolve`, `request-review`, `checks`, `check-log`, `attempt` |
| Unattended | `list-work`, `mark-done`, `run-pr --mode review\|babysit --pr REF`, `loop --mode review\|babysit` |
| Setup | `doctor`, `gen-agents --out agents`, `gen-skills --out codex/skills` |
| Developer | `shadow` (collect, export-golden, record-verdict), `eval --replay\|--live\|--calibrate\|--history` |

Loops: `/loop 15m /code-check:review-prs`, `/loop 10m /code-check:babysit-prs`, `/loop 2m /code-check:review-local`. Each PR gets one headless `claude -p` process and a failing PR is retried up to 3 attempts per fingerprint; caps are in [CONFIGURATION.md](docs/CONFIGURATION.md).

## Configuration

Precedence, highest first: environment (`CODE_CHECK_<SECTION>__<KEY>`), `~/.config/code-check/config.toml`, the repository's `.code-check.toml`, defaults. Unknown keys are errors. Minimal example:

```toml
provider = "auto"              # auto, "github" or "gitlab"
base_branch = "auto"

[review]
auto_approve = false           # true: approve only with no Critical or Warning findings
security = false
performance = false
full = false
ignore = ["**/*.lock", "dist/**"]

[address]
test_command = ""              # runs before every push; a failure blocks the push

[models]
aggregator = "opus"
language_experts = "haiku"
```

A repository's `.code-check.toml` cannot set `state_dir`, `[run_pr]`, `[shadow]`, `[golden]`, `[codex]`, `[codex_models]`, `[codex_reasoning]`, `review.auto_approve`, `review.attribution`, `review.label`, `review.login`, `review.seed_comments`, `loop.review_forks`, `address.test_command` or `deep_reviewer.command`: a repository you review is not trusted to decide what an unattended run may do. Set those in your user file or the environment. After changing `[models]`, re-run the installer or `code-check gen-agents --out agents`. Every key, with defaults: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

### CODE_CHECK.md

A `CODE_CHECK.md` at the repository root tells the reviewers, in plain prose, what to look at and what to ignore (max 16,000 characters). PR runs read it from the **base branch**, never the PR head; local runs read your working tree. A missing, empty or symlinked file is ignored. The same way, reviewers also get the repository's `CLAUDE.md` (and nested ones) as background on conventions and how it is built; it cannot give them tasks. In a monorepo, a `CODE_CHECK.md` in a directory above a changed file is read too and applies only to files under that directory, where it wins over the root file on a conflict; all files share the one size limit. Optionally (`review.learn_guidance`, user config only, off by default), `review-local` can propose edits to these files from what past reviews on this machine learned, writing the working tree only and never committing, and `review-pr` can leave the same kind of proposal as a patch file to open as a separate PR, never touching the PR it reviewed; see [ARCHITECTURE.md](docs/ARCHITECTURE.md#code_checkmd). This repository's own file also checks that the docs stay accurate: the README stays short and current, and architecture detail lives under `docs/`.

## Safety

- Never force-pushes. Pushes happen only after `address.test_command` passes and only when the remote branch has nothing you lack.
- Never posts to or approves a draft, never reviews its own PRs, and does not post if the PR head moved while the review ran.
- Never approves when a reviewer failed or returned invalid output.
- A thread you pushed back on is never resolved by the tool.
- Unattended runs use only the tools in `run_pr.*_allowed_tools`, and the defaults include plain `Bash`. Everything in a PR is untrusted text. Keep `review.auto_approve` off and `loop.review_forks` off for repositories that accept outside contributions. Read [Prompt injection and unattended runs](docs/ARCHITECTURE.md#prompt-injection-and-unattended-runs) before relying on the defaults.

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design, review flow, loops and state, GitLab differences, optional reviewers, safety model |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every setting, defaults, repository restrictions, Codex models |
| [docs/INSTALL-CODEX.md](docs/INSTALL-CODEX.md) | Codex install, first run, measured sandbox limits |
| [docs/gitlab-testing.md](docs/gitlab-testing.md) | GitLab test plan and what is unverified |
| [docs/decisions/](docs/decisions/README.md) | Why each choice was made: short, append-only decision records |
| [docs/MAINTAINING.md](docs/MAINTAINING.md) | Shadow mode, hand-offs, golden cases, evals, commit gates, extending |
| [docs/Evals.md](docs/Evals.md) | How the evals fit together |
| [CHANGELOG.md](CHANGELOG.md) | User-visible changes per release |

## Troubleshooting

Start with `code-check doctor` (or `node installer/cli.js doctor`).

| Symptom | Fix |
|---|---|
| `gh auth` / `glab auth` fails | `gh auth login` / `glab auth login`, or set `GH_TOKEN` / `GITLAB_TOKEN` for unattended runs |
| "no changes against base" | Nothing differs from `<remote>/<base>`; check `base_branch` |
| "already reviewed" / "unchanged since last review" | Nothing changed; pass `--force` to `precheck` to review anyway |
| "N replies ... no new commits since your review" | The loop never re-reviews on a reply. Run `review-pr <PR>` interactively to choose a re-review |
| "base may be stale" | The fetch failed (offline); results compare against the last fetched base |
| "run this from a clone of OWNER/REPO" | `review-pr` needs a local clone of the PR's repository |
| A loop does nothing | It acts only on PRs with something new; check `code-check list-work --mode review` |
| "cannot be set in the repository's .code-check.toml" | Move that key to `~/.config/code-check/config.toml` or a `CODE_CHECK_` variable |
| Python older than 3.14.10 | `brew upgrade python@3.14` or `uv python install 3.14` |

## License

[Apache License 2.0](LICENSE). Copyright 2026 Subhi Andrews; see [NOTICE](NOTICE). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately: [SECURITY.md](SECURITY.md).
