# Tokio round: build identity, unposted comparison, race coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make hand-offs identify the plugin build and compare unposted reviews with human threads, add a live Rust shutdown-race snippet and golden cases from the tokio runs, and tune a prompt only if those tests show the panel misses the race.

**Architecture:** Hand-off changes live in `shadow/handoff.py` (pure rendering and comparison) and `commands/handoff.py` (I/O), with a new small `shadow/buildid.py` for the plugin hash. Prompt text lives in `agentgen.py` and `agents/*.md` is regenerated. The snippet is a data folder under `tests/snippets/`; golden cases come from `shadow promote` and need the user's confirmation. The plan has two phases with a hard stop between them for results only the user can produce.

**Tech Stack:** Python 3.14, pytest, ruff, the existing `FakeProvider`/`FakeRunner` test fakes, Node installer tests (unchanged), `claude -p` live eval (user-run).

**Spec:** `docs/superpowers/specs/2026-11-13-tokio-round-handoff-and-race-coverage-design.md`

## Global Constraints

- Work in the worktree `/Users/subhiandrews/opensource/codecheck/.worktrees/tokio-round` (branch `feat/tokio-round`), with its own venv: `python3.14 -m venv .venv && .venv/bin/pip install -e '.[dev]'` (the shared venv's editable install points at the main checkout).
- Every CLI subcommand prints exactly one JSON document; shadow side effects only through `ctx.guarded(...)`; hand-off must never fail a review.
- Nothing from a PR, config or reviewer output reaches git/gh/claude argv as an option.
- `agents/*.md` are generated: rerun `.venv/bin/python -m codecheck gen-agents --out agents` after any prompt change; the drift test must pass.
- README and skills stay consistent with the CLI and config (`tests/test_readme.py`, `tests/test_plugin_static.py`).
- Any command containing `eval` is blocked in this worktree session. The user runs `eval --live` from a separate shell and sends result files (e.g. `/tmp/live-1.json`, `/tmp/live-2.json`). The commit gate runs the live eval for gated paths (`agentgen.py`, `agents/`, `commands/{context,handoff,ingest,post}.py`, `src/codecheck/evals/`), takes 15+ minutes and can flake: retry once; never `--no-verify`.
- `tests/golden/` labels must be confirmed by the user: draft with `shadow promote`, show a table of findings and labels, finalize only after confirmation. Tokio is public; `promote` refuses private or unknown repos.
- `main` needs an approving review the user cannot give: ask before `gh pr merge --squash --admin`. Squash merge; afterwards check the tree against `main` and delete the branch with `-D`.
- `.venv/bin/ruff check .` and `.venv/bin/pytest -q` must pass before every commit. Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Unposted comparison when the PR cannot be fetched (offline, gh failure): the hand-off is still written, `comparison` is null with a reason, no exception.
2. An unposted run whose aggregator produced zero findings: every human thread is listed as a miss, `unraised_findings` is empty, no crash.
3. `CLAUDE_PLUGIN_ROOT` set to a missing directory, or a tree where `agents/` holds a symlink: `prompts_hash` is null with a `build_reason` (or ignores the symlink), never an exception.
4. A merged or closed PR, and a PR whose head moved after the review: the comparison still runs and is labelled (`pr_state`, `head_moved`), not skipped.
5. The race snippet's `expected` line number drifting from the real pushing line when the file is edited: a test pins the line to the text it must point at.

---

## Phase A: no model results needed

### Task 1: Build identity in the hand-off

**Files:**
- Create: `src/codecheck/shadow/buildid.py`
- Modify: `src/codecheck/commands/handoff.py` (add three keys to the `handoff` dict), `src/codecheck/shadow/handoff.py` (`render_markdown`, module docstring field list)
- Test: `tests/test_buildid.py` (new), `tests/test_handoff.py`

**Interfaces:**
- Produces: `plugin_build(env: Mapping[str, str], *, default_root: Path | None = <repo root two levels above src/>) -> dict` returning `{"plugin_root": str | None, "prompts_hash": str | None, "build_reason": str | None}`. `handoff.json` gains those three keys at the top level (schema version stays 1).

- [ ] **Step 1: Write failing tests in `tests/test_buildid.py`**
  - `test_prompts_hash_changes_when_an_agent_file_changes`: build a tmp tree with `agents/a.md` and `skills/s/SKILL.md`; hash it, edit `agents/a.md`, assert the hash differs and has 12 hex chars.
  - `test_prompts_hash_ignores_other_files_and_symlinks`: adding `README.md` or a symlink inside `agents/` leaves the hash unchanged.
  - `test_plugin_build_uses_claude_plugin_root_first`: `env={"CLAUDE_PLUGIN_ROOT": str(tree)}` gives `plugin_root == str(tree.resolve())` and a non-null hash.
  - `test_plugin_build_without_a_usable_root_reports_a_reason`: empty env with `default_root=None`, and `CLAUDE_PLUGIN_ROOT` pointing at a missing directory, both return null root/hash and a non-empty `build_reason`; neither raises.
- [ ] **Step 2: Run `.venv/bin/pytest tests/test_buildid.py -q`; expect import failure.**
- [ ] **Step 3: Implement `plugin_build` in `src/codecheck/shadow/buildid.py`.** Root is `CLAUDE_PLUGIN_ROOT` if set, else `default_root` only when it contains `agents/` and `.claude-plugin/plugin.json` (an installed venv's `Path(__file__).parents[3]` will not, which yields the null case). Hash is sha256 over each regular, non-symlink file of `agents/*.md` and `skills/*/SKILL.md` in sorted relative-path order, feeding `relpath`, a NUL, the bytes, a NUL; first 12 hex chars.
- [ ] **Step 4: Write failing tests in `tests/test_handoff.py`:** `test_handoff_records_plugin_build` (set `CLAUDE_PLUGIN_ROOT` on the test context's env the way the file sets `CLAUDE_CONFIG_DIR`; assert `handoff.json` has the three keys and the hash matches `plugin_build`), and extend `test_render_markdown_mentions_the_essentials` so a hand-off with `prompts_hash` renders a `- Build: <hash> (<root>)` line (and `- Build: unknown (<reason>)` when null).
- [ ] **Step 5: Implement.** Add the three keys in `build_handoff` (`**plugin_build(ctx.env)`), the Build line in `render_markdown` right after the Head line, and list the fields in the `shadow/handoff.py` docstring (read `test_handoff_schema_note_lists_present_and_absent_fields` first; it constrains that docstring).
- [ ] **Step 6: Run `.venv/bin/pytest tests/test_buildid.py tests/test_handoff.py -q` and `.venv/bin/ruff check .`; expect pass.**
- [ ] **Step 7: Commit** `feat: record the plugin build (root and prompts hash) in hand-offs`.

### Task 2: Comparison for unposted reviews

**Files:**
- Modify: `src/codecheck/commands/handoff.py` (`build_handoff`, `_compare`), `src/codecheck/shadow/handoff.py` (`render_markdown`, docstring), `README.md` (hand-off section), `tests/test_handoff.py`
- Test: `tests/test_handoff.py`

**Interfaces:**
- Consumes: `PullRequest` fields `head_sha`, `is_draft`, `state` from `ctx.provider.get_pr(ref)`.
- Produces: `handoff.json` keys `comparison_mode` (`"posted"`, `"unposted"` or null), `head_moved` (bool or null), `pr_state` (`"open"|"closed"|"merged"` or null). `_compare(ctx, ref, run_id, *, findings: list[dict] | None = None)`: when `findings` is given, those hand-off findings (id, file, line, severity, category) replace the shadow `finding` records as the "ours" side, because an unposted run has no such records.

- [ ] **Step 1: Write failing tests.**
  - `test_handoff_compares_an_unposted_review` (new helper that runs context, review and ingest but never `post`): `comparison_mode == "unposted"`, `head_moved is False`, `pr_state == "open"`, a human thread within 3 lines matches a hand-off finding, no review was posted (assert the provider recorded no posts; read `tests/fakes.py` for the attribute).
  - `test_handoff_unposted_with_no_findings_lists_every_thread_as_a_miss`.
  - `test_handoff_unposted_comparison_failure_keeps_the_files`: provider `list_threads` raising yields `comparison is None`, a reason starting `comparison failed`, files written.
  - Rewrite `test_handoff_skips_comparison_when_the_head_moved`: after a stale `post`, the comparison now runs with `comparison_mode == "unposted"` and `head_moved is True`.
  - `test_handoff_unposted_records_closed_pr_state`: a merged PR gives `pr_state == "merged"` and still compares.
  - Keep `test_handoff_skips_comparison_when_not_posted` (draft) passing unchanged: a draft PR is skipped with the existing "not posted" reason and zero thread calls.
  - Extend the markdown test: an unposted comparison renders the heading line `## Human comments (unposted review; compared with the threads as they are now)` and, when `head_moved`, a `- Head moved since the review` line.
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement.** In `build_handoff`, fetch the PR once (reuse it for `pr_url`). Posted (`decision` present): unchanged path, `comparison_mode="posted"`. No decision and PR is a draft: keep today's reason, no provider thread calls. No decision otherwise: `_compare(..., findings=<the hand-off findings>)`, `comparison_mode="unposted"`, `head_moved = pr.head_sha != meta["head_sha"]`, `pr_state = pr.state`. A failed PR fetch yields `comparison=None` with a reason. Local mode keeps its reason with all three keys null.
- [ ] **Step 4: Update `README.md` hand-off section** (the paragraph describing `comparison` and "A review that was not posted ... gets no comparison"): unposted PR reviews now get a comparison marked unposted; drafts and local reviews still do not. Update the `shadow/handoff.py` docstring field list. Run `.venv/bin/pytest tests/test_readme.py tests/test_handoff.py -q`.
- [ ] **Step 5: Run full `.venv/bin/pytest -q` and ruff; expect pass.**
- [ ] **Step 6: Commit** `feat: compare unposted PR reviews with the human threads`. (Touches `commands/handoff.py`: the live gate runs; retry once on flake.)

### Task 3: Prompt cleanups (aggregator Grep hint, missing `run_files`)

**Files:**
- Modify: `src/codecheck/agentgen.py` (aggregator steps near lines 98-110, reviewer step 2 near line 31), regenerated `agents/*.md`
- Test: `tests/test_agentgen.py` (find the existing prompt assertions; add to it)

**Interfaces:**
- Produces: aggregator prompt where the ripgrep hint ("Grep is ripgrep: escape literal parentheses and brackets, do not use look-around or backreferences, and prefer a fixed string over a regex for code.") sits in the verification step (today step 7), not the "do not invent findings" step 9; reviewer and aggregator prompts tell the agent that when `context.json` has no `run_files` key it must look for `RUN_DIR/CODE_CHECK.md` and `RUN_DIR/existing-comments.md` itself.

- [ ] **Step 1: Write failing tests:** the aggregator text contains the Grep hint inside the verification step and not in the "Do not invent findings" step; every reviewer prompt and the aggregator prompt mention falling back to `RUN_DIR/CODE_CHECK.md` when `run_files` is absent.
- [ ] **Step 2: Run; expect failure.**
- [ ] **Step 3: Edit `agentgen.py`** (move the hint; add the fallback sentence to the `run_files` instruction in both prompts), then `.venv/bin/python -m codecheck gen-agents --out agents`.
- [ ] **Step 4: Run `.venv/bin/pytest -q` (drift test included) and ruff; expect pass.**
- [ ] **Step 5: Commit** `fix: aggregator Grep hint in the verification step; fall back when run_files is absent`. (Gated path: the live gate runs, 15+ minutes; retry once.)

### Task 4: Hand-off cleanups (assertion, swallowed error reason)

**Files:**
- Modify: `src/codecheck/commands/handoff.py` (`_general_comments`, `build_handoff`), `src/codecheck/shadow/handoff.py` (docstring), `tests/test_handoff.py`

**Interfaces:**
- Produces: `_general_comments(ctx, ref) -> tuple[list[dict], str | None]` (comments, reason), and a `general_comments_reason` key in `handoff.json` (null when fine, a scrubbed one-line reason such as `list_reviews failed: <ExceptionType>` otherwise).

- [ ] **Step 1: Write the failing test** `test_handoff_records_why_general_comments_were_skipped`: a provider whose `list_reviews` raises gives `general_comments == []` and a non-null `general_comments_reason` naming the exception type; the success path gives null.
- [ ] **Step 2: Simplify `test_render_markdown_collapses_general_comment_newlines`:** replace the final convoluted `split(...)` assertion with a direct check that the Human comments section is one block, for example `assert md.count("\n## Session\n") == 1` (the heading appears only where `render_markdown` emits it).
- [ ] **Step 3: Implement the return type change and key; update callers and the docstring field list.**
- [ ] **Step 4: Run `.venv/bin/pytest tests/test_handoff.py -q`, full suite, ruff; expect pass.**
- [ ] **Step 5: Commit** `fix: record why general comments were skipped; simplify a render test`.

### Task 5: The `rust-shutdown-race` live snippet

**Files:**
- Create: `tests/snippets/rust-shutdown-race/case.json`
- Modify: `tests/test_eval_live.py` (`test_shipped_snippets_load_and_are_marked_calibrated`)
- Test: `tests/test_eval_live.py`

**Interfaces:**
- Consumes: the snippet format in README "Live snippet eval" (`name`, `base`, `change`, `expected`, `must_not_report`, `calibrated`).
- Produces: a snippet named `rust-shutdown-race`, `"calibrated": false`.

- [ ] **Step 1: Update the test first.** The shipped set becomes the four existing names plus `rust-shutdown-race`; the four existing stay `calibrated is True`, the new one `False`; assert its `expected` has one `warning` entry; assert the line `expected[0]["line"]` of `change["src/registry.rs"]` (1-based) contains `pending_release.push`; assert `must_not_report` forbids a `critical` anywhere in the file.
- [ ] **Step 2: Run; expect failure (snippet missing).**
- [ ] **Step 3: Write `case.json`.** One small valid Rust file `src/registry.rs`: a `Registry` with a lock-guarded `pending_release: Vec<Handle>` and a `shut_down: bool`, a `shutdown()` that sets the flag and drains `pending_release`, and `deregister()`. `base` has `deregister` check `shut_down` before pushing; `change` removes that check (the only edit), so a late `deregister` re-queues a handle after shutdown drained the list and it is never released. One defect, no distractor, under 60 lines. `expected`: file `src/registry.rs`, line = the `pending_release.push` line, severity `warning`, category list `["race", "concurrency", "leak", "lifecycle", "shutdown", "correctness", "logic"]`. `must_not_report`: `{"file": "src/registry.rs", "severity": ["critical"]}`.
- [ ] **Step 4: Run `.venv/bin/pytest tests/test_eval_live.py -q` and ruff; expect pass.**
- [ ] **Step 5: Commit** `test: add the rust-shutdown-race live snippet (uncalibrated)` in its own commit, after Tasks 1-4.

### Task 6: Draft golden cases from the baseline runs, then stop

**Files:**
- Create (not committed): drafts under the session scratchpad, e.g. `<scratchpad>/golden-drafts/<run_id>.json`
- Modify: `docs/superpowers/specs/2026-11-13-tokio-round-handoff-and-race-coverage-design.md` (Follow-up section)

- [ ] **Step 1: Draft** `.venv/bin/python -m codecheck shadow promote --run <id> --out <draft>` for `98596b385d5b`, `2c3b00d46689`, `8c75582f3970` (shadow store `~/.local/share/code-check/shadow/`). Each draft must `check_case` cleanly; report any that do not.
- [ ] **Step 2: Probe promote on the other two shapes** and record the answers: the unposted run `84048732c6dd` (does `draft_case` accept it, and does the draft replay given `expected_posted` is empty?) and a local run (case id uses `local`). Do not change `promote.py`; only record behaviour.
- [ ] **Step 3: Build a table** per run: each finding (id, file:line, severity, category) with its auto label (`expected`, `not_expected` or unlabelled) and each human comment as a miss, using the thread text from `gh api repos/tokio-rs/tokio/pulls/<n>/comments`.
- [ ] **Step 4: Update the spec's Follow-up section** with the probe answers and commit it (`docs: record promote probe results`).
- [ ] **Step 5: HARD STOP.** Run the full suite and ruff, push the branch (do not open the PR yet), and give the user: the tables to confirm, and the three things only they can do:
  1. Run `eval --live --only rust-shutdown-race` twice from a separate shell and send `/tmp/live-1.json` and `/tmp/live-2.json`.
  2. Check out tokio at `53de1f6b` on a branch against `master` and run `review-local` with the 0.1.3 plugin and hand-off on; send the run id.
  3. Confirm or correct the label tables.

## Phase B: after the user's results arrive

### Task 7: Draft the real-run golden case for #8735 at `53de1f6b`

**Files:**
- Create (not committed): draft under the scratchpad

**Interfaces:**
- Consumes: the user's run id for the local `review-local` run on tokio at `53de1f6b`.

- [ ] **Step 1: Confirm the hand-off:** `tool_version` 0.1.3, `mode: "local"`, `head_sha` starting `53de1f6b`, panel includes `lang-rust`. If not, tell the user and stop.
- [ ] **Step 2: Draft** with `shadow promote --run <id> --out <draft>`. Local runs carry no human threads, so author the race label by hand in the draft: the thread at `tokio/src/runtime/io/driver.rs:360` ("a possible race between deregistration and shutdown", fixed upstream in `f3dda518`) is a label `expected` for a finding at that location when the panel raised one, otherwise an entry in `misses`. Record whether any reviewer or the aggregator raised it (check `raw.json` reviewers and `dropped`).
- [ ] **Step 3: Show the user the table** (findings, labels, the race row, the verdict) and wait for confirmation.

### Task 8: Decide on a prompt change from the results

**Files:**
- Modify (only if the rule below says so): `src/codecheck/agentgen.py`, regenerated `agents/*.md`
- Test: `tests/test_agentgen.py`

- [ ] **Step 1: Read `/tmp/live-1.json` and `/tmp/live-2.json`** for the `rust-shutdown-race` case (`matched`, `recall`, `unexpected_critical`, `forbidden_hits`) and the Task 7 verdict. Write the numbers into the spec's Follow-up section.
- [ ] **Step 2: Apply the decision rule.** If the snippet matched in at least one run and the real-run case raised the race: make no prompt change, skip to Task 9. If the snippet failed both runs, or the real-run case missed the race: continue.
- [ ] **Step 3 (conditional): Write a failing test** that the prompts of the reviewers owning Rust code (lang-rust, skeptic, architect) contain a short shutdown/lock/lifecycle checklist: what a call that arrives after shutdown can re-queue or leak, and what a lock guard protects across an early return or `.await`. One paragraph, no new rules about severity.
- [ ] **Step 4 (conditional): Implement in `agentgen.py`, regenerate `agents/`, run the suite.**
- [ ] **Step 5 (conditional): Commit** `feat: shutdown and lifecycle checklist for Rust reviewers`, then ask the user to rerun `eval --live` (all snippets) twice. Pass means: `rust-shutdown-race` matches in at least one run, every other snippet still passes, no clean snippet gains a warning or critical. If it fails after two attempts, stop, revert the prompt change, and report; do not widen the prompt further.

### Task 9: Finalize golden cases, calibrate, release

**Files:**
- Create: `tests/golden/<repo>-<pr>-<sha8>.json` files via `shadow promote --finalize`
- Modify: `tests/snippets/rust-shutdown-race/case.json` (`calibrated`), `tests/test_eval_live.py`, `VERSION`, `pyproject.toml`, `package.json`, `package-lock.json` (two entries), `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `src/codecheck/__init__.py`, `tests/test_smoke.py`, the spec's Follow-up section

- [ ] **Step 1: Finalize** each draft the user confirmed with `shadow promote --finalize <draft>`; each must replay exactly. `eval --replay` is blocked in this session, so run `.venv/bin/pytest tests/test_golden_replay.py -q`; the user confirms `eval --replay` passes from their shell.
- [ ] **Step 2: Set `calibrated: true`** on `rust-shutdown-race` only if it passed in the user's runs; otherwise leave it false and say so. Update `test_shipped_snippets_load_and_are_marked_calibrated` to match.
- [ ] **Step 3: Bump the version** `0.1.3` to `0.1.4` in the eight files listed above (`package-lock.json` has two entries) (the plugin payload changed: hand-off format, prompts).
- [ ] **Step 4: Run `.venv/bin/pytest -q`, `node --test tests-node/*.test.js`, `.venv/bin/ruff check .`; expect all pass.** Fill in the spec's Follow-up section and commit.
- [ ] **Step 5: Open the PR** (squash-merge title style `... (#N)`), with the results in the body; ask the user before any `--admin` merge. After merging, remind the user to run `claude plugin update code-check@code-check`, restart, and confirm the next hand-off shows `tool_version` 0.1.4 and a `prompts_hash`.
