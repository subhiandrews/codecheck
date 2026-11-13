# Hand-off data, golden set and eval gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in hand-off (session signals, blind PR reviews, human-comment comparison), a promotion path to an in-repo golden set, a replay eval that runs in `pytest` and CI, a local live snippet eval, commit gates, and a skip for already-approved PRs.

**Architecture:** Deterministic CLI work stays in Python (`shadow/`, `core/`, new `evals/`, new `commands/`); skills only gain one `handoff` step. Replay reuses the same pure pipeline functions `post` uses (extracted, not copied). The live eval drives the real plugin through `claude -p` against throwaway repos and reads the result back from the shadow store.

**Tech Stack:** Python 3.14, pytest, ruff, argparse CLI returning one JSON document, bash for gates, `gh` and `git` behind existing interfaces.

**Spec:** `docs/superpowers/specs/2026-11-13-handoff-and-golden-eval-design.md`

## Global Constraints

- Python >= 3.14 (CI asserts >= 3.14.10); `.venv/bin/ruff check .` and `.venv/bin/pytest -q` must pass at every commit.
- Every CLI subcommand prints exactly one JSON document on stdout; errors are JSON too. A new command goes in both the import list and `ALL` in `commands/__init__.py`.
- Skills and README are cross-checked against the argparse parser by `tests/test_plugin_static.py` and `tests/test_readme.py`: change a command, flag or config key and update both.
- `[shadow]` config is user-config-only (already in `REPO_FORBIDDEN`); `shadow.handoff` default is `false`.
- Shadow side effects go through `ctx.guarded(...)` or an equivalent catch; recording never changes a command's outcome. Everything written to the shadow store or golden files is scrubbed (`shadow/scrub.py`).
- Anything from a PR that reaches git/gh/claude argv must never act as an option; finding or PR file paths go through `commands/_common.contained()`.
- Tests use `FakeProvider`/`FakeRunner` (`tests/fakes.py`); no test calls real `gh`, `git` remotes or `claude`.
- Never force-push; never post to draft PRs; the tool does not review its own PRs.
- Hook-time env vars are unverified (spike): nothing may depend on them.
- **Invisible when off:** with `shadow.handoff = false`, a review run must show no sign of hand-off: no new keys in any command's JSON output, `context.json` or shadow records, no extra `gh` or provider calls, no extra tool calls by the skill, no extra files. Only `--help`, README and the skill text (static) mention it. Every task that touches a run path adds a test that the `handoff=false` output equals today's output.
- Live eval pass bar: at least 80% of a case's expected findings matched, file equal, line within ±2, category equal, severity equal; no `must_not_report` hit; no unexpected Critical.

## Review Focus

- Claude Code transcript directory missing, unreadable, or containing truncated/garbled JSONL lines: the hand-off is still written with `transcript: null` or skipped lines counted (Task 4, Task 5).
- Secrets inside transcript text (tokens in tool output): scrubbed in every file `handoff` writes (Task 4).
- `shadow.handoff = false`: `context` output, `context.json` and shadow records are key-for-key identical to today, no `repo_is_private` call is made, the skill never invokes `handoff`, and a direct `handoff` call is a no-op (Tasks 3 and 5).
- Multiple reviewers, a `ghost` (deleted) reviewer, an approval then changes-requested, an approval on an old head: the approval skip must follow the spec rules (Task 1).
- A malformed or hand-edited golden case file: a clear `CodeCheckError` naming the file, never a traceback (Task 6).

---

### Task 1: Skip PRs that already have an approval

**Files:**
- Modify: `src/codecheck/core/precheck.py`, `src/codecheck/commands/_common.py`, `src/codecheck/commands/precheck.py`, `src/codecheck/commands/list_work.py`
- Test: `tests/test_precheck.py`, `tests/test_cli_review.py`, `tests/test_loop.py` (or the existing list_work test file)

**Interfaces:**
- Produces: `approved_by(reviews: Sequence[Review], head_sha: str) -> str | None` in `core/precheck.py`; `approval_skip(ctx, ref, pr) -> str | None` in `commands/_common.py` returning `"already approved by <login>"` or `None`.

