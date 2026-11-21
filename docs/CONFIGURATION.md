# Configuration

Every code-check setting. The README has a minimal example; this is the full reference. Shadow, golden, Codex and `run_pr` settings are security-sensitive and can only be set in your user config or the environment (see below).

Settings are layered, highest precedence first: environment (`CODE_CHECK_<SECTION>__<KEY>`, for example `CODE_CHECK_REVIEW__AUTO_APPROVE=true`), your `~/.config/code-check/config.toml`, the repository's `.code-check.toml`, then the defaults below. Your own settings therefore win over a repository's. Unknown keys and wrong types in files are errors.

A repository's `.code-check.toml` may **not** set the security-sensitive keys `state_dir`, anything under `[run_pr]`, `[shadow]`, `[golden]`, `[memory]`, `[codex]`, `[codex_models]` or `[codex_reasoning]`, `review.auto_approve`, `review.attribution`, `review.label`, `review.login`, `loop.review_forks`, `address.test_command` or `deep_reviewer.command`: a repository you review is not trusted to choose what an unattended run may do or execute, where state is written, or whether reviews approve. Such a key in the repository file is an error that names the key; set it in your user file or the environment instead. Model names must match `[A-Za-z0-9._:[\]-]+`, `run_pr.permission_mode` must be one of `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`, and allowed-tool entries and prompts must not start with `-`.

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

[memory]                       # user config or environment only; `read` and `reviewers` now put lessons into reviews (see MAINTAINING.md, Memory read path)
enabled = false                # collect evidence and consolidate lessons
read = false                   # put lessons into reviews (needs enabled; never in a blind hand-off run)
reviewers = false              # also give the reviewers miss lessons (`context`); the aggregator gets finding lessons (`ingest`) whenever read is on
store = "~/.local/share/code-check/memory"
max_lessons = 12               # most lessons shown to one review
max_chars = 6000               # most characters of lessons shown to one review
timeout_seconds = 3            # time budget for reading memory; a slow store is skipped, never waited on
url = ""                       # shared memory server, https unless it is on this machine; "" keeps memory local
token_file = ""                # file holding the server token
share_level = "lessons"        # most detail pushed to the server: "lessons", "evidence" or "episodes"
private_level = "lessons"      # the same cap, applied to private repositories
share_repos = []               # repositories whose memory may be pushed; empty pushes nothing
embed_url = "http://127.0.0.1:11434"   # embedding service, https unless it is on this machine
embed_model = ""
embed_at = "client"            # where embeddings are computed: "client" or "server"
collect = false                # collect outcomes automatically when a skill finishes
half_life_days = 90            # evidence weighs half after this many days (used by `memory consolidate`)
expire_days = 180              # evidence counts for nothing after this many days; `memory consolidate` does not store it, `--prune` deletes it

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

## Memory settings (user config or environment only)

`[memory]` is listed in the block above and can only come from your user file or `CODE_CHECK_MEMORY__<KEY>` (for example `CODE_CHECK_MEMORY__ENABLED=1`, `CODE_CHECK_MEMORY__SHARE_REPOS=org/a,org/b`); a repository's `.code-check.toml` containing `[memory]` is an error. `share_level` and `private_level` must be `lessons`, `evidence` or `episodes`, `embed_at` must be `client` or `server`, the numeric keys must be positive, and `url` and `embed_url` must be `https` unless they point at `localhost` or `127.x.x.x`. `memory.collect` makes cleanup collect outcomes and consolidate; `memory.read` (with `reviewers`) puts lessons into reviews as `memory.md`, never in blind runs; with a `url` set, `context` first refreshes the local replica from the server (at most every 10 minutes, within half of `timeout_seconds`, any failure silently keeps the replica), and with `url` and `share_repos` both set, collection pushes after it consolidates. `memory consolidate` is run by hand, works with `enabled = false`, writes `memory.db` inside `store` (directory mode 0700, file 0600) and reads `half_life_days` and `expire_days`.

`collect = true` takes effect: when a skill finishes (`cleanup`), reviewed PRs that are due are collected and the shadow store is consolidated into `store`. See "Automatic collection" in MAINTAINING.md.

Shadow records carry who said what: each run records `pr_author`, each reply on a posted finding records a `role`, and each outcome records `resolved_by_role`. A role is `maintainer` (can push to the repository), `author` (the PR author), `contributor`, `self` (code-check's own login) or `unknown`. Write access comes from the host (GitHub: the collaborator permission endpoint; GitLab: project member access level 30 or more); when the host will not say, GitHub's author association is used instead (OWNER, MEMBER and COLLABORATOR count as maintainer, which can over-count organisation members). Records written before this carry none of these fields and still load.
