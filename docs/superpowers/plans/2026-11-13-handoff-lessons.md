# Handoff Lessons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make hand-offs describe exactly one run, cut avoidable failed tool calls, tighten the aggregator's severity, merge and scope rules, and protect recall and noise with a live snippet pair and two golden cases.

**Architecture:** `shadow/transcripts.py` and `commands/handoff.py` scope signals to a run (subagents by their `RUN_DIR=` first message, the main session by timestamp window). Prompt changes live in `agentgen.py` and are regenerated into `agents/`. Provider gains one method and two fields for non-inline human feedback. Snippets and golden cases are data files checked by the existing `eval` code.

**Tech Stack:** Python 3.14, pytest, ruff, the `code-check` CLI (`.venv/bin/...`).

**Spec:** `docs/superpowers/specs/2026-11-13-handoff-lessons-design.md`

## Global Constraints

- Every CLI subcommand prints exactly one JSON document on stdout; hand-off failures never fail a review (`ctx.guarded` / the existing catch-all in `commands/handoff.py`).
- Hand-off output is scrubbed (`shadow.scrub.scrub` / `scrub_text`); `~/.claude` and `~/.local/share` are never read by tests (use `tmp_path` and `CLAUDE_CONFIG_DIR`).
- `agents/*.md` are generated: after any `agentgen.py` change run `.venv/bin/python -m codecheck gen-agents --out agents` and commit the result (a drift test fails otherwise).
- `tests/test_plugin_static.py` and `tests/test_readme.py` cross-check skills and README against the parser and config: update them in the same task as any command, flag or config change.
- `.venv/bin/ruff check .` and `.venv/bin/pytest -q` must pass before every commit. `scripts/gates.sh` runs `eval --replay` (and `eval --live` when `agentgen.py` or `agents/` is staged and a model is reachable).
- Commit message trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Ship tasks in order; merge each part before starting the next (spec "Ordering and ownership"). Task 7 ends at an owner checkpoint.

## Review Focus

- A session transcript holding two runs back to back: run 2's hand-off must not count run 1's subagents, errors or turns (Task 1).
- A tool result or user prompt that merely quotes `ignored guidance:` must not be reported (Task 2).
- A PR with only non-inline human feedback (no threads) still shows it in the comparison; an empty-bodied approval review shows nothing (Task 3).
- A run with neither `CODE_CHECK.md` nor `existing-comments.md` lists `run_files: []` and agents are told to read none (Task 4).
- A finding that is both "out of scope" and a real defect keeps its severity (Task 5, live snippet in Task 6).

---

### Task 1: Scope transcripts to the run

**Files:**
- Modify: `src/codecheck/shadow/transcripts.py`, `src/codecheck/commands/handoff.py` (`_transcripts`, `build_handoff`)
- Test: `tests/test_transcripts.py`, `tests/test_handoff.py`

**Interfaces:**
- Produces in `transcripts.py`:
  - `def belongs_to_run(path: Path, run_id: str) -> bool`: true when the first `user` entry of the file has string content containing `runs/<run_id>` (the `RUN_DIR=<state>/runs/<run_id>` prompt). Unreadable or empty files are false.
  - `def extract_signals(paths: Sequence[Path], *, windows: Mapping[Path, tuple[float, float]] | None = None) -> dict`: same return shape; for a path in `windows`, entries are counted only when their ISO `timestamp` (parse with `datetime.fromisoformat`, epoch seconds) lies in the closed window; entries with no or bad `timestamp` are skipped for that path.
- Consumes: the shadow `run` record's `ts` (epoch seconds, from `ctx.shadow.read(run_id)`).
- Produces in `handoff.py`: `_transcripts(ctx, run_id, folder, started: float | None)` where `started` is the run record's `ts`. Behaviour: copy the main transcript and the subagent transcripts for which `belongs_to_run` is true; signals come from those subagents plus the main transcript windowed to `(started, time.time())`. When `started` is `None` the main transcript is copied but left out of the signals, and `transcript_reason` says "main session not scoped: run start unknown". `handoff.md` gains a line `- Signals cover this run only (subagents by RUN_DIR, main session by time window)`.