- [ ] **Step 1: Write failing tests** in `tests/test_precheck.py`: `test_approved_by_rules` asserting, with `Review(author, commit_id, submitted_at, state)`: approval at head returns the login; approval at an older `commit_id` returns `None`; `APPROVED` then later `CHANGES_REQUESTED` by the same author returns `None`; `APPROVED` then later `COMMENTED` returns the login; `APPROVED` then `DISMISSED` returns `None`; a `ghost` author's approval counts; empty `submitted_at` (pending) is ignored; two reviewers where only one still approves returns that one. In `tests/test_cli_review.py` add `test_precheck_pr_skips_approved_head` (FakeProvider `reviews[ref] = [Review("bob", head, "2026-01-01T00:00:00Z", "APPROVED")]` gives `proceed` false, reason contains `already approved by bob`, `--force` gives `proceed` true). In the list_work tests add `test_list_work_skips_approved_pr` expecting the PR under `skipped` with that reason.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_precheck.py tests/test_cli_review.py -q -k approv`. Expected: FAIL (names undefined).
- [ ] **Step 3: Implement** `approved_by`: per author take the latest review by `submitted_at` among states `APPROVED`, `CHANGES_REQUESTED`, `DISMISSED` (ignore `COMMENTED` and empty `submitted_at`); return the first author, sorted, whose latest is `APPROVED` and whose `commit_id == head_sha`. Implement `approval_skip` calling `ctx.provider.list_reviews(ref)`. In `commands/precheck.py` PR mode, after `cheap_skip` returns `None` and when not `args.force`, return the usual output shape with `proceed: false` and the approval reason. In `list_work`'s per-PR skip function return `(None, reason)` after the fork check and before `review_verdict`.
- [ ] **Step 4: Run** the same command plus `.venv/bin/pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: skip PRs that already have an approval at the current head`.

---

### Task 2: `shadow.handoff` flag and repo privacy lookup

**Files:**
- Modify: `src/codecheck/config.py`, `src/codecheck/cli.py`, `src/codecheck/providers/base.py`, `src/codecheck/providers/github.py`, `tests/fakes.py`, `README.md`
- Test: `tests/test_config.py`, `tests/providers/` (the provider contract suite), `tests/test_readme.py` passes

**Interfaces:**
- Produces: `ShadowConfig.handoff: bool = False` (third field, after `store`); `Provider.repo_is_private(self, repo: str) -> bool | None`; `FakeProvider.private: bool | None = None` (the fake returns it).

- [ ] **Step 1: Write failing tests:** `test_shadow_handoff_defaults_false_and_env_override` (`CODE_CHECK_SHADOW__HANDOFF=1` gives `True`; the repo `.code-check.toml` setting `[shadow] handoff = true` raises `ConfigError` naming `shadow`); a `build_context` test that a `Config` with `enabled=False, handoff=True` still produces a `ShadowStore`; a provider contract test that `repo_is_private` returns `True`/`False` from `gh repo view <repo> --json isPrivate --jq .isPrivate` output and `None` when `gh` fails (use the existing `FakeRunner` for the GitHub adapter).
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_config.py tests/providers -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the field; `build_context` creates the store when `config.shadow.enabled or config.shadow.handoff`; GitHub `repo_is_private` using the existing `_repo()` validator and `_gh`, catching `ProviderError` to return `None`. The spec named the override `CODE_CHECK_SHADOW_HANDOFF`; the existing mechanism uses double underscores (`CODE_CHECK_SHADOW__HANDOFF`), so use that. README: document `handoff` under `[shadow]` and the env name, in the Shadow mode section.
- [ ] **Step 4: Run** `.venv/bin/pytest -q` and `.venv/bin/ruff check .`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: add shadow.handoff flag and repo privacy lookup`.

---

### Task 3: Blind context in hand-off runs

**Files:**
- Modify: `src/codecheck/commands/context.py`, `src/codecheck/shadow/recorder.py`
- Test: `tests/test_existing.py` (where context and existing comments are tested) or `tests/test_cli_review.py`

**Interfaces:**
- Consumes: `ShadowConfig.handoff`, `Provider.repo_is_private`.
- Produces: only when `cfg.shadow.handoff` is true, the `context` output gains `handoff: true` and `blind: bool` (this is the skills' signal to run the hand-off step), `context.json` gains `handoff`, `blind` and `repo_private: bool | None`, and the shadow `run` record gains `blind`, `handoff`, `repo_private`. When it is false none of these keys exist anywhere.

- [ ] **Step 1: Write failing tests:** `test_context_blind_when_handoff_on` (PR mode, `handoff=True`, human threads on the FakeProvider): no `existing-comments.md`, `FakeProvider.thread_calls` and `review_calls` empty, output `blind` is `True`, `replies` and `existing_comments` are `None`; `test_context_unchanged_when_handoff_off` (the output has none of `handoff`, `blind`, `repo_private`; the same holds for `context.json` and the shadow `run` record; `FakeProvider` records no `repo_is_private` call; existing comments are still written); `test_context_records_repo_private` (handoff on, `FakeProvider.private = False` appears in the shadow `run` record); local mode with handoff on has `blind` `False` and `repo_private` `None`.
- [ ] **Step 2: Run** the new tests. Expected: FAIL.
- [ ] **Step 3: Implement** in `_build`: `blind = args.mode == "pr" and cfg.shadow.handoff`; when blind skip the whole `try:` block that lists threads and reviews. Only when `cfg.shadow.handoff` is on, set `repo_private` with `ctx.provider.repo_is_private(ref.repo)` inside a bare `except Exception` returning `None`, add the keys listed under Produces, and have `record_run` add its three fields only when `meta` has `handoff`. With the flag off, do not touch `meta`, the output or the record.
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: withhold existing comments from reviewers in hand-off runs`.

