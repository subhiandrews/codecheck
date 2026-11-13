# Spike: headless mode, subagent nesting, plugin mechanics

Date: 2026-11-12. CLI: `claude --version` -> `2.1.311 (Claude Code)`. Model in use: `claude-sonnet-5-5`.
All probes were run from inside a Claude Code session (nested `claude -p` works). Probe artifacts were throwaway and lived in the scratchpad.

## CLI surface (Step 1)

All expected flags exist with these spellings: `-p/--print`, `--output-format <text|json|stream-json>`, `--allowedTools, --allowed-tools <tools...>` (variadic), `--disallowedTools`, `--permission-mode <acceptEdits|auto|bypassPermissions|manual|dontAsk|plan>`, `--permission-prompts <host|none>` (print mode only), `--plugin-dir <path>` (dir or .zip, repeatable, session only), `--plugin-url`, `--agents <json-or-file>`, `--model`, `--fallback-model`, `--no-session-persistence`, `--input-format`.
- There is NO `--max-turns` in `claude --help` (grep found nothing). Do not use it.
- `--permission-mode` has no `default` choice; the choices include `manual`, `auto`, `dontAsk`.
- `claude plugin` subcommands: `configure, details, disable, enable, eval, init|new, install|i, list, marketplace, prune|autoremove, tag, test, uninstall|remove, update, validate`.
- `claude plugin marketplace` subcommands: `add <source>`, `list`, `remove|rm <name>`, `update [name]`.
- Note: with stdout piped and no stdin, `claude -p` prints "Warning: no stdin data received in 3s" to stderr and waits 3s. Redirect `< /dev/null` (and keep stderr out of the JSON stream) in scripts.

## (a) Can a subagent launch a subagent?

Command: the brief's Step 2 command (`--agents` with `outer` having tools `["Agent","Read"]` and `inner` replying PONG).
Observed: `"result":"The outer agent returned: **PONG**\n\nIt delegated to the inner agent, which replied PONG."`, and `subagent_stats` = `{"spawned":2, ..., "max_depth":2, "spawned_by_subagents":1, "completed":2, "failed":0, "refused":{"depth_limit":0,...}, "by_type":{"outer":1,"inner":1}}`.

Result: nesting WORKS (depth 2) when the outer agent is granted the `Agent` tool. Not tested beyond depth 2.

**Decision:** nesting is supported, but `run-pr` stays headless-per-PR (one `claude -p` per PR; single code path, also the remote path). In-session alternative: the orchestrating skill can launch reviewer subagents directly, and a reviewer agent that needs fan-out must list `Agent` in its `tools`. `subagent_stats` in the JSON is available for verifying depth/fan-out.

## (b) Can `claude -p` run a plugin skill by name?

Command: `claude -p "Use the spike:probe skill." --plugin-dir <spike-plugin> --output-format json --allowedTools "Bash"`.
Observed result:
```
ROOT=<scratchpad>/spike-plugin
DATA=/Users/subhiandrews/.claude/plugins/data/spike-inline
agents
skills
```
The skill ran (num_turns 4) from a natural-language request naming `spike:probe`. The same request also worked against the plugin installed from a marketplace.

**Decision:** natural-language invocation `Use the <plugin>:<skill> skill.` works in `-p`. No need to inline skill steps in Task 17's default prompts. Use the `code-check:<skill>` form in prompts.

## (c) Env vars in skills

`${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` are both substituted/available in skill bash blocks, both non-empty:
- `--plugin-dir` load: ROOT = the given directory (absolute path as passed); DATA = `~/.claude/plugins/data/<plugin>-inline` (here `spike-inline`).
- marketplace install: ROOT = the marketplace-source copy (`.../spike-market/spike`, for a relative-path source it points into the marketplace dir); DATA = `~/.claude/plugins/data/<plugin>-<marketplace>` (here `spike-spike-market`).

Not verified: availability inside hooks (no hook was probed). Docs convention is that both are also exported to hook commands, but this spike did not confirm it.

**Decision:** Tasks 16 and 18 use exactly `${CLAUDE_PLUGIN_ROOT}` (read-only plugin files, e.g. `bin/code-check`) and `${CLAUDE_PLUGIN_DATA}` (persistent state). Do not assume DATA is the same path between `--plugin-dir` and installed runs. Treat hook availability as unverified; if a hook needs them, add a probe in that task.

## (d) JSON shape of `claude -p --output-format json`

Single JSON object on stdout. Top-level keys (sorted): `api_error_status, duration_api_ms, duration_ms, fast_mode_disabled_reason, fast_mode_state, first_content_frame_ms, is_error, modelUsage, num_turns, permission_denials, queued_turn_count, result, result_index, safety_stops, session_id, stop_reason, subagent_stats, subtype, terminal_reason, time_to_request_ms, total_cost_usd, ttft_ms, ttft_stream_ms, type, usage, uuid`.
- Values seen: `type":"result"`, `subtype":"success"`, `is_error":false`, `terminal_reason":"completed"`, `stop_reason":"end_turn"`.
- Cost: `total_cost_usd` (float). Text: `result` (string). Turns: `num_turns`. Time: `duration_ms`, `duration_api_ms`.
- `usage` keys: `input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, cache_creation{ephemeral_1h_input_tokens,ephemeral_5m_input_tokens}, server_tool_use, service_tier, speed, inference_geo, iterations, output_tokens_details, fallback_credit`.
- `modelUsage` is keyed by model id (e.g. `claude-sonnet-5-5`, `claude-haiku-5-5`) with `inputTokens, outputTokens, cacheReadInputTokens, cacheCreationInputTokens, costUSD, contextWindow, ...`. This is the per-model (incl. subagent models) breakdown; `usage` covers the main-thread totals only (in the nesting run `usage.output_tokens` 183 vs `modelUsage` outputTokens 427).
- `permission_denials` is a list of objects with `tool_name` (empty when none).
- `subagent_stats`: `spawned, requested, max_depth, spawned_by_subagents, completed, failed, killed, refused, by_type`.
- stderr may carry the "no stdin data" warning; capture stdout only.

