# Codex support alongside Claude Code

Status: proposal, for Codex to implement. This fills in the Codex install item of Phase 2 in `2026-11-12-code-check-design.md`. The [Codex spike](../spikes/2026-11-19-codex.md) takes precedence for measured Codex behaviour. For everything else, follow this spec, then the plan.

## Intent

Make the review skills usable from Codex without touching how they run in Claude Code. The deterministic CLI (diff parsing, finding validation, anchoring, policy, state, `git`/`gh`) is shared. On Codex the **CLI starts the reviewers**, each as its own read-only `codex exec` process, instead of the skill dispatching subagents. The release that ships this is 1.0.0.

## Why the reviewers are processes, not subagents

The spike measured it. A subagent inherits the parent's sandbox policy and the `sandbox_mode = "read-only"` in its agent file had no effect: both reviewers wrote outside `findings/`. The parent cannot be read-only, because the CLI it calls creates worktrees and run directories and runs `git` and `gh`. So no per-child restriction exists. Reviewers read untrusted PR text and, on Codex, have a shell, so an unrestricted child is not acceptable.

A standalone `codex exec -s read-only` process denied every write (11 of 11, exit 1, `Operation not permitted`), could still read the separate run directory and worktree, and returned a final message that parsed directly as JSON. A reviewer that cannot write returns its findings as JSON; the CLI validates them and writes `findings/<name>.json` itself.

## Non-goals

- Changing anything Claude Code sees. `skills/`, `agents/`, `hooks/` and `.claude-plugin/` stay byte-identical apart from the version fields. Claude keeps subagents.
- A Codex version of the unattended path (`loop`, `run_pr`, which shell out to `claude -p` and run a whole skill). They stay Claude-only in 1.0.0 and the install doc says so. The `panel run` command below is most of what a Codex runner would need, so a follow-up is small.
- Forking the CLI, the evals or the golden set.
- Changing the protected merge guard (`shadow/queue.py`, `commands/handoff_queue.py`, `tests/test_handoff_queue.py`).

## Measured facts (spike, Codex CLI 0.170.0, macOS)

- Skills load by name from `<repo>/.agents/skills/<name>/SKILL.md` and `$HOME/.agents/skills/<name>/SKILL.md`. Setting only `CODEX_HOME` does not move user-skill discovery. Whether `argument-hint` is shown is unconfirmed.
- Read-only reviewer processes: `-C` sets the working directory and does **not** limit reads, so a reviewer can read the run directory and a separate worktree. Writes are denied. Stdin closed with approval `never` runs without prompting.
- Output: `--output-schema`, `--output-last-message` and `--json` together gave a final message that parsed as JSON in every run and matched the JSONL stream. Behaviour with any of the three omitted is unconfirmed.
- Network: the read-only reviewer shell could reach neither the loopback HTTP server nor `https://example.com/`; both returned HTTP 200 in the control shell. Public numeric-IP egress and non-shell network tools are unconfirmed. This is residual risk and `panel run` performs no runtime network check.
- Parallelism: 3 and 8 concurrent processes both finished in about 10 s with no rate limit seen. That is one account and a short prompt; sustained throughput on real prompts is unconfirmed.
- Models seen in the catalog and on OpenAI's models page: `gpt-6.2-astra`, `gpt-6.2-sol`, `gpt-6-luna` (and others). Live requests to Astra and Luna are unconfirmed.
- Shared Codex homes log harmless skill-install and shell-snapshot errors under concurrency.

## Layout

```
codex/skills/<name>/SKILL.md              generated from skills/<name>/SKILL.md
src/codecheck/commands/panel.py           `panel run`: starts the reviewers and the aggregator
AGENTS.md                                 development guidance for Codex
docs/INSTALL-CODEX.md                     install, uninstall, configuration, first run
docs/superpowers/spikes/2026-11-19-codex.md   what Codex actually does (Phases 0 and 0b)
```