---

### Task 4: Transcript discovery and signal extraction

**Files:**
- Create: `src/codecheck/shadow/transcripts.py`
- Test: `tests/test_transcripts.py`

**Interfaces:**
- Produces:
  - `find_transcript(projects_dir: Path, run_id: str, *, max_age_s: float = 86400, now: Callable[[], float] = time.time) -> Path | None`: among `projects_dir/*/*.jsonl` modified within `max_age_s`, newest first, the first whose bytes contain `run_id`.
  - `subagent_transcripts(main: Path) -> list[Path]`: sorted `main.with_suffix("")/"subagents"/*.jsonl`, empty if absent.
  - `extract_signals(paths: Sequence[Path]) -> dict` with keys `tool_calls: int`, `tool_errors: list[{"agent","tool","error"}]` (error text scrubbed, at most 300 chars), `retries: int` (an identical tool name and input repeated right after an error), `permission_denials: int` (errors whose text contains `permission`, case-insensitive), `agents: list[{"name","tool_calls","tool_errors","turns","output_tokens"}]` (name is the file stem), `unreadable_lines: int`.
  - `copy_scrubbed(paths: Sequence[Path], dest: Path) -> list[str]`: writes scrubbed copies (mode 0600, directory 0700), returns the written file names.
  - Format (verified on this machine): one JSON object per line; `type == "assistant"` with `message.content` blocks `{"type":"tool_use","id","name","input"}` and `message.usage.output_tokens`; `type == "user"` with blocks `{"type":"tool_result","tool_use_id","is_error"}`.

