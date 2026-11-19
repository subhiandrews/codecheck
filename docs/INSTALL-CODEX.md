# Install code-check for Codex

## Prerequisites

- macOS for supported Codex reviews. Codex CLI on PATH and signed in with `codex login`. The sandbox and output behavior below were measured with Codex CLI 0.170.0 on macOS.
- Node.js 20 or later, Git, and Python 3.14. Python 3.14.10 or later is recommended. The launcher uses `uv` when available to create its Python environment on first use.
- The CLI for your repository's host. Log in with `gh auth login` for GitHub or `glab auth login` for GitLab.

## Install and uninstall

From a checkout, run:

```sh
node installer/cli.js doctor --target codex
node installer/cli.js install --target codex
```

Pass `--provider gitlab` for GitLab or `--provider both` to check prerequisites for both hosts. Doctor reports the Codex CLI version and login status. The installer stages the launcher and writes its absolute path into the skills it installs under `$HOME/.agents/skills/`. `CODEX_HOME` does not change that skill directory. Existing skills with the same name are skipped. Start a fresh Codex session after installing.

```sh
node installer/cli.js uninstall --target codex
```

Uninstall removes the skill files listed in the Codex receipt and prunes their empty parent directories. It leaves other files, the staged launcher, the Python environment, review state and shadow data in place. The receipt lives at `$CODE_CHECK_HOME/codex/receipt.json` when `CODE_CHECK_HOME` is set, or `~/.local/share/code-check/codex/receipt.json` by default.

## Configuration

Set Codex options in `~/.config/code-check/config.toml` or via the corresponding `CODE_CHECK_<SECTION>__<KEY>` environment variables. A repository's `.code-check.toml` is rejected if it contains `[codex_models]`, `[codex_reasoning]` or any `codex.*` setting.

```toml
[codex]
max_parallel = 4
timeout_seconds = 300
allow_unmeasured_platform = false

[codex_models]
reviewers = "gpt-6.2-sol"

[codex_reasoning]
reviewers = "medium"
```

`codex.max_parallel` caps concurrent reviewer processes. `codex.timeout_seconds` bounds each reviewer and the aggregator individually. `doctor --target codex` warns outside macOS, and the panel refuses to start there. Set `codex.allow_unmeasured_platform = true` in user configuration only to opt in to an unmeasured platform. Both model and effort tables accept `aggregator`, `language_experts`, `reviewers`, `deep`, `security`, `performance`, `judge` and `per_language` overrides. The defaults use Astra for aggregation, security and judging, Sol for general, deep and performance review, and Luna for language reviews. Reasoning accepts `low`, `medium` and `high`.

## First run

Open Codex in the repository you want to review and enter `$review-local` to see findings without posting. Use `$review-pr 123` to review a teammate's PR or merge request. Give `$babysit-pr` an explicit PR reference when using it interactively. The unattended `loop`, `run-pr` (the `run_pr` path), and `babysit-pr` under `/loop` use Claude Code.

## Measured sandbox limits

The CLI launches each reviewer and the aggregator with `codex exec -s read-only`, approval set to never, closed input and hooks disabled. Reviewers return findings as JSON. The CLI validates those findings and writes the files itself.

The tool shell inherits only `HOME`, `PATH`, and `TMPDIR` under the measured environment policy. In the probe, the tool shell could see neither the synthetic `CODEX_API_KEY` nor an unrelated environment variable, while client authentication, Git, and access to review files worked. The probe did not measure shell startup files that reintroduce variables or non-shell tools.

Reviewers can read anywhere on disk that the process can read, including Codex’s own auth file under the user’s home with file-backed ChatGPT login and other readable home files. The checkout directory selected by `-C` does not confine reads. On the measured macOS setup, the shell could read a sibling run directory and a separate worktree. Writes were denied. A tool shell could reach neither a loopback HTTP server nor a public HTTPS hostname that the control shell could reach. Public numeric-IP egress and network access through non-shell tools are **unconfirmed**. See the [Codex spike](superpowers/spikes/2026-11-19-codex.md) for the commands and results.

A prompt-injected PR could try to make the reviewer include credentials or private text in a finding that is then posted. The CLI checks every finding’s message and suggestion for recognised secret patterns, including JWT-shaped strings, `sk-` keys, bearer values, and `token=...` assignments. It redacts matches in messages and withholds entire suggestions that match. Standalone opaque tokens and private text without a recognised secret pattern can pass through.