There is no `codex/agents/`: the reviewer prompts come from `agentgen` and go to `codex exec` at run time. `CODE_CHECK.md` is shared: every reviewer prompt reads it first, as the Claude agents do.

## Design

**Models.** Add a `[codex_models]` table with the same keys as `[models]` (`aggregator`, `language_experts`, `reviewers`, `deep`, `security`, `performance`, `judge`, `per_language`) and a `[codex_reasoning]` table with the same keys for the effort. `model_for()` gains a `runtime` argument defaulting to `"claude"`, so every existing call is unchanged. Defaults, from the spike:

| Claude | Roles | Codex model | Effort |
|---|---|---|---|
| opus | aggregator, security, judge | `gpt-6.2-astra` | high |
| sonnet | architect, skeptic, pragmatist, performance, deep | `gpt-6.2-sol` | medium |
| haiku | the 15 language experts | `gpt-6-luna` | high |

These stay configurable in user config. A repo's `.code-check.toml` may not set `codex_models`, `codex_reasoning` or any `codex.*` key (they choose what runs and with which sandbox), the same as the `run_pr.*` keys. A model value must not start with `-`; it reaches argv as `--model=<value>`, never as a free word.

**Prompts.** `agentgen.py` gains a function that renders a role's prompt for a run (`RUN_DIR`, owned files). The Codex prompt is the shared role text plus an output step that says: return your findings as the final message, as JSON matching the schema; do not write any file. The Claude agent files, and so `gen-agents`, are untouched.

**`panel run`.** New subcommand (add it to both the import list and `ALL` in `commands/__init__.py`). For a run it:
1. Builds one `codex exec` argv per panel member from the plan `context` already holds, as a list (no shell): `-s read-only`, `-C <worktree>`, `--ephemeral`, `--ignore-user-config`, `--disable hooks`, `--model=<model>`, `-c approval_policy="never"`, `-c model_reasoning_effort=<effort>`, `--output-schema <schema>`, `--output-last-message <capture>`, `--json`. The prompt goes on stdin or after `--`, never as a bare word an option parser could take. Stdin is otherwise closed. `-s read-only` is fixed in code and cannot be set from any config.
2. Writes each capture file under the run directory, outside the checkout, and runs the members with a cap (`codex.max_parallel`, default 4; 8 worked in the spike).
3. Parses each capture as JSON, checks it against the finding schema, and writes `findings/<name>.json` itself, with the path through `commands/_common.contained()`. A member that exits non-zero, times out, or returns something that does not parse is reported as a **failed member**. It is never turned into `[]`, because a reviewer that failed is not a reviewer that found nothing.
4. Runs the aggregator the same way, reading the findings files, and writes the aggregator output the file `ingest` already expects.
5. Prints one JSON document: members run, failed members, files written.

Findings from Codex reviewers go through the same `ingest` validation as Claude's, and their message text through the existing scrub.

**Skills.** Generate `codex/skills/` from `skills/` with a fixed substitution table. The panel-dispatch step (the `Agent` call with `subagent_type` `code-check:<member>`, then the aggregator call) becomes one call to `code-check panel run`. The `${CLAUDE_PLUGIN_ROOT}` path becomes the absolute install path. A static test fails if a Claude-only construct survives, and `tests/test_plugin_static.py` cross-checks the Codex skills against the real argparse parser. Generating instead of copying avoids the duplicated step sequences `CODE_CHECK.md` item 1 flags.

**Install.** `installer/` gains `--target codex`. It copies `codex/skills` into `$HOME/.agents/skills/` (not `CODEX_HOME`), records a receipt, and uninstalls only what the receipt lists, refusing anything outside that directory. It writes the absolute CLI path into the skills at install time. `bin/code-check` already bootstraps its own venv, so the CLI needs no change. `doctor` gains a `codex` check (installed, version, logged in).

**AGENTS.md.** Short. Read `CLAUDE.md` for the architecture and invariants (they apply to Codex too); the commands to run; generated files are never hand-edited. It must not restate `CLAUDE.md`, so the two cannot disagree.

## Phases