- [ ] **Step 1: Write failing tests** with synthetic JSONL in `tmp_path`: `test_find_transcript_picks_newest_containing_run_id` (two files contain the id, the newer wins; a file older than `max_age_s` is ignored; a `subagents/` file is never returned as main); `test_find_transcript_missing_dir_returns_none`; `test_extract_signals_counts_errors_retries_denials` (assert exact counts and that `agents` has one entry per file); `test_extract_signals_skips_garbled_lines` (a truncated line increments `unreadable_lines`, nothing raises); `test_extract_and_copy_scrub_secrets` (a `ghp_`-style token in a tool error and in a copied file is absent from both outputs).
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_transcripts.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the four functions; pair `tool_result` to `tool_use` by id to name the tool; use `scrub_text` from `shadow/scrub.py`.
- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_transcripts.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: find and summarise Claude Code session transcripts`.

---

### Task 5: The `handoff` command and skill steps

**Files:**
- Create: `src/codecheck/shadow/handoff.py`, `src/codecheck/commands/handoff.py`
- Modify: `src/codecheck/shadow/store.py`, `src/codecheck/commands/__init__.py` (import and `ALL`), `skills/review-local/SKILL.md`, `skills/review-pr/SKILL.md`, `README.md`
- Test: `tests/test_handoff.py`, `tests/test_shadow_store.py`

**Interfaces:**
- Consumes: `find_transcript`, `subagent_transcripts`, `extract_signals`, `copy_scrubbed` (Task 4); `collect_outcomes(provider, shadow, ref, me)` from `shadow/collect.py`; `ShadowConfig.handoff`.
- Produces:
  - `ShadowStore.save_raw(run_id: str, data: dict) -> None` and `read_raw(run_id: str) -> dict | None` (file `<run_id>.raw.json`, scrubbed, 0600); `ShadowStore.handoff_dir(run_id: str) -> Path` (`<root>/<run_id>.handoff`, created 0700 on demand).
  - `shadow/handoff.py`: `implied_severity(body: str) -> str | None` (returns `critical|warning|suggestion` only when the comment starts, after optional markdown emphasis, with that word, case-insensitive); `build_comparison(records: Sequence[dict], findings: Sequence[dict]) -> dict` with keys `human_comments` (each `thread_id, path, line, author, matched_finding, human_severity, finding_severity, severity_agrees`), `unraised_findings` (finding ids no human comment matched), `misses` (thread ids with no match); `render_markdown(handoff: dict) -> str`.
  - `code-check handoff --run <id>` returns `{"handoff": false}` when the flag is off, otherwise `{"handoff": true, "dir": str, "pr_url": str | None, "transcript": bool, "comparison": bool | str}` (a string is the reason it was skipped). Any exception becomes `{"handoff": false, "error": str}` with exit code 0.
  - Files in `handoff_dir`: `handoff.json` (schema 1), `handoff.md`, `transcript/` (scrubbed copies). `handoff.json` keys: `run_id, skill, mode, repo, pr, pr_url, head_sha, tool_version, models, panel, blind, reviewers` (per member: output present, valid count, from `ingest.json`), `signals, findings` (from `findings/aggregator.json`: id, file, line, severity, category), `decision` (from the shadow `decision` record or null), `comparison`, `transcript` (bool plus `reason` when null).

- [ ] **Step 1: Write failing tests** in `tests/test_handoff.py` (use `make_ctx`, `finish_review`-style helpers from `test_cli_review.py`): `test_handoff_noop_when_flag_off` (a direct call returns `{"handoff": false}`, no `.handoff` dir, no provider calls); `test_skills_gate_handoff_on_context_flag` (both skill files contain the `handoff: true` condition text); `test_handoff_local_mode_writes_json_and_md` (`pr_url` is `None`, findings listed, no `comparison`); `test_handoff_pr_mode_compares_human_comments` (after `post`, FakeProvider has a human thread within 3 lines of a finding saying `Warning: ...` and one far from any finding; assert `matched_finding` set on the first, `human_severity == "warning"`, the second in `misses`, our unmatched finding in `unraised_findings`, `pr_url` equals the FakeProvider PR `url`); `test_handoff_skips_comparison_when_not_posted` (draft PR: `comparison` is a reason string); `test_handoff_transcript_missing` (`transcript` false with a reason, files still written); `test_handoff_saves_raw_outputs` (`read_raw(run_id)` has `reviewers`, `aggregator`, `panel`); `test_handoff_error_is_swallowed` (monkeypatch a failing step: output `handoff: false`, exit 0); `test_implied_severity_cases` (`"**Critical** · x"` yes, `"this is critical"` no, `"Suggestion: x"` yes). Store tests: `save_raw` scrubs and `read_raw` round-trips; unknown run returns `None`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_handoff.py tests/test_shadow_store.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the store methods, the pure functions, and the command. The command reads the run directory (`context.json`, `ingest.json`, `findings/*.json`), so it must run before `cleanup`. Transcript search root: `$CLAUDE_CONFIG_DIR/projects` if set, else `~/.claude/projects`. In PR mode, comparison runs only if a shadow `decision` record exists for the run (the review was posted): call `collect_outcomes`, then build the comparison from `ctx.shadow.read(run_id)`. Skills: add a step "If the `context` output contains `handoff: true`, run `code-check handoff --run <run_id>` and ignore its output; otherwise skip this step" immediately before the cleanup step in `review-local` and `review-pr` (PR-argument path), so a run with the flag off makes no extra call. Register the subcommand with `help=argparse.SUPPRESS` so it is absent from `code-check --help` (if the static plugin or README tests need it listed, keep the README entry and leave the subparser listed). README: document the command, the files it writes, blind mode, and the privacy note (transcripts are scrubbed best-effort).
- [ ] **Step 4: Run** `.venv/bin/pytest -q` and `.venv/bin/ruff check .`. Expected: PASS (includes the static plugin and README checks).
- [ ] **Step 5: Commit** `feat: add the handoff command, blind comparison and raw output capture`.

---

### Task 6: Replay eval

**Files:**
- Create: `src/codecheck/core/pipeline.py`, `src/codecheck/evals/__init__.py`, `src/codecheck/evals/golden.py`, `src/codecheck/commands/eval_run.py`, `tests/golden/seed-pipeline.json`
- Modify: `src/codecheck/commands/post.py`, `src/codecheck/commands/__init__.py`, `.github/workflows/ci.yml`, `README.md`
- Test: `tests/test_pipeline.py`, `tests/test_golden_replay.py`

**Interfaces:**
- Produces:
  - `core/pipeline.py`: `finalize_findings(findings: list[Finding], diff_files) -> list[Finding]` (attach anchors, merge exact duplicates, sort by severity rank descending then file then line, exactly as `post` does today); `ReviewPlan` frozen dataclass `(inline: list[tuple[Finding, <anchor from anchor_finding>]], not_inline: list[Finding], repeated: int)`; `plan_review(findings, diff_files, *, already: set[str], cap: int) -> ReviewPlan`. `post.py` is refactored to call both; behaviour unchanged.
  - Golden case JSON (schema `version: 1`): `case_id, repo, pr, head_sha, diff, reviewer_outputs: {member: [finding dicts]}, aggregator: <raw aggregator JSON>, expected_posted: [{file, line, category, severity, inline}], expected_decision: "COMMENT"|"APPROVE", labels: [{id, file, line, severity, label}], misses: [...], signals: dict | null, label_status: "confirmed", repo_private: bool | null`.
  - `evals/golden.py`: `load_cases(directory: Path) -> list[dict]` (raises `CodeCheckError` naming the file on bad JSON or a missing required key); `posted_view(aggregator: object, diff: str, *, auto_approve: bool = False, cap: int = 25) -> dict` returning `{"findings": [...as expected_posted...], "decision": str}` using `validate_findings`, `finalize_findings`, `plan_review`, `decide`; `check_case(case: dict) -> list[str]` (problems: a reviewer output that no longer validates to the same count, posted findings or decision differing from the expected ones, each message naming the finding); `run_replay(directory: Path) -> dict` returning `{"passed": bool, "cases": int, "failed": [{"case", "problems"}]}`.
  - CLI: `code-check eval --replay [--cases DIR]` (default `tests/golden`, resolved against the plugin repo root); output is `run_replay`'s dict.

- [ ] **Step 1: Write failing tests:** `tests/test_pipeline.py::test_plan_review_caps_and_skips_repeats` (cap 1 of 2 anchored findings gives 1 inline, 1 not_inline; an `already` id counts as `repeated`; an unanchorable finding goes to `not_inline`); `tests/test_golden_replay.py::test_every_golden_case_replays` parametrized over `tests/golden/*.json` using `check_case(case) == []`; `test_check_case_reports_severity_change` (mutate the seed case's expected severity: problem text names the finding); `test_load_cases_names_bad_file` (truncated JSON: `CodeCheckError` whose message contains the filename); `test_eval_replay_command_reports_failures` (via `call(ctx, "eval", "--replay", "--cases", dir)` with a broken case: `passed` false, `failed[0]["case"]` set). Existing `tests/test_cli_review.py` must pass unchanged after the refactor.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_pipeline.py tests/test_golden_replay.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the pipeline extraction first (run `.venv/bin/pytest tests/test_cli_review.py -q` green), then `evals/golden.py` and the command (`NAME = "eval"`, mutually exclusive required `--replay`/`--live` group; only `--replay` exists in this task, add `--live` in Task 8). Hand-build `tests/golden/seed-pipeline.json` from synthetic code (a 6-line diff of an invented file, two reviewer outputs, an aggregator output with one duplicate), no real project code. CI: add a `python -m codecheck eval --replay` step to the `python` job in `ci.yml` after `pytest -q` and make it fail the job when `passed` is false (`... | python -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["passed"] else 1)'`). README: document `eval --replay`.
- [ ] **Step 4: Run** `.venv/bin/pytest -q`, `.venv/bin/ruff check .`, and `.venv/bin/python -m codecheck eval --replay`. Expected: PASS and `"passed": true`.
- [ ] **Step 5: Commit** `feat: replay eval against golden cases, run in pytest and CI`.

---

### Task 7: Promote hand-offs to golden cases

**Files:**
- Create: `src/codecheck/shadow/promote.py`
- Modify: `src/codecheck/commands/shadow.py`, `README.md`
- Test: `tests/test_promote.py`

**Interfaces:**
- Consumes: `ShadowStore.read_raw/read_diff/read`, `posted_view` and `check_case` (Task 6), `shadow/export.py` label logic.
- Produces:
  - `draft_case(shadow: ShadowStore, run_id: str) -> dict`: builds a golden case with `label_status: "auto"`; each label is `expected` when the finding's outcome is resolved/outdated or a human comment matched it, `not_expected` when it has replies disputing it, otherwise `"unlabelled"`; `expected_posted` and `expected_decision` come from `posted_view` over the stored aggregator output. Raises `CodeCheckError("no raw outputs recorded for <run>: only runs made with shadow.handoff can be promoted")` when `read_raw` is `None`, and when the run has no diff.
  - `finalize_case(path: Path, dest_dir: Path, *, allow_private: bool) -> Path`: refuses unless `label_status == "confirmed"`, no label is `"unlabelled"`, `check_case(case) == []`, and (`repo_private is False` or `allow_private`); writes `dest_dir/<repo with / as ->-<pr number>-<head_sha[:8]>.json` (0644, no symlink following), scrubbed.
  - CLI: `code-check shadow promote --run <id> --out <draft path>` and `code-check shadow promote --finalize <draft path> [--allow-private] [--dest tests/golden]`.

- [ ] **Step 1: Write failing tests** in `tests/test_promote.py`: build a shadow store by running the real flow with `FakeProvider` (reuse the Task 5 helpers) with `handoff=True`; `test_draft_requires_raw_outputs` (a run recorded without handoff raises with the message above); `test_draft_labels_unlabelled_by_default`; `test_finalize_refuses_unconfirmed`; `test_finalize_refuses_unlabelled`; `test_finalize_refuses_private_or_unknown_repo` (`repo_private` `True` and `None` both refuse; `--allow-private` passes); `test_finalize_writes_case_that_replays` (edit labels, set `confirmed`, finalize, then `check_case` on the written file returns `[]`); `test_finalize_scrubs` (token in a message is gone).
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_promote.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** both functions and wire the `promote` subparser (`--run`/`--out` and `--finalize` mutually exclusive). README: document the two-step flow and the privacy rule.
- [ ] **Step 4: Run** `.venv/bin/pytest -q` and `.venv/bin/ruff check .`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: promote confirmed hand-offs into the golden set`.

---

### Task 8: Live snippet eval

**Files:**
- Create: `src/codecheck/evals/match.py`, `src/codecheck/evals/live.py`, `tests/snippets/off-by-one/case.json`, `tests/snippets/clean-code/case.json`
- Modify: `src/codecheck/commands/eval_run.py`, `README.md`
- Test: `tests/test_eval_match.py`, `tests/test_eval_live.py`

**Interfaces:**
- Produces:
  - `evals/match.py`: `match_case(expected: Sequence[dict], must_not_report: Sequence[dict], got: Sequence[dict], *, tolerance: int = 2, recall_bar: float = 0.8) -> dict` with `passed, matched, expected, recall, severity_mismatches (expected item, got item), unexpected_critical, forbidden_hits`. A got finding matches an expected one when file and category are equal and `abs(line - line) <= tolerance`; it then counts as matched only if severity is equal (otherwise it is listed in `severity_mismatches`). Each got finding is used once. A `must_not_report` item hits a got finding with the same file and, when given, the same category and line within tolerance. `unexpected_critical` lists got Critical findings that match no expected item. `passed` needs `recall >= recall_bar` (1.0 when `expected` is empty), no forbidden hits, no unexpected Critical.
  - Snippet `case.json`: `{"name","base": {path: content}, "change": {path: content}, "expected": [{file,line,category,severity}], "must_not_report": [{file, category?, line?}]}`.
  - `evals/live.py`: `load_snippets(directory: Path) -> list[dict]` (errors name the file); `claude_reachable(ctx) -> bool` (runs `[claude_bin, "-p", "reply with ok", "--output-format", "json", "--permission-mode", "dontAsk"]` via `ctx.runner` with a 60 s timeout; True only when the JSON parses and `is_error` is false); `run_snippet(ctx, snippet: dict, workdir: Path) -> dict` returning `{"name", "result": <match_case dict>, "got": [...]}`: creates a bare origin and a clone under `workdir` with `git` through `ctx.runner`, commits `base` on `main` and pushes, applies `change` as an uncommitted edit, then runs `claude -p "Use the code-check review-local skill. Run unattended: never ask questions; finish with one line summarizing the outcome." --plugin-dir <plugin root> --output-format json --permission-mode dontAsk --permission-prompts none --add-dir <workdir> --allowedTools <run_pr.review_allowed_tools>` with cwd the clone, argv prefixed with `env CODE_CHECK_STATE_DIR=<workdir>/state CODE_CHECK_SHADOW__STORE=<workdir>/shadow CODE_CHECK_SHADOW__HANDOFF=1` so nothing touches the user's real state; findings are read from `ShadowStore(<workdir>/shadow)` newest run's `read_raw(...)["aggregator"]`. Plugin root: `$CLAUDE_PLUGIN_ROOT` if set, else the repository root two levels above `src/`.
  - CLI: `code-check eval --live [--snippets DIR] [--only NAME] [--if-reachable]` (default `tests/snippets`). Without `--if-reachable`, an unreachable model raises `CodeCheckError("model not reachable: ...")`; with it the output is `{"passed": true, "skipped": "<reason>", "cases": []}`. Output otherwise `{"passed": bool, "cases": [...]}`.

- [ ] **Step 1: Write failing tests:** `tests/test_eval_match.py`: `test_match_within_tolerance_and_severity` (line off by 2 matches, off by 3 does not); `test_severity_mismatch_not_counted` (right place, wrong severity: `matched` 0 and one `severity_mismatches` entry); `test_recall_bar` (4 of 5 passes, 3 of 5 fails); `test_unexpected_critical_fails`; `test_forbidden_hit_fails`; `test_got_finding_used_once`. `tests/test_eval_live.py` with `FakeRunner`: `test_live_unreachable_raises_without_flag`; `test_live_unreachable_skips_with_flag`; `test_run_snippet_builds_isolated_argv` (the recorded claude argv starts with `env`, contains the three `CODE_CHECK_` assignments pointing under `workdir`, `--plugin-dir`, and no argument that comes from the snippet content); `test_load_snippets_names_bad_file`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_eval_match.py tests/test_eval_live.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the modules and extend `eval_run.py`. Write the two snippets: `off-by-one` (a small Python function with a loop bound bug, one expected Warning-or-higher finding) and `clean-code` (correct code, `must_not_report` a Critical anywhere). Category strings in the snippets must be the ones the reviewers actually emit: run `code-check eval --live --only off-by-one` once by hand and set categories from what the panel produced, then check that the expected severity is a defensible judgment, not just whatever came out. README: document `eval --live`, cost, and that it is local-only.
- [ ] **Step 4: Run** `.venv/bin/pytest -q` and `.venv/bin/ruff check .` (unit tests only; the hand run in Step 3 is the one real-model check). Expected: PASS.
- [ ] **Step 5: Commit** `feat: live snippet eval with tolerant matching`.

---

### Task 9: Commit gates

**Files:**
- Create: `scripts/gates.sh`, `scripts/gate-paths.txt`, `scripts/install-git-hooks.sh`, `.githooks/pre-commit`
- Modify: `.claude/settings.json`, `README.md` (a short "Development" note), `CLAUDE.md` (Commands section: the gates and `eval`)
- Test: `tests/test_gates.py`

**Interfaces:**
- Produces: `scripts/gates.sh [--if-no-git-hook]`: with `--if-no-git-hook`, exit 0 at once when `git config core.hooksPath` equals `.githooks` (the git hook will run the gates, so a commit made by Claude does not run them twice). Otherwise run `eval --replay` always (non-zero exit when `passed` is false), then run `eval --live --if-reachable` only when `git diff --cached --name-only` matches an extended regex in `scripts/gate-paths.txt`. `scripts/gate-paths.txt`, one ERE per line: `^src/codecheck/agentgen\.py$`, `^agents/`, `^skills/`, `^src/codecheck/config\.py$`, `^src/codecheck/core/(ingest|findings|diffmap|policy|pipeline|render)`, `^src/codecheck/commands/(ingest|post)\.py$`, `^src/codecheck/evals/`. (`ingest` logic lives in `commands/ingest.py` and `core/findings.py`; confirm the list against the tree.)
- The `.claude/settings.json` commit hook gains a third `PreToolUse` command (`if: Bash(git commit*)`, timeout 900 s) running `scripts/gates.sh --if-no-git-hook`.

- [ ] **Step 1: Write failing tests** in `tests/test_gates.py` (use `subprocess` against the scripts in a temp git repo with the real `.venv` python stubbed by an env override `CODE_CHECK_BIN` that the script honours, pointing at a tiny fake that prints `{"passed": true}` or `{"passed": false}` and records its argv): `test_gates_runs_replay_always`; `test_gates_runs_live_only_for_gated_paths` (stage `README.md`: no live call; stage `agents/x.md`: live call with `--if-reachable`); `test_gates_fail_when_replay_fails`; `test_if_no_git_hook_exits_early_when_hookspath_set`; `test_gate_paths_patterns_match_expected_files` (each pattern is a valid ERE and the listed examples match).
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_gates.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the scripts (default `CODE_CHECK_BIN` is `.venv/bin/python -m codecheck`; if missing, print a skip notice and exit 0 like the existing ruff hook). `.githooks/pre-commit` runs ruff, eslint (same skip-if-missing behaviour as the existing hooks), then `scripts/gates.sh`. `install-git-hooks.sh` runs `git config core.hooksPath .githooks`. Edit `.claude/settings.json` and document the install command in README and CLAUDE.md.
- [ ] **Step 4: Run** `.venv/bin/pytest -q`, `.venv/bin/ruff check .`, `node --test tests-node/*.test.js`, then `scripts/install-git-hooks.sh` and a throwaway `git commit --allow-empty -m test` on a scratch branch to see the hook run (then `git reset --soft HEAD~1`). Expected: PASS; hook prints replay result.
- [ ] **Step 5: Commit** `feat: run replay and path-gated live eval on commit`.

---

## Deviations from the spec (all small, for you to veto)

- Env override is `CODE_CHECK_SHADOW__HANDOFF` (the repo's existing double-underscore scheme), not `CODE_CHECK_SHADOW_HANDOFF`.
- Raw reviewer and aggregator outputs are copied by `handoff` (between `post` and `cleanup`), not by `post`: same data and timing, and it also covers local runs, which `post` never records. Note `ingest` rewrites reviewer files with their validated findings, so the stored reviewer outputs are the post-ingest form.
- Two-hooks-run-once uses `scripts/gates.sh --if-no-git-hook` (skip when the git hook is installed) instead of an environment variable, because the Claude hook and the git hook run in separate processes and cannot share one.
- `eval` reports pass/fail in its JSON (`passed`); the gate script and CI turn that into an exit code.
