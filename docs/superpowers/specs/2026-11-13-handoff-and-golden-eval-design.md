# Hand-off data, golden set and eval gates

## Intent

code-check needs a way to get better from its own use. When the user runs `/review-local` or `/review-pr` in their terminal, the run should leave a hand-off: a record of where tool calls failed, where agents went off track, and (for PRs) whether the findings and severities agree with what human reviewers said. A later Claude session reads hand-offs to improve the plugin. The same data, once a human has checked its labels, becomes a golden set that gates future changes, so a prompt, model or pipeline change cannot silently make reviews worse.

Separately: `review-pr` must not review a PR that already has an approval.

## Goals

- Opt-in hand-off per run: structured (`handoff.json`) and narrative (`handoff.md`), including the PR URL for remote PRs.
- Blind comparison: in hand-off runs the reviewers never see human comments; the CLI fetches them only after the review is posted and compares them to our findings (matched, missed by us, unmatched findings, severity agreement).
- A promotion path from collected hand-offs to an in-repo golden set, with a human checking every label.
- Two gates that run on commit: a deterministic replay of the golden set (always) and a live review of test snippets (only when review behaviour may have changed). CI runs the unit tests and the replay, never the live eval.
- `review-pr` skips any PR that already has an approval.

## Non-goals and anti-goals

- No LLM call inside the `handoff` CLI command. Judging "went off track" is done later by a Claude session reading the hand-off.
- No live model eval in GitHub CI (the runners may have no model access, and fork PRs have no secrets).
- Nothing leaves the machine. Hand-offs and transcripts stay in the local shadow store.
- Anti-goal: private code reaching the repo. Promotion must never publish code from a private repo by default.
- Anti-goal: a flaky commit gate that trains people to use `--no-verify`.
- Anti-goal: blind mode silently changing reviews for people who did not turn it on.

## Constraints

- Existing invariants in `CLAUDE.md` hold: shadow side effects go through `ctx.guarded(...)`, recording never changes a command's behaviour (blind mode is the one deliberate, opt-in exception, below), everything scrubbed, `[shadow]` config is user-config-only (a repo's `.code-check.toml` cannot set it), every command prints one JSON document, new commands go in both import list and `ALL`, skills and README must match the argparse parser (`test_plugin_static.py`, `test_readme.py`).
- The existing `[shadow] enabled` (default true) keeps meaning "light recording". Hand-off is a separate switch.
- `ShadowStore` already stores run, reviewer_finding, finding, decision, dropped, outcome, human_comment and verdict records, and `export-golden` already produces auto-labelled rows. Build on these.
- Hook-time env vars are unverified (spike), so nothing may depend on them.

## Approaches considered

Capture of session logs:

1. **Read Claude Code's own transcript after the run (chosen).** A final skill step, `code-check handoff --run <id>`, finds the session transcript under `~/.claude/projects/` (and its subagent transcripts), scrubs it, copies it into the run's shadow folder and extracts deterministic signals. No hooks, nothing added during the run. Trade-off: depends on where Claude Code writes transcripts; this must be verified first.
2. Hooks log tool calls live. Cleaner per-call data, but needs run-to-session linkage that hook env vars may not provide, and adds a hook to every session.
3. CLI-side records only. Simplest and least private-data exposure, but blind to what agents do between CLI calls, which is where drift happens.

Eval in CI:

- A. Deterministic replay (chosen for commit and CI): stored reviewer outputs replayed through ingest, dedupe, anchoring, severity policy and render. Free, fast, no secrets.
- B. Live panel eval (chosen for local commits, path-gated, and `eval --live`): real reviewers and aggregator on snippets. Costs tokens, varies run to run, so it uses tolerant matching and a recall bar.

## Design

### 1. Configuration

```toml
[shadow]
enabled = true     # existing: light recording
handoff = false    # new: session logs, blind reviews, hand-off files
```

`handoff = true` implies `enabled`. Env override `CODE_CHECK_SHADOW_HANDOFF=0|1` for one-off runs. Applies to the next run; turning it off stops all hand-off behaviour immediately, including blindness. User-config-only like the rest of `[shadow]`. The flag value is recorded on the `run` record, and `context` reports it so the skill knows whether to run the hand-off step.

### 2. Blind context (my call: part of the flag)

When `handoff` is on, `context --mode pr` does not write `existing-comments.md` and does not pass replies to reviewers (the files and metadata that normally tell reviewers about them are omitted). It reports `blind: true`. After `post`, `handoff` fetches the threads. The cost, accepted: blind runs may repeat what a human already said, so they are for evaluation, and the user can turn the flag off for reviews they want to post as final. `review-local` has no PR and therefore no comparison; its hand-off has the signals and findings only.