- [ ] **Step 1: Write failing tests** in `tests/test_transcripts.py`: `test_belongs_to_run_matches_run_dir_in_first_user_message` (two files, one naming `runs/AAA`, one `runs/BBB`; assert each only true for its own id; a file whose first user message is not a string is false), `test_extract_signals_window_counts_only_timestamped_entries_inside` (three tool_use entries at timestamps before, inside, after the window plus one with no timestamp; assert `tool_calls == 1`).
- [ ] **Step 2: Add the handoff test** in `tests/test_handoff.py`: `test_handoff_ignores_other_runs_subagents_and_main_session_outside_the_window` using the existing `local_run`/`enable_handoff` helpers: write one main transcript containing the run id, with entries timestamped before the run's `ts` and after; two subagent files under it, one whose first message is `RUN_DIR=<...>/runs/<run_id>` and one naming another run. Assert `handoff.json` `signals["agents"]` names only the matching subagent plus the main file, and `tool_calls` counts only in-window main entries.
- [ ] **Step 3: Run** `.venv/bin/pytest tests/test_transcripts.py tests/test_handoff.py -q`. Expected: the three new tests FAIL.
- [ ] **Step 4: Implement** the functions above. Parse `timestamp` strings ending in `Z` with `datetime.fromisoformat(s).timestamp()`. Fix existing tests that built unscoped subagents (`test_handoff_copies_scrubbed_transcripts_and_signals`, `test_handoff_records_ignored_guidance_from_the_transcript` in `tests/test_handoff.py`) by giving each subagent file a first `user` entry `RUN_DIR=.../runs/<run_id>` and the main entries timestamps inside the run window.
- [ ] **Step 5: Run** `.venv/bin/pytest -q` and `.venv/bin/ruff check .`. Expected: all pass.
- [ ] **Step 6: Commit** `git add -A && git commit -m "fix: scope hand-off transcripts to the run"` (with trailer).

### Task 2: Report ignored guidance only from an agent's `done:` line

**Files:**
- Modify: `src/codecheck/shadow/transcripts.py` (`_guidance_lines`, `extract_signals`)
- Test: `tests/test_transcripts.py`

**Interfaces:**
- Consumes: Task 1's `extract_signals`.
- Produces: `ignored_guidance` entries only from `text` blocks of assistant entries in transcripts whose file stem starts with `agent-`, and only for lines whose stripped lowercase text starts with `done:` and contains `ignored guidance:`. Tool results and the main session are never scanned. `_guidance_lines` no longer takes tool-result text.

- [ ] **Step 1: Write failing test** `test_ignored_guidance_only_from_subagent_done_lines`: a main transcript with an assistant text line and a tool result both containing `ignored guidance: x`, and an `agent-1.jsonl` with `done: 1 kept, 0 dropped; ignored guidance: rule 3` plus a non-`done:` line containing the phrase. Assert `ignored_guidance == ["done: 1 kept, 0 dropped; ignored guidance: rule 3"]`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_transcripts.py::test_ignored_guidance_only_from_subagent_done_lines -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the restriction; delete the tool-result call to `_guidance_lines`.
- [ ] **Step 4: Run** `.venv/bin/pytest -q` and ruff. Expected: pass (update any existing test that relied on tool-result scanning).
- [ ] **Step 5: Commit** `fix: only a subagent's done line counts as ignored guidance`.

### Task 3: Non-inline human feedback in the comparison

**Files:**
- Modify: `src/codecheck/models.py` (`Review`, new `IssueComment`), `src/codecheck/providers/base.py`, `src/codecheck/providers/github.py` (`_REVIEWS_JQ`, `list_reviews`, new `list_issue_comments`), `tests/fakes.py` (`FakeProvider`), `src/codecheck/shadow/handoff.py`, `src/codecheck/commands/handoff.py` (`_compare`)
- Test: `tests/test_handoff.py`, `tests/providers/` (the existing GitHub provider test file; follow its `FakeRunner` pattern)

