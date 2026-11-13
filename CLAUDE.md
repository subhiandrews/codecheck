# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`code-check` is a Claude Code plugin plus a Python CLI for PR review and review-comment handling. The design is split on purpose: a **deterministic Python CLI** does everything that must be exact (diff parsing, finding validation, dedupe, anchoring, policy, state, git/gh calls), and **skills + subagents** do only the LLM work (reviewing, aggregating, addressing comments). Authority order when they disagree: spec (`docs/superpowers/specs/2026-11-12-code-check-design.md`), then the Task 1 spike (`docs/superpowers/spikes/`, which records real `claude` CLI behaviour and overrides plan prose), then the plan.

## Commands

Python 3.14 (CI asserts >= 3.14.10). The venv lives in the repo at `.venv`.

```
.venv/bin/pytest -q                              # all Python tests
.venv/bin/pytest tests/test_loop.py::test_name   # one test
node --test tests-node/*.test.js                 # installer tests (Node >= 20)
.venv/bin/python -m codecheck <subcommand>       # run the CLI (also: bin/code-check wrapper)
.venv/bin/python -m codecheck gen-agents --out agents   # regenerate agents/*.md
```

`.venv/bin/ruff check .` must pass (default rules, no config file; ruff is in the `dev` extra). A project hook in `.claude/settings.json` runs it before any `git commit` Claude runs and blocks the commit on failure. Every CLI subcommand prints exactly one JSON document on stdout; errors are JSON too (a catch-all in `cli.py`).

## Architecture

**Layers (`src/codecheck/`)**
- `cli.py` builds `AppContext` (config, runner, provider, git, `StateStore`, optional `ShadowStore`) and dispatches. Each file in `commands/` is one subcommand exposing `NAME` and `run`; a new command must be added to **both the import list and `ALL`** in `commands/__init__.py`, or the static plugin check fails.
- `core/` is pure logic: `diffmap` (unified diff to right-side line numbers, finding anchoring), `findings` (validate/dedupe/ID), `panel` (which reviewers and languages; `is_ignored` is plain full-match glob, not gitignore), `chunk`, `policy`, `precheck`, `render`, `state`.
- `providers/` (GitHub via `gh`) and `vcs/` (git) sit behind interfaces; tests use `FakeProvider`/`FakeRunner` (`tests/fakes.py`) and never call real `gh`/`git` remotes or `claude`.
- `shadow/` records runs locally for later golden-set export; every write goes through scrub.

**Review flow:** skill calls `precheck`, then `context` (creates a worktree and `state_dir/runs/<run>/`), dispatches reviewer subagents that write `findings/*.json`, then the `code-check:aggregator` agent; `ingest` validates and anchors findings, `post` posts the review, `cleanup` removes the worktree. Project guidance: `core/guidance.py` reads `CODE_CHECK.md` (PR mode from the base branch via `git show`, local mode from the worktree, 16,000 character cap, symlinks and bad files ignored); `context` copies it to the run dir and reports `guidance` metadata, `workspace` returns it with the text, and every reviewer prompt in `agentgen.py` reads it first. The opt-in `security` panel member (`--security` / `review.security`, `models.security`, one agent per chunk) writes category `security` findings whose text `core/findings.py` scrubs. Address/babysit flow uses `workspace`, `threads`, `commit`, `push`, `reply`, `resolve`, `checks`, `attempt`.

**Unattended path:** `loop` (per-mode `FileLock`) calls `list_work` then `run_pr` per PR, which shells out to `claude -p` with `--permission-mode dontAsk --permission-prompts none --allowedTools ...` and stdin closed. There is no `--max-turns` flag (spike finding).

**Plugin and installer:** `skills/`, `agents/`, `hooks/`, `bin/`, `.claude-plugin/` form the plugin payload. `agents/*.md` are **generated** from `agentgen.py` and committed; a drift test fails if they differ, so rerun `gen-agents` after changing prompts or default models. Subagent types are namespaced `code-check:<agent>`. `hooks/ensure-runtime.sh` (with `bin/code-check`) bootstraps a venv into `CLAUDE_PLUGIN_DATA` (fallback `~/.local/share/code-check`); the wrapper re-runs it on every call. `installer/` (Node) installs either as a Claude plugin or by copying skills/agents, tracking a receipt so uninstall removes only what it created.

`tests/test_plugin_static.py` cross-checks skills against the real argparse parser (subcommands, flags, agent names), and `tests/test_readme.py` checks the README against the CLI and config keys, so changing a command, flag or config key means updating skills and README too.

## Invariants to preserve

- Anything from a PR, config or reviewer output that reaches git/gh/claude argv must never be able to act as an option (leading `-` guards, see `Git._safe`, provider validators).
- Finding or PR-derived file paths go through `commands/_common.contained()`.
- Never force-push; never post to draft PRs; the tool does not review its own PRs.
- Shadow recording side effects only via `ctx.guarded(...)`: recording must never change a command's behaviour.
- `ShadowStore.run_ids()` is not chronological; use `ordered_run_ids()` / `runs_for_pr`.
- `StateStore` locks itself (`state.json.lock`); loop bookkeeping keys live under `loop:<mode>:<ref>`, never the `review:<ref>` keys that `precheck`/`post` own. Security-sensitive config keys (`state_dir`, `run_pr.*`, `shadow.*`, `review.auto_approve`, `loop.review_forks`) are rejected in a repo's `.code-check.toml`; user config beats repo config.

## Work in progress

This branch was built via a subagent-driven plan; `.superpowers/sdd/` (git-ignored) holds the run ledger. Real-machine smoke tests of the plugin, installer and unattended `claude -p` runs have not been done; behaviours the spike did not confirm (hook-time env vars, absolute-path `Bash(...)` allow rules, `--add-dir`, which `run-pr` passes by default; `run_pr.add_state_dir=false` disables it) are unverified.