### 3. The hand-off command

`code-check handoff --run <run_id>`, called by `review-local` and `review-pr` (PR mode only if the review was posted; local mode always) after `post` and before `cleanup`. A no-op returning `{"handoff": false}` when the flag is off. Writes to the run's shadow folder:

- `handoff.json`: schema-versioned. Fields: run id, skill, mode, repo, head SHA, `pr_url` (null for local-only branches), tool version, config hash, models, panel; per-reviewer status (output present, valid, finding count, tokens, duration); `signals` (below); our findings with severity, category, kept/dropped; the decision; and for PRs a `comparison` block: each human comment with whether it matched one of our findings, matched finding's severity vs the severity implied by the human comment where one can be inferred (otherwise null), our findings no human raised, and human comments we missed.
- `handoff.md`: a short narrative of the same run for a person or a later session to read.
- `transcript/`: scrubbed copies of the main and subagent transcripts.

`signals` are deterministic: failed tool calls (tool, error text, scrubbed), retries of the same call, permission denials, number of turns and tokens per agent, agents that ended without writing output, invalid or rejected findings and why, `ingest` drops, stale-PR retries, and any `ignored guidance` lines. Deciding whether the agent "went off track" is left to the reading session; the file gives it the evidence.

Finding the transcript: search the project's transcript directory for the one that contains the run id, newest first. If it cannot be found, the hand-off is still written with `transcript: null` and a reason. Recording failures never change the review's outcome (`ctx.guarded`).

Matching human comments to findings reuses the existing nearness logic in `shadow/collect.py` (same file, line within 3). Severity of a human comment is not inferred by guessing text; it is recorded as null unless the comment uses an explicit severity word, and the reading session or the labeller sets it during promotion.

### 4. Skip PRs that are already approved

In `precheck` (PR mode, applies always, not only with the flag): use `Provider.list_reviews` and treat the PR as already approved when any reviewer's latest non-dismissed review is `APPROVED` on the current head SHA. Approvals on older commits do not count. Reason text: "already approved by <login>". `--force` overrides, as it does for the other prechecks. In queue mode the PR appears under `skipped` with that reason. Unaffected: drafts, closed PRs, own PRs.

### 5. Golden set and promotion

`code-check shadow promote --run <run_id> --out tests/golden/<case>.json` converts a collected run into a golden case: the diff, the stored raw reviewer outputs, expected findings with `{file, line, category, severity}` and a label (`expected` or `not_expected`), and misses. Before writing it refuses unless each label has been confirmed by a human (a `--confirm-labels` flag, with the auto labels printed for review) and the repo is public or `--allow-private` is passed for that case. Everything is scrubbed. Auto labels from `export-golden` stay `auto` and never reach `tests/golden/` unconfirmed.

Hand-offs and golden cases built from them are the input for the improvement loop, so `promote` also writes the originating hand-off's `signals` summary into the case's metadata.

### 6. Replay eval (every commit, in CI)

`code-check eval --replay` (and a pytest test calling the same code) runs each case in `tests/golden/` through ingest, dedupe, anchoring, severity policy and render using the stored raw reviewer outputs, and asserts the outputs equal the case's expected findings and severities. Deterministic, no network, no models. It runs inside the normal `pytest -q`, so CI gets it with no workflow change beyond what pytest already does.

### 7. Live snippet eval (local, path-gated)

`tests/snippets/` holds small test-code snippets, each with expected findings (file, line, category, severity) and optionally "must not report" entries. `code-check eval --live` runs the real panel and aggregator on each snippet. Matching is tolerant: a finding matches when the file is equal, the line is within ±2, the category is equal; severity must then equal the expected severity. A case passes when at least 80% of its expected findings match with the right severity, and no `must not report` item and no unexpected Critical appears. The pass bar and tolerance live in one place. Output is one JSON document with per-case results, so a failure is readable.

### 8. Commit gating

A git `pre-commit` hook (installed by a script in the repo, since git hooks are not versioned) and the existing `.claude/settings.json` commit hook both run:

- ruff, eslint (existing).
- `eval --replay` always.
- `eval --live` only when the staged changes touch `src/codecheck/agentgen.py`, `agents/`, `skills/`, default models in `config.py`, or `src/codecheck/core/` ingest, findings, diffmap or policy code, and only when a model is reachable. If no model is reachable the hook prints that the live gate was skipped and why, and does not block.

`eval --live` can always be run by hand. `--no-verify` remains possible; CI's replay is the backstop for the deterministic part.

### 9. Docs and tests