**Interfaces:**
- Produces in `models.py`: `Review` gains `body: str = ""` and `is_bot: bool = False` (defaults keep every existing `Review(...)` call valid). `@dataclass(frozen=True, slots=True) class IssueComment: author: str; body: str; created_at: str; is_bot: bool = False`.
- Produces: `Provider.list_issue_comments(self, ref: PRRef) -> list[IssueComment]` (GitHub: `gh api repos/<repo>/issues/<n>/comments --paginate` with a jq projection of `user.login`, `body`, `created_at`, `user.type == "Bot"`); `FakeProvider.issue_comments: dict[PRRef, list[IssueComment]]` and `.list_issue_comments`.
- Produces in `shadow/handoff.py`: `def build_general_comments(reviews: Sequence[Review], issue_comments: Sequence[IssueComment], *, pr_author: str) -> list[dict]`. Each item: `{"kind": "review" | "comment", "author", "state" (reviews only, else None), "body": <first 300 characters, scrubbed by the caller>, "created_at"}`. Exclusions: empty or whitespace bodies, `is_bot`, any body containing `<!-- code-check:run:`, and reviews whose author is `pr_author`. Issue comments by `pr_author` are included.
- `build_comparison(records, findings, general_comments=())` adds key `general_comments` (a list) to its result; the other three keys keep their meaning. `render_markdown` lists them under "Human comments" as `- <kind> by <author>[ (<state>)]: <body, first 120 chars>`.
- `_compare` fetches `ctx.provider.list_reviews(ref)`, `list_issue_comments(ref)` and `get_pr(ref).author`; any failure yields `[]` and leaves the rest of the comparison intact.