**Decision:** Task 17 parses `result`, `is_error`, `subtype`, `total_cost_usd`, `num_turns`, `duration_ms`, `usage.input_tokens`, `usage.output_tokens`, `usage.cache_read_input_tokens`, `usage.cache_creation_input_tokens`. For true total tokens across subagents sum `modelUsage.*.inputTokens/outputTokens/...`; `total_cost_usd` already includes subagent cost (nesting run: total 0.1277 equals the modelUsage costUSD sum). Treat `is_error`/`subtype != "success"` as failure.

## (e) Plugin CLI

Run against a throwaway marketplace (all exit 0, then cleaned up):
```
claude plugin marketplace add <dir>            -> "Successfully added marketplace: spike-market (declared in user settings)"
claude plugin install spike@spike-market       -> "Successfully installed plugin: spike@spike-market (scope: user)"
claude plugin list                             -> lists "spike@spike-market  Version: 0.0.1  Scope: user  Status: enabled"
claude plugin uninstall spike@spike-market     -> "Successfully uninstalled plugin: spike (scope: user)"
claude plugin marketplace remove spike-market  -> "Successfully removed marketplace: spike-market"
```
`marketplace remove` has alias `rm`; `uninstall` has alias `remove`; `install` has alias `i`. `marketplace add` accepts a local path (also URL or GitHub repo). Marketplace manifest used: `{"name","owner":{"name"},"plugins":[{"name","source":"./spike","description"}]}` at `.claude-plugin/marketplace.json`. Install scope defaults to `user`. Installed plugin is read in place from the marketplace directory (`Read from: .../spike-market/spike`), not copied, for a local-path marketplace. After cleanup `claude plugin list` and `marketplace list` showed only the pre-existing entries. Also available: `claude plugin validate <path>` for manifests and `--plugin-dir` for session-only loading.

**Decision:** Task 18 uses exactly `claude plugin marketplace add <path|repo>`, `claude plugin install <plugin>@<marketplace>`, `claude plugin uninstall <plugin>@<marketplace>`, `claude plugin marketplace remove <name>`. The CLI exists, so the copy-fallback is not needed as the primary path; keep it only as a fallback when `claude` is unavailable. Task 18's static check can also run `claude plugin validate`. Note installs change the real user config, so tests must always uninstall/remove.

## (f) Permission and tool flags for unattended runs

Test: prompt asks to Write `out.txt` and run `./tool.sh` via Bash. The machine's user setting is `permissions.defaultMode = "auto"`, so a bare run succeeds here and is NOT a valid baseline.

| Flags | Write | Bash |
|---|---|---|
| none (inherits user `auto` mode) | ok | ok |
| `--permission-mode manual --permission-prompts none` | denied | denied |
| `--permission-mode dontAsk` (no allowedTools) | denied | denied |
| `--permission-mode dontAsk --permission-prompts none --allowedTools "Write" "Bash(./tool.sh:*)"` | ok | ok |
| `--permission-mode manual --permission-prompts none --allowedTools "Write" "Bash(./tool.sh:*)"` | ok | ok |
| `--permission-mode acceptEdits --allowedTools "Write" "Bash(./tool.sh:*)"` | ok | ok |

Denials show up in `permission_denials[].tool_name`; the model then reports the denial rather than hanging. `--allowedTools` takes space-separated rules (variadic) and rule syntax `Bash(<prefix>:*)`. In plugin runs `--allowedTools "Agent"` was enough to launch a plugin agent and `"Bash"` for skill bash blocks.

**Decision:** for headless reviews use `--permission-mode dontAsk --permission-prompts none --allowedTools "Read" "Write" "Edit" "Agent" "Bash(<path-to>/bin/code-check:*)"` (extend with specific read-only Bash rules as needed). Always pass the mode explicitly so behaviour does not depend on the invoking user's `defaultMode`; deny-by-default with an explicit allowlist is the safest unattended setup. Put the `--allowedTools` list after the prompt or use `--allowed-tools=...` carefully since it is variadic and can swallow the positional prompt; pass the prompt via `-p "<prompt>"` first. Use `--output-format json`, `< /dev/null`. Do not use `--max-turns` (absent). The exact `Bash(...)` rule for an absolute `${CLAUDE_PLUGIN_ROOT}/bin/code-check` path was tested only with a relative `./tool.sh`; Task 17 should verify the absolute-path form.

## (g) Plugin agent `subagent_type`

Command: asked the model to launch the plugin's `pong` agent. Result: `"I used the subagent_type string spike:pong, and the agent replied PONG."` `subagent_stats.by_type` = `{"spike:pong":1}`; the haiku frontmatter `model: haiku` was honoured (`modelUsage` includes `claude-haiku-5-5`). Frontmatter `name: pong` in `agents/pong.md` plus plugin name `spike` gives `<plugin-name>:<agent-name>`.

**Decision:** format is `<plugin>:<agent>`, so `code-check:<agent>` as Task 18 assumes holds, provided the plugin's `plugin.json` `name` is `code-check` and each agent file's `name` matches the intended agent name. No plan change needed. (Agents passed via `--agents` are addressed by bare name, e.g. `outer`, `inner`.)

## Summary of contradictions with later-task assumptions

- None of (a)-(g) contradicts the plan. Caveats: `--max-turns` does not exist; hook-time env vars unverified; the absolute-path `Bash(...)` allow rule unverified; permission baseline depends on the user's `defaultMode`, so always set `--permission-mode` explicitly.