README gets the `[shadow] handoff` key and the new commands (`handoff`, `shadow promote`, `eval`); the skills get the hand-off step; both are checked by the existing static tests. Unit tests use `FakeProvider`/`FakeRunner` and never call real `gh`, `git` remotes or `claude`. Tests cover: flag off is a no-op, blind context omits comments, comparison with matching and missing comments, transcript not found, approval skip rules (approval at head, approval at old head, dismissed approval, changes-requested after approval), promotion refusal rules, replay equality and failure output, live-eval matching rules (with fake panel output).

## Parts, in shipping order

1. Approval skip (independent, small).
2. Config flag, blind context, `handoff` command and skill steps.
3. `shadow promote`, `tests/golden/`, `eval --replay` in pytest.
4. `tests/snippets/`, `eval --live`, the commit hooks and install script.

## Left to the builder

- Where exactly Claude Code stores session and subagent transcripts; verify with a real run before relying on it.
- The `handoff.json` schema details beyond the fields above, and the schema version number.
- The number and content of the first snippets and the first golden cases.
- How the pre-commit hook detects model reachability.

## Resolved details (from the builder check)

- **Raw outputs for replay.** When `handoff` is on, `post` also copies the raw `findings/*.json`, the aggregator's output file and the chunk and panel metadata that `ingest` needs into the run's shadow folder, scrubbed like everything else. `promote` refuses runs that lack them (including every run recorded before this change). So the first golden cases come from runs made after part 2 ships.
- **What replay asserts.** A case stores `expected_posted`: the findings the pipeline produced at promotion time, as the human confirmed them (file, line, category, severity, inline or summary). Replay asserts the pipeline output equals `expected_posted` exactly. The per-finding labels (`expected`, `not_expected`) and misses are not used by replay; they feed the live eval and the improvement analysis.
- **Human-comment records.** `handoff` calls the same matching and record-writing function as `shadow collect` and writes the same `human_comment` and `outcome` records, so `export-golden`, `promote` and `collect` see one set of records and the existing latest-per-thread rule applies. `handoff.json` repeats the comparison for reading convenience; `promote` reads the shadow records, not `handoff.json`.
- **Promotion inputs and confirmation.** `promote` reads shadow records only and works offline; hand-off `signals` are copied in when a `handoff.json` exists and omitted otherwise. Promotion is two steps: `promote --run <id> --out <file>` writes a draft with `label_status: "auto"`; a human edits the labels and sets `label_status: "confirmed"`; `promote --finalize <file>` refuses any file that is not confirmed and then moves it into `tests/golden/`.
- **Public or private.** `context` records `repo_private` on the run via `gh repo view` (null when unknown, for example offline or a branch with no remote). Null counts as private.
- **Approval rules.** `precheck` extends its existing `list_reviews` use (`Review` already has `author`, `commit_id`, `submitted_at`, `state`). Per reviewer, take the latest review whose state is `APPROVED`, `CHANGES_REQUESTED` or `DISMISSED`, ignoring `COMMENTED`; the PR counts as approved when any reviewer's latest such review is `APPROVED` at the current head. Bot approvals count. Our own approval counts (it is the same rule).
- **Not posted.** When the review was not posted (draft, stale, dry run), `handoff` skips the human-comment comparison and records why.
- **Duplicates in blind runs.** `post` already suppresses repeats of our own earlier comments from finding markers in existing threads, which does not use `existing-comments.md`, so that stays. Blind runs can repeat points that humans made; that is accepted (see section 2).
- **Live eval harness.** `eval --live` builds a throwaway git repo per snippet (a base commit plus the change) and runs the `review-local` flow through `claude -p`, the way `run_pr` shells out, with explicit permission flags. Model reachability means a cheap `claude -p` probe succeeds.
- **Two hooks, one authority.** The gates live in one script that holds the path list. The git `pre-commit` hook calls it; the Claude `.claude/settings.json` hook calls it too, with an environment variable set so a commit made by Claude does not run the gates a second time.
- **Transcript to run.** Take the newest transcript (by modified time) that contains the run id; subagent transcripts come from that session's folder only.

## Calls made without you (please check)

- Blind mode is tied to `handoff` (you asked for this flag to turn the whole thing on; blindness was my reading).
- A separate `handoff` key under `[shadow]`, default off, instead of changing `enabled`.
- Approval means an approval at the current head, not older commits.
- Live eval pass bar: 80% recall per case, ±2 lines, no unexpected Critical.
- Human-comment severity is null unless explicit, set by a human at promotion.
- Golden cases from private repos are refused unless explicitly allowed.
- Order of shipping.