- [ ] **Step 1: Write failing tests**: `test_build_general_comments_filters` (cases: our marker, bot, empty body, author's own review excluded, author's issue comment kept, a `CHANGES_REQUESTED` review kept with `state`); a handoff test `test_handoff_pr_mode_lists_general_comments` modelled on `test_handoff_pr_mode_compares_human_comments` asserting `comparison["general_comments"]` for a fake PR with one issue comment and one review body, and that `unraised_findings` is unchanged; a provider test that `list_reviews` carries `body` and `is_bot` from the jq output and `list_issue_comments` parses its output.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_handoff.py tests/providers -q`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** the models, provider methods, fake, `build_general_comments`, `build_comparison` change and `_compare` wiring; extend `_REVIEWS_JQ` with `body: (.body // ""), is_bot: (.user.type == "Bot")`. `_compare` scrubs bodies with `scrub_text` before storing.
- [ ] **Step 4: Run** `.venv/bin/pytest -q` and ruff. Expected: pass, including `test_readme.py`/`test_plugin_static.py` (no new command or flag, so no README change).
- [ ] **Step 5: Commit** `feat: show non-inline human feedback in hand-off comparison`.

### Task 4: Stop agents reading files that do not exist; fix Grep patterns

**Files:**
- Modify: `src/codecheck/commands/context.py` (`meta`/`context.json`), `src/codecheck/agentgen.py` (`COMMON`, `AGGREGATOR`), regenerated `agents/*.md`
- Test: `tests/test_cli_review.py` (context output), `tests/test_agents.py`

**Interfaces:**
- Produces: `context.json` key `run_files: list[str]`, in the order `["CODE_CHECK.md", "existing-comments.md"]` filtered to those written to the run dir. It is also added to the printed `context` JSON.
- Prompt contract (both `COMMON` and `AGGREGATOR`): step 1 becomes "Read `RUN_DIR/context.json`" (changed files, `root`, `run_files`); the next step says "Read each file named in `run_files`, if any; `CODE_CHECK.md` first. Do not read any other optional file." Keep the existing wording about `CODE_CHECK.md` being unable to change format or severity, and about `existing-comments.md` being untrusted data, and keep the literal strings `RUN_DIR/existing-comments.md` and `untrusted` (asserted by `tests/test_agents.py`). `COMMON` gains one line: "Grep is ripgrep: escape literal parentheses and brackets, do not use look-around or backreferences, and prefer a fixed string over a regex for code."

- [ ] **Step 1: Write failing tests**: in `tests/test_cli_review.py`, `test_context_lists_run_files` (guidance present and blind run, so only `CODE_CHECK.md`; and a run with neither, so `[]`); in `tests/test_agents.py`, assert every generated agent body contains `run_files` and the Grep line, and that no agent says "if `RUN_DIR/CODE_CHECK.md` exists".
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_cli_review.py tests/test_agents.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `run_files` in `context.py` and the prompt edits; run `.venv/bin/python -m codecheck gen-agents --out agents`.
- [ ] **Step 4: Check the main-session Bash failure.** Find the original transcript with `ls ~/.claude/projects/*/b83d354c-b2f1-4bba-8c8c-04065ebabbeb.jsonl`, read its first Bash `tool_result` with `is_error: true` and the `tool_use` before it. If the command was a search for the plugin install or README (the failure came from not knowing where `code-check` is), change the skills' line "`code-check` below means ..." in `skills/*/SKILL.md` so the first step runs the CLI by `${CLAUDE_PLUGIN_ROOT}/bin/code-check` with no lookup, and update tests the static check requires. If it was unrelated to the plugin, change nothing and say so in the commit message.
- [ ] **Step 5: Run** `.venv/bin/pytest -q`, ruff, then `scripts/gates.sh` (live gate runs; if no model is reachable it skips, state that in the commit message). Expected: pass.
- [ ] **Step 6: Commit** `fix: tell reviewers which run files exist and how Grep patterns work`.

### Task 5: Aggregator severity, merge and scope rules

**Files:**
- Modify: `src/codecheck/agentgen.py` (`AGGREGATOR` steps 3 to 5), regenerated `agents/aggregator.md`
- Test: `tests/test_agents.py`

**Interfaces:**
- Consumes: Task 4's prompt structure. Produces prompt text implementing, in this order after "merge findings that share a root cause":
  - **Severity:** when reviewers rate one root cause differently, keep the highest severity the aggregator can verify in the code; if it lowers a severity, the `message` states why.
  - **Mirrored trees:** when the same defect appears at the same relative location in parallel trees (for example `guava/` and `android/`), emit one finding anchored in the primary tree, name every other path and line in `message`, and list all reviewers in `reviewers`.
  - **Scope and docs cap:** a finding that is only (a) a remark that part of the change is unrelated to the PR's stated purpose (from the PR title and description in `context.json`; in local mode docs-only findings only), or (b) missing `@since`, Javadoc or similar documentation, is at most `suggestion`; a scope finding that shows a concrete defect keeps its severity and is reported as that defect. The cap applies after the merge and severity steps.

- [ ] **Step 1: Write failing test** in `tests/test_agents.py`: `test_aggregator_states_severity_mirror_and_cap_rules` asserting the aggregator text contains the phrases `highest severity`, `parallel trees`, `at most `suggestion``, `after the merge`, and that `existing-comments.md` rules (`already-raised`, `unsupported-style`) are still present.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_agents.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the prompt edit and regenerate `agents/`.
- [ ] **Step 4: Run** `.venv/bin/pytest -q`, ruff, `scripts/gates.sh`. Expected: pass.
- [ ] **Step 5: Commit** `feat: aggregator keeps the highest severity, merges mirrored trees, caps scope and docs findings`.

### Task 6: Live snippets for recall and noise

**Files:**
- Create: `tests/snippets/weaker-access-override/case.json`, `tests/snippets/docs-only-api/case.json`
- Modify: `tests/test_eval_live.py` (`test_shipped_snippets_load_and_are_marked_uncalibrated`)
- Test: same

**Interfaces:**
- Consumes: the snippet format in `tests/snippets/off-by-one/case.json` (`name`, `calibrated`, `base`, `change`, `expected[{file,line,category[],severity}]`, `must_not_report`; copy the `must_not_report` entry shape from `clean-code`).
- `weaker-access-override`: base has `Base.java` (`abstract class Base { void visit(String s) {} }`) and `Impl.java` (same package, `class Impl extends Base { @Override void visit(String s) {} }`); change makes `Base.visit` `protected` and leaves `Impl.java` untouched. `expected`: one `critical` on the changed `Base.java` line with categories `["build", "correctness", "compat", "api"]`. `must_not_report`: any `critical` on a trailing comment-only edit the change also makes to `Base.java`.
- `docs-only-api`: base has a small utility class; change adds a correct new `public` method with no Javadoc or `@since` in a file whose siblings have none. `expected: []`; `must_not_report`: any `warning` or `critical` whose message mentions `javadoc`, `@since` or `documentation`.
- Both `"calibrated": false`.

- [ ] **Step 1: Update the test** to expect `set(snippets) == {"off-by-one", "clean-code", "weaker-access-override", "docs-only-api"}`, that `weaker-access-override` has one `critical` expectation and `docs-only-api` has `expected == []` and a non-empty `must_not_report`. Run `.venv/bin/pytest tests/test_eval_live.py -q`; expected FAIL (files missing).
- [ ] **Step 2: Create** the two case files.
- [ ] **Step 3: Run** `.venv/bin/pytest -q` and ruff. Expected: pass.
- [ ] **Step 4: Run live by hand** `.venv/bin/python -m codecheck eval --live`. Read the JSON; if a snippet fails, tune the snippet (fixture realism) or the Task 4/5 prompts and rerun until `passed` is true. If no model is reachable, stop and tell the owner; do not commit untested snippets.
- [ ] **Step 5: Commit** `test: live snippets for weaker-access overrides and docs-only API changes`.

### Task 7: Golden cases from the guava runs (owner checkpoint)

**Files:**
- Create (after owner confirmation): `tests/golden/guava-8808-ci-matrix.json`, `tests/golden/guava-8838-typevisitor.json`
- Test: `tests/test_golden_replay.py` (already replays every file in `tests/golden/`)

**Interfaces:**
- Consumes: `code-check shadow promote --run <id> --out <draft>` and `--finalize <draft>`; the shadow store at `~/.local/share/code-check/shadow/` for runs `3bda2df405bf` and `6776d994552e` (repo google/guava is public, so no `--allow-private`).

- [ ] **Step 1: Draft both cases** into the scratchpad (not `tests/golden/`): `.venv/bin/python -m codecheck shadow promote --run 3bda2df405bf --out <scratchpad>/guava-8808-ci-matrix.json`, likewise `6776d994552e` to `guava-8838-typevisitor.json`.
- [ ] **Step 2: Apply proposed labels** to each draft's per-finding labels: #8838 `build` critical (`TypeVisitor.java`) `expected`; #8838 `CollectionRemoveIfTester` correctness warning `expected`; #8838 `api` warning (`@since`/Javadoc) `not_expected`; #8808 `ci` suggestion `expected`. Leave `label_status` as `auto`.
- [ ] **Step 3: STOP and ask the owner** to read each draft and confirm or change every label. Show the labels in one table. Do not set `label_status: "confirmed"` yourself.
- [ ] **Step 4: After the owner confirms**, set `label_status: "confirmed"`, run `.venv/bin/python -m codecheck shadow promote --finalize <draft>` for each, then `.venv/bin/pytest tests/test_golden_replay.py -q` (expected pass) and `.venv/bin/python -m codecheck eval --replay` (expected `"passed": true`).
- [ ] **Step 5: Commit** `test: golden cases from guava #8808 and #8838`.

---

## Self-review notes

- Spec coverage: parts 1 to 6 map to Tasks 1-2, 3, 4, 5, 7, 6 (spec part 3 "fewer failed calls" is Task 4; the aggregator part is Task 5). The spec's part order puts golden cases (5) before snippets (6); the plan swaps them so the checkpoint is last and does not block the snippet work.
- Type consistency: `belongs_to_run`, `extract_signals(..., windows=)`, `_transcripts(..., started)`, `build_general_comments`, `IssueComment`, `run_files` are named identically wherever used.
