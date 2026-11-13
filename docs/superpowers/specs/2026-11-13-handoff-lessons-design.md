# Lessons from the first two hand-offs

## Intent

The first two real hand-offs (google/guava #8808 and #8838, runs `3bda2df405bf` and `6776d994552e`, in `~/.local/share/code-check/shadow/`) show where code-check wastes work, mis-records its own runs and under-protects review quality. The owner's priority for the golden set: first, do not miss real bugs; second, keep noise down. Fix what the hand-offs show, and turn the two runs into regression protection.

## What the hand-offs showed

1. **Cross-run contamination.** Both runs came from one Claude session. `find_transcript` returns the newest transcript containing the run id, and `subagent_transcripts` returns every subagent of that session. Run 2's hand-off therefore includes run 1's four subagents and three tool errors, and the main session's turn and token counts are session totals.
2. **False `ignored_guidance`.** `extract_signals` matches `ignored guidance:` in any assistant text or tool result. Both hand-offs report a README line the session had grepped, not an agent's `done:` line.
3. **Empty comparison.** Both runs report `human_comments: []`, but #8808 has a maintainer comment and #8838 has a maintainer comment ("describe your use case?") and a `CHANGES_REQUESTED` review. Only inline threads are collected.
4. **Failed reads.** Reviewers `Read` `RUN_DIR/CODE_CHECK.md` and `existing-comments.md` although those files did not exist (guidance absent; blind run). The prompts say "if it exists", but nothing tells the agent which files exist.
5. **Failed greps.** A reviewer sent look-around and unbalanced-parenthesis patterns to Grep (ripgrep).
6. **Failed bash call.** The main session spent a failed Bash call locating the plugin install.
7. **Severity disagreement.** On #8808 the architect said suggestion and the skeptic warning for the same point; the aggregator kept suggestion without saying why.
8. **Mirrored trees.** On #8838 the same defect in `guava/` and `android/` became two near-identical criticals.
9. **Scope and docs noise.** An out-of-scope test change and a missing `@since`/Javadoc point were both posted as warnings.
10. Reviewers did not raise the maintainer's motivation question. Judged speculative from one PR and left out (see non-goals).

## Goals

- Hand-offs describe exactly one run.
- No spurious `ignored_guidance`; the comparison sees non-inline human feedback.
- Fewer failed tool calls in reviewer and main sessions.
- Aggregator rules: higher severity wins on disagreement (with the reason recorded), mirrored-tree findings merge, scope and docs-only findings cap at suggestion.
- Regression protection for missed real bugs (a live snippet) and for pipeline behaviour (golden cases from the two runs).

## Non-goals and anti-goals

- No "maintainer-style question" reviewer behaviour (approach C). Revisit with more hand-offs.
- No change to the posting policy, `post` anchoring or the CLI's JSON contracts beyond additive fields.
- Anti-goal: a severity rule that makes real warnings disappear. The cap applies only to findings that are both out of the PR's stated scope or docs-only, and never to a finding that describes a build, correctness or security defect.
- Anti-goal: private code reaching the repo. Both runs are from public google/guava; later promotions still follow the existing public/private rule.
- Anti-goal: golden labels set without the owner. Every label in `tests/golden/` is confirmed by the owner.

## Constraints

- All `CLAUDE.md` invariants hold (shadow side effects through `ctx.guarded`, one JSON document per command, scrubbed output, new commands in both import list and `ALL`, `agents/*.md` regenerated with `gen-agents`, skills and README matching the parser).
- Aggregator rules are prompt-level (it is an LLM agent), so they cannot be asserted by replay; only the live snippets check them.
- The commit gates run `eval --live` when `agentgen.py` or `agents/` change; expect that to run when this ships.

## Approaches considered

- **A. Capture fixes only.** Cheapest; replay protects only the pipeline, not reviewer recall. Rejected: does not address missed bugs.
- **B. A plus aggregator rules plus a live snippet (chosen by the owner).** The snippet is the only thing that can guard recall; the rules and golden cases guard noise and pipeline.
- **C. B plus maintainer-question prompting.** Speculative, and risks noise. Rejected for now.

## Design

Parts ship in this order, each its own commit or PR.

### 1. Run-scoped hand-offs

`handoff` scopes transcripts to the run. Verified on the real transcripts: every subagent transcript's first user message is `RUN_DIR=<state>/runs/<run_id>`, and every entry (except a few bookkeeping lines such as `last-prompt`, which have no `timestamp`) carries a `timestamp`. So: keep a subagent transcript only if its first message names this run's directory. For the main transcript, count only entries timestamped between the run's `ts` (shadow `run` record) and the `handoff` call; entries without a timestamp are skipped. If the main transcript cannot be scoped, record `transcript_reason` and report only the subagent signals, saying so in `handoff.md`, rather than reporting session totals as the run's. Tests use small synthetic transcripts with two interleaved runs, not the files in `~/.local/share`.

`_guidance_lines` accepts only a line that is an agent's final `done:` line (starts with `done:`) in assistant text of a subagent transcript. Tool results are never scanned.

### 2. Non-inline human feedback in the comparison

`handoff` also collects PR review bodies (any state except the author's own, skipping empty bodies) and issue-conversation comments, excluding our own (run marker `<!-- code-check:run:... -->`) and bots listed as such by the provider. The PR author's own issue comments are included and labelled with their author. They appear in `comparison` under a new `general_comments` list with author, kind (`review`/`comment`), review state where applicable, and a scrubbed body excerpt. They are never matched to findings (no file or line); they are context for the reading session and for labelling. Additive: `human_comments`, `unraised_findings` and `misses` keep their meaning. Needs a provider method for issue comments (reviews already exist).

### 3. Fewer failed tool calls

- `context` reports `run_files`: which of `CODE_CHECK.md`, `existing-comments.md` exist in the run dir. Reviewer and aggregator prompts say to read only the files listed in `run_files` of `context.json` (and the skills pass the same list in the task message). Remove the "if it exists" wording that invites blind reads.
- Reviewer and aggregator prompts get one line: Grep is ripgrep; escape literal parentheses, no look-around or backreferences; use `-F` style fixed strings for code snippets.
- `run_files` lists only the optional run-dir files `CODE_CHECK.md` and `existing-comments.md` that exist; it is an additive field of `context.json`, and `test_plugin_static.py` / README are updated with it.
- Skills: replace the plugin-install lookup with the documented `bin/code-check` / `CLAUDE_PLUGIN_ROOT` path; the builder checks the actual failing command in the main transcript (`b83d354c...jsonl`, first Bash error) before choosing the fix.

### 4. Aggregator rules (prompt text in `agentgen.AGGREGATOR`)

- **Severity disagreement.** When reviewers rate one root cause differently, keep the highest severity the aggregator can verify against the code; if it downgrades, the `message` says why. (Today step 4 lets it downgrade silently.)
- **Mirrored trees.** When the same defect appears at the same relative location in parallel trees (for example `guava/` and `android/`), emit one finding anchored in the primary tree, name every other path and line in the message, and list all reviewers.
- **Order of rules.** Merge, then reconcile severity, then apply the scope and docs cap. The cap wins over "highest severity" for a finding that is only a scope or docs remark. The stated scope comes from the PR title and description in `context.json` (local mode: the branch name and commit messages only; the cap then applies to docs-only findings alone).
- **Scope and docs.** Findings that are only (a) an unrelated change outside the PR's stated purpose with no defect in it, or (b) missing `@since`, Javadoc or similar documentation, are capped at suggestion. A scope finding that also shows a concrete defect keeps its severity; the defect, not the scope remark, is then the finding.

After editing, run `gen-agents --out agents`; the drift test must pass.

### 5. Golden cases from the two runs

Promote both runs (`shadow promote`, then the owner confirms labels, then `--finalize`). Proposed labels for the owner to confirm:

- #8838: `build` critical on `TypeVisitor.java` is `expected`. `CollectionRemoveIfTester` correctness warning is `expected`. The `@since`/Javadoc warning is `not_expected` at warning (it would now be a suggestion).
- #8808: the `ci` finding is `expected` at suggestion (maintainer reported the run passes; no bug was missed).

Note: `expected_posted` is whatever the pipeline produced at promotion time, so the `@since` finding stays a warning in the golden case's replay (the aggregator rule does not re-run in replay); that is correct and is why part 6 exists.

### 6. Live snippets

Add `tests/snippets/weaker-access-override/`: a two-file Java change in which a base class method goes from package-private to protected while a subclass in the same package, left unchanged, overrides it package-private. Expected: one `build` or `correctness` critical on the base method line, found by the panel. `must_not_report`: any critical on the unrelated formatting change included in the snippet. Add `tests/snippets/docs-only-api/`: a new public class with no `@since`/Javadoc and an otherwise correct change; expected findings none above suggestion; `must_not_report` a warning or critical about docs. Marked `calibrated: false` like the existing cases (a test asserts that for every shipped snippet; the flag does not affect gating). The gate does block on a failing snippet when a model is reachable, so the builder runs `eval --live` by hand and tunes the snippets and prompts until both pass before committing.

## Left to the builder

- How to attribute transcripts to a run (time window or prompt content), and the exact window boundaries.
- Provider method name and shape for issue comments; bot detection.
- Exact prompt wording within the rules above.
- Where `run_files` appears in `context.json` and how skills pass it.
- Exact snippet file contents and line numbers.

## Ordering and ownership

Parts 1 to 4 are independent of each other apart from sharing `handoff.json`; ship them in order, each merged before the next starts. Part 5 starts after 1 and 2 are merged (it needs correct hand-off data) and ends at a checkpoint: the builder drafts the cases with `shadow promote` from the local shadow store (which exists on the owner's machine), and `--finalize` waits for the owner to confirm the labels. Part 6 starts after 4.