0. **Spike** (done): `docs/superpowers/spikes/2026-11-19-codex.md`, Phases 0 and 0b.
1. **Guidance.** `AGENTS.md`. Commit it with the spike. No behaviour change. First PR.
2. **Config, prompts and `panel run`.** `codex_models`, `codex_reasoning`, `codex.*` config and the repo-config rejection, the prompt renderer, `panel run`, Codex skill generation, tests. Show that `agents/` and `skills/` are unchanged and `eval --replay` passes.
3. **Install.** Installer target, uninstall, doctor, `docs/INSTALL-CODEX.md`, a README section that links to it. `tests/test_readme.py` checks the README against the CLI and config keys, so update them together.
4. **Release 1.0.0.** Bump every place the version lives: `VERSION`, `pyproject.toml`, `package.json`, `package-lock.json` (both entries), `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `src/codecheck/__init__.py`. Add `codex/` and `src/codecheck/commands/panel.py` to the paths in rule 3 of `CODE_CHECK.md`. Tag after merge.

Phases 2 to 4 are the second PR. Before Phase 2 ends, run one real review end to end on a Codex machine and record it in the spike file.

## Risks

| Risk | Level | Mitigation |
|---|---|---|
| A reviewer can read Codex's own auth file and other credentials under the user's home. Prompt injection in a PR could try to put that material in a finding that is then posted | Medium | Ingest applies the scrubber to every finding's message and suggestion. It recognises patterns including JWT-shaped strings, `sk-` keys, bearer values and `token=...` assignments. It redacts matches in messages and withholds an entire suggestion if it matches. It can miss bare opaque tokens and private text without a recognised secret pattern |
| Public numeric-IP egress and network access via non-shell tools remain unconfirmed after the reviewer shell failed to reach loopback and `example.com` | Medium | The CLI fixes the measured read-only argv. Residual risk remains and there is no runtime network check |
| A failed reviewer read as "no findings" | High | A failed member is reported and makes the run's result say so; a test covers a non-zero exit, a timeout and unparseable output |
| The `-s read-only` setting weakened by a config or flag | High | Fixed in code; `codex.*` keys rejected in repo config; a test asserts the argv for every member |
| Guessed model names are wrong or retired | Medium | Defaults come from the catalog and are overridable; Astra and Luna are not yet tested live |
| Rate limits on a real review (up to 15 language experts plus the panel and aggregator) | Medium | `codex.max_parallel` cap; the end-to-end run measures it |
| A Claude regression | Medium | Byte-identical `agents/` and `skills/`; replay gate |
| Skill drift between targets | Medium | Generate, with a drift test |
| The merge guard accepts only one-step patch bumps, so 1.0.0 is refused | Low | The guard governs unattended `handoff-round`; do this release by hand and do not edit the guard |

## Testing

Claude outputs: the existing drift tests pass with no regenerated file. `panel run` (with `FakeRunner`, never a real `codex`): the argv for each member is exactly the flags above, with `-s read-only`, the model as `--model=<value>`, and no shell; a model starting with `-` is refused; `codex.*` in a repo config is rejected; a failed, timed-out or unparseable member is reported and writes no findings file; a valid capture writes `findings/<name>.json` through `contained()`; the cap bounds concurrency. Codex skills: no Claude-only construct, and every `code-check` subcommand and flag they use exists in the argparse parser. Config: defaults, per-language override, `model_for(..., runtime="claude")` unchanged. Installer (`node --test`): the Codex install writes the receipt and the absolute path; uninstall removes only receipt entries and refuses anything outside `$HOME/.agents/skills`; the Claude install is unchanged.

## Open decisions

- Whether to make a Codex runner for `loop` and `run_pr` part of 1.0.0. This spec says follow; `panel run` makes it small.
- Whether a Codex-side live eval is wanted to judge the model mapping, beyond configuring it. It costs tokens and is advisory, like `eval --calibrate`.
- Default for `codex.max_parallel` once the end-to-end run has measured a real review.
