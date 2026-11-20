# Configuration

Every code-check setting. The README has a minimal example; this is the full reference. Shadow, golden, Codex and `run_pr` settings are security-sensitive and can only be set in your user config or the environment (see below).

Settings are layered, highest precedence first: environment (`CODE_CHECK_<SECTION>__<KEY>`, for example `CODE_CHECK_REVIEW__AUTO_APPROVE=true`), your `~/.config/code-check/config.toml`, the repository's `.code-check.toml`, then the defaults below. Your own settings therefore win over a repository's. Unknown keys and wrong types in files are errors.

A repository's `.code-check.toml` may **not** set the security-sensitive keys `state_dir`, anything under `[run_pr]`, `[shadow]`, `[golden]`, `[codex]`, `[codex_models]` or `[codex_reasoning]`, `review.auto_approve`, `review.attribution`, `review.label`, `review.login`, `loop.review_forks`, `address.test_command` or `deep_reviewer.command`: a repository you review is not trusted to choose what an unattended run may do or execute, where state is written, or whether reviews approve. Such a key in the repository file is an error that names the key; set it in your user file or the environment instead. Model names must match `[A-Za-z0-9._:[\]-]+`, `run_pr.permission_mode` must be one of `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`, and allowed-tool entries and prompts must not start with `-`.

```toml
provider = "auto"              # auto (a gitlab.com remote means gitlab, anything else github), "github" or "gitlab"
base_branch = "auto"           # auto = the repository's default branch
remote = "origin"
state_dir = "~/.local/share/code-check/state"

[review]
auto_approve = false           # true: approve only when there are no Critical or Warning findings
max_files = 80                 # larger changes are split into chunks, never skipped
max_chunk_chars = 120000
max_inline_comments = 25       # the rest goes into the review summary
security = false               # true: always include the security reviewer (see [ARCHITECTURE.md](ARCHITECTURE.md#security-review))
performance = false            # true: always include the performance reviewer (see [ARCHITECTURE.md](ARCHITECTURE.md#performance-review))
full = false                   # true: every reviewer on every review, routing skipped (see [ARCHITECTURE.md](ARCHITECTURE.md#reviewer-routing-and-full-mode))
attribution = true             # one credit line (tool, licence, repo link) in the review summary, never in inline comments
label = "code-check"           # label added to reviewed PRs; "" disables it
login = ""                     # act as this login instead of `gh api user`; set it in CI, where the token cannot call /user (e.g. "github-actions[bot]")
ignore = ["**/*.lock", "dist/**"]   # plain glob patterns, see below

[review.routing]               # which optional reviewers a change earns; `full` and forced flags ignore it
mode = "auto"                  # "auto" or "off" (off: always architect, pragmatist, skeptic)
small_max_lines = 40           # at most this many changed lines in at most small_max_files files skips the architect
small_max_files = 3
performance_min_score = 3      # score of the loop, query and memory signals in the added lines that adds the performance reviewer
max_comment_percent = 3        # comments may be at most this share of the added lines (docs reviewer)
docs_min_comment_lines = 3     # fewer added comment lines than this are never over the cap, so they never route the docs reviewer or raise the cap finding

[loop]
max_prs_per_iteration = 5
repos = []                     # empty = every repository you have access to
review_forks = false           # the unattended review loop skips fork-based PRs unless true

[models]
aggregator = "opus"
language_experts = "haiku"
reviewers = "sonnet"           # architect, pragmatist, skeptic
deep = "sonnet"
security = "opus"             # the opt-in security reviewer
performance = "sonnet"         # the performance reviewer
docs = "sonnet"                # the docs reviewer
judge = "opus"                # `eval --calibrate` only: the model that judges findings (developer tooling)
[models.per_language]          # for example: rust = "sonnet"

[deep_reviewer]
enabled = false                # the slot exists; if it is off or unreachable the review is unaffected
command = ""

[shadow]
enabled = true
store = "~/.local/share/code-check/shadow"
handoff = false                # record hand-off data even when shadow.enabled is false (user config or CODE_CHECK_SHADOW__HANDOFF only)

[golden]                       # user config or environment only
licenses = ["mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "0bsd", "unlicense"]   # `shadow promote --finalize` refuses a repository under any other license

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
# See [Prompt injection and unattended runs](ARCHITECTURE.md#prompt-injection-and-unattended-runs) before relying on these defaults.
```

**`review.ignore` is not gitignore.** Each pattern is matched against the whole repository-relative path with plain glob semantics (Python's `PurePosixPath.full_match`): `*` does not cross a `/`, `**` matches any number of directories, there is no negation (`!`), no trailing-slash directory rule and no anchoring rules. `*.lock` therefore matches only a top-level file; use `**/*.lock` for any depth and `dist/**` for everything below `dist`. Ignored files are left out of the review entirely.

After changing `[models]`, re-run the installer (`node installer/cli.js install`) (or `code-check gen-agents --out agents` in a checkout) so the agent definitions pick up the new models.

| Role | Default model |
|---|---|
| Aggregator | Opus |
| Architect, pragmatist, skeptic | Sonnet |
| Security reviewer (opt-in) | Opus |
| Performance reviewer (opt-in) | Sonnet |
| 15 language experts | Haiku 4.5 |

Haiku keeps the language-expert fan-out cheap, and the Opus aggregator verifies Critical and Warning findings before they are posted. The risk is recall: the aggregator cannot recover a bug that nobody reported. Use `models.per_language` to move a language up a tier, and use shadow data to see which ones need it. Opus is used for the aggregator and, only when you turn it on, the security reviewer.

For a Codex review, run `code-check panel run --run <run_id>` after `context`. The CLI starts read-only reviewers and the aggregator, validates their JSON, and runs `ingest` before aggregation. Check `failed_members` in the result. If the aggregator failed, stop before `post` and clean up the run. Other failed members make the review incomplete.

`[codex_models]` and `[codex_reasoning]` use the same keys as `[models]`, including `per_language`. The defaults are GPT-6.2 Astra at high effort for aggregation, security and judging, GPT-6.2 Sol at medium effort for general, deep, performance and docs review, and GPT-6 Luna at high effort for the language experts. Codex model values cannot start with `-`. `codex.max_parallel` defaults to 4 concurrent reviewers and `codex.timeout_seconds` to 300 seconds per process. Codex picks up these settings at run time. `codex.allow_unmeasured_platform` defaults to false and lets users explicitly opt in outside macOS.

Regenerate the Codex skill templates from a checkout with `code-check gen-skills --out codex/skills`. Automatic queues via `loop` and `run-pr` use Claude Code.

## Attribution and label

Each posted PR review ends its summary comment with one small line saying it was made with code-check (Apache-2.0) and linking this repository. Inline comments never carry it. After posting, the PR gets the `code-check` label (GitHub creates it if missing); labeling is best-effort, so a reviewer without triage rights still posts the review and `post` reports `labeled: false`. Set `review.attribution = false` or `review.label = ""` in your user config to turn either off; a repository cannot change them.

## Shadow and golden settings (user config or environment only)

`[shadow]` and `[golden]` are listed in the block above. `shadow.enabled` records runs locally, `shadow.store` is where, `shadow.handoff` also records hand-off data (see [MAINTAINING.md](MAINTAINING.md#shadow-mode)), and `golden.licenses` lists the licenses whose diffs may be promoted to golden cases. A repository's `.code-check.toml` cannot set any of them.
