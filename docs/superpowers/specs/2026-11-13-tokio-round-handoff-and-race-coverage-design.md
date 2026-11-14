# Tokio round: build identity, unposted comparison, race coverage (design)

Source: four Rust hand-offs on tokio-rs/tokio. Three were produced by a stale 0.1.2 plugin cache (PR #9 content): `98596b385d5b` (#8779), `2c3b00d46689` (#8757), `8c75582f3970` (#8735, posted). One was produced by 0.1.3: `84048732c6dd` (#8735 at head `e7aa7747`, deliberately not posted because it is a real PR). Authority order is unchanged (spec, spike, plan). Size: a project.

## Intent

The user's priorities are, in order: do not miss real bugs, then keep noise down. These runs showed the headline bugs were caught (the `after_push` panic-safety warning on #8779, the swallowed io_uring setup errors on #8757). The one real bug no reviewer raised was a race on #8735: `deregister_source` returning after `RegistrationSet::shutdown` had run could re-queue a release (`io/driver.rs:360`, human thread, author confirmed, fixed in `f3dda518`). The rest of the "missed by us" threads were design direction, API shape and test strictness.

The work is evidence-driven. Fix the tooling gaps the runs exposed, add a test that shows whether the panel catches that class of bug, and change a prompt only if the test fails.

## Findings from the runs (verified)

- A stale cache was invisible. `tool_version` was `0.1.2` for both the old and the new build, and `config_hash` was the same. The only evidence was the plugin path inside the transcripts.
- A review that is not posted gets no comparison, although the human threads exist and could be read without writing anything (`commands/handoff.py`: `decision is None` skips `_compare`). Testing on a real PR without cluttering it therefore yields no comparison.
- Golden cases (`tests/golden/`) replay recorded reviewer and aggregator output through the deterministic pipeline. They never call a model. Only live snippets (`tests/snippets/`) exercise the model. So "does the panel catch this class of bug" can only be tested by a live snippet, or by a real review run whose output is then recorded.
- On the 0.1.3 run, lang-rust (haiku) raised two race warnings about the new tests; the aggregator dropped both as `not-verified` and downgraded a third warning to a suggestion. Not enough evidence to act on; recorded for the next round.

## Goals

1. Every hand-off identifies the plugin build that produced it, so a stale cache is detectable from `handoff.json` alone.
2. A PR-mode review that was not posted still gets a comparison with the human threads, marked as unposted.
3. A live Rust snippet reproduces the shutdown-race class of bug (a lock-protected release set that a late deregistration can repopulate after shutdown), expected as a warning.
4. A real-run golden case for #8735 at `53de1f6b` (the commit before `f3dda518`, where the humans commented) records whether the full panel catches the real race.
5. Tune a prompt only if goal 3 or 4 fails (see Decision rule).
6. The three baseline runs become golden cases (labels confirmed by the user).
7. The #11 Minors are cleared (see Cleanups).

## Non-goals and anti-goals

- No new reviewer, no change to panel membership, no model change (lang-rust stays on its configured model).
- No semantic matching of human comments to findings (different lines, same topic). Evidence is one weak example; the line-window rule (same file, line within 3) stays.
- No attempt to catch maintainer design-direction comments (API breakage, where setup should live).
- Anti-goal: a prompt change that makes clean snippets report warnings or criticals. All existing snippets must still pass.
- Anti-goal: the comparison step for an unposted review writing anything to GitHub, or failing the review. It reads threads only and never fails a run.
- Anti-goal: private repositories in the golden set (`promote` refuses private or unknown repos; tokio is public).

## Constraints

- `CLAUDE.md` invariants apply: one JSON document per subcommand, shadow side effects only through `ctx.guarded`, nothing from a PR reaching argv as an option, `gen-agents` rerun after prompt changes, README and skills updated with any command, flag or config change.
- Golden labels in `tests/golden/` must be confirmed by the user. Draft with `shadow promote`, show a table of findings and labels, finalize only after confirmation.
- `eval` commands are blocked in a worktree session. The user runs `eval --live` from a separate shell and sends result files (for example `/tmp/live-1.json`, `/tmp/live-2.json`).
- The commit gate runs the live eval for gated paths and can take 15+ minutes and flake; retry once, never `--no-verify`.
- `main` needs an approving review the user cannot give on their own PR. Bypassing with `gh pr merge --squash --admin` needs the user's explicit go-ahead each time.
- Squash merges only; after one, check the tree against `main` and delete the branch with `-D`.

## Approaches considered

1. Measure first, no prompt changes: build identity, unposted comparison, golden cases and snippets only. Lowest risk; does not improve recall by itself.
2. Measure first, then tune only where a test fails (chosen by the user). Same as 1, plus a prompt step gated on the new tests.
3. A dedicated concurrency reviewer. Rejected: new cost and noise on every run, justified by one miss.

## Design

### Build identity (goal 1)

`handoff.json` gains `plugin_root` (the plugin directory the session ran from, from `CLAUDE_PLUGIN_ROOT` when set, otherwise null) and `prompts_hash` (sha256, truncated like `config_hash`, over the sorted contents of `agents/*.md` and `skills/**/SKILL.md` under that root, null with a reason when no root is known). `handoff.md` prints both on the Run line. Schema version stays 1 because the fields are additive; readers treat them as optional. The builder decides how the root is resolved if `CLAUDE_PLUGIN_ROOT` is not available at CLI time (the spike did not confirm hook-time environment variables); it must not fail the hand-off.

### Unposted comparison (goal 2)

In PR mode, when no `decision` record exists, run the same comparison against the PR's current threads and the aggregator findings, and set `comparison_mode` to `"unposted"` (the existing case is `"posted"`, local reviews keep their reason). The read-only paths used today (`_compare`, `collect_outcomes`, general comments) are reused; nothing is posted. A draft PR or a fetch error still produces `comparison: null` with a reason, never an exception. Our own earlier posted comments are excluded the same way as today (the `<!-- code-check:run: -->` marker and bot or author filtering). `render_markdown` labels the section "Human comments (unposted review; compared with the threads as they are now)". README hand-off section and the `handoff` docstring are updated.

### Race snippet (goal 3)

A new live snippet `rust-shutdown-race/` in `tests/snippets/`: a small Rust module with a `Mutex`/`RwLock`-guarded registry holding a `pending_release` list and a `shut_down` flag. The change makes `deregister` push onto `pending_release` without checking `shut_down`, after `shutdown` has already drained the list, so entries leak and are never released. `expected`: one finding at the pushing line (within 2), severity `warning`, categories `race`, `concurrency`, `leak`, `lifecycle`, `shutdown`, `correctness`. `must_not_report`: criticals on unrelated lines. It is marked `calibrated` only after the user has run it twice against a real model.

### Real-run golden case (goal 4)

The user checks out tokio at `53de1f6b` on a branch against `master` and runs `review-local` (not `review-pr`, which posts), with the 0.1.3 plugin and hand-off on. The hand-off for that run is promoted with `shadow promote`: the `io/driver.rs:360` race is labelled `expected` and, if the panel did not raise it, goes into `misses`. A run that misses it is a result, not a failure of the task.

### Baseline golden cases (goal 6)

Promote `98596b385d5b`, `2c3b00d46689` and `8c75582f3970`. They carry pre-#11 reviewer output; replay is deterministic so that is fine, and each case records its `tool_version`. `84048732c6dd` is not promoted unless `shadow promote` accepts an unposted run (the builder checks; the answer is recorded either way). Labels come from the human threads and are confirmed by the user in a table before finalizing.

### Decision rule for prompts (goal 5)

After goals 3 and 4, the user runs `eval --live` twice and sends the results.

- If the race snippet matches in at least one run and the real-run case caught the race: no prompt change. Record the result in this document's follow-up section.
- If the snippet fails both runs, or the real-run case missed the race: add a short shutdown, lock and lifecycle checklist (what a late call after shutdown can re-queue, what a lock guard protects across an `.await` or an early return) to the `agentgen.py` prompts of the reviewers that own Rust code, regenerate `agents/`, and rerun every snippet twice. All snippets must still pass and no clean snippet may gain a warning or critical. If the change does not fix the snippet after two attempts, stop and report; do not widen the prompt further.

### Cleanups from #11 (goal 7)

1. Move the Grep hint in the aggregator prompt (`agentgen.py`) from step 9 to step 7 (the verification step); regenerate `agents/aggregator.md`.
2. Simplify the assertion in `test_render_markdown_collapses_general_comment_newlines`.
3. A `context.json` without `run_files` must not silently drop guidance: fall back to checking for `CODE_CHECK.md` in the run directory.
4. `_general_comments` records a reason when it swallows an error (`general_comments_reason` in the hand-off, `null` otherwise).
5. Watch item, no code: report if `weaker-access-override` ever reports a critical at `Impl.java:5`.

### Plan shape (builder-check answers)

- One branch and one PR, in two phases with a hard stop between them. Phase A (build identity, unposted comparison, cleanups, race snippet with `calibrated: false`, baseline promotion drafts) needs no model results. Phase B waits for the user's `eval --live` results and the real-run hand-off, then does the conditional prompt tuning, finalizes golden labels and bumps the version. The bump comes last, after any change to `agents/`.
- `shadow promote` already builds a case without a PR number (the case id uses `local`), so a `review-local` run at `53de1f6b` is promotable. It has no human threads, so the `io/driver.rs:360` label is hand-authored from the #8735 thread (the builder drafts it, the user confirms it in the table). The builder verifies this against `promote.py` and records the answer.
- The shadow store is `~/.local/share/code-check/shadow/` on the user's machine, outside the repo. The builder reads it directly and drafts the label tables; the user only confirms them.
- Unposted comparison and a moved head: when the PR's current head differs from the reviewed `head_sha`, still compare but add `head_moved: true` and a reason, since human threads may have landed on later commits and the line window then misleads. The same flag applies to a merged or closed PR (`pr_state`). It is a label on the result, not a skip.
- `calibrated` is only a validated boolean in `evals/live.py`; nothing skips an uncalibrated snippet, so it runs in the live gate. The new snippet is added with `"calibrated": false` and, because a flaky snippet could block later commits, it is committed in its own commit after the cleanups, and switched to `true` only after the user's two runs.

### Release

If the final diff touches the plugin payload (`agents/`, `skills/`, hand-off format), bump `VERSION` and the six other places in the last task so the installed cache refreshes. The new `prompts_hash` makes the effect checkable. Bumping is part of the branch, not a separate step.

## Left to the builder

Task order and sizing; how the plugin root and prompt hash are resolved; the exact Rust snippet text (it must be valid, compact and unambiguous: one defect, no distractors); the test layout for the hand-off changes; wording of the checklist if it is needed.

## Follow-up (filled in as results arrive)

- Race snippet results, two runs: pending.
- Real-run golden case at `53de1f6b`: pending.
- Whether `shadow promote` accepts an unposted run (and a local run): pending, builder verifies against `promote.py`.
