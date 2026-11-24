# Maintaining code-check

Developer and maintainer tooling: shadow mode, hand-off data, golden cases, evals, hand-off rounds and the commit gates. None of this is needed to use the skills. Settings for these features are in [CONFIGURATION.md](CONFIGURATION.md); the design is in [ARCHITECTURE.md](ARCHITECTURE.md).

## Shadow mode

Use it first alongside human reviewers. It does not change behavior; it records what the tool did so human feedback can improve the skills and become a golden evaluation set.

Everything is local: nothing leaves your machine. It is written as JSONL under `shadow.store`: the run, every reviewer finding, which ones the aggregator kept, what was posted, the decision, and the verdict for every comment `address-comments` evaluated.

`shadow.handoff` (default `false`) turns on the hand-off data recording and makes the shadow store available even when `shadow.enabled = false`. Like the rest of `[shadow]` it can only be set in your user config or with `CODE_CHECK_SHADOW__HANDOFF=1`, never in a repository's `.code-check.toml`.

### Hand-off data

With `shadow.handoff = true`, `context` reports `handoff: true` and the `review-local` and `review-pr` skills run one extra step after `post` and before `cleanup`: `code-check handoff --run <run_id>`. With the flag off the skills skip that step, so nothing about a run changes; called directly, the command prints `{"handoff": false}` and writes nothing. It is an internal step and is not listed in `code-check --help`. It never fails a review: any error comes back as `{"handoff": false, "error": ...}`.

It writes to `<shadow.store>/<run_id>.handoff/`:

- `handoff.json`: the run (skill, mode, repo, PR and its URL, head SHA, a `config_hash` of the effective configuration, models, panel, and the plugin build: `plugin_root`, a 12-character `prompts_hash` over the agent and skill prompts, and `build_reason` saying why those two are null; the root is `CODE_CHECK_PLUGIN_ROOT`, which `bin/code-check` exports, else `CLAUDE_PLUGIN_ROOT`, else the checkout), each reviewer's status from `ingest`, the aggregated findings in the order `post` uses, the aggregator's `dropped` list, the decision, `ignored_guidance` (up to 20 transcript lines, from agents' replies and tool results, that report `ignored guidance:`), deterministic `signals` from the session transcript (failed tool calls, retries, permission denials, turns and output tokens per agent), and for a posted PR review a `comparison` of human comments with our findings: which ones matched (same file, line within 3), the severity a human gave when the comment starts with Critical, Warning or Suggestion (otherwise null), findings no human raised, and human comments we missed (every comparison entry carries the comment's scrubbed `body`). It fetches the PR's threads and writes the same `human_comment` records as `shadow collect`. A PR review that was not posted (stale head, dry run) is still compared, against the threads as they are now, and is marked `comparison_mode: "unposted"` with `head_moved` (whether the PR head differs from the reviewed one; null when the run recorded no head) and `pr_state` (open, closed or merged). `general_comments_reason` says why the PR's general comments are missing when fetching them failed (null otherwise). A draft PR or a local review gets no comparison and says why.
- `handoff.md`: the same run as a short narrative for a person or a later session.
- `transcript/`: copies of the Claude Code session transcript that mentions the run id (under `$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`, newest first, last 24 hours) and its subagent transcripts. When none is found, `handoff.json` says so with `transcript: false` and a reason.

Two things the design lists are deliberately not in `handoff.json`: tokens and duration per reviewer (subagent transcripts are named by file, not by panel member, so they cannot be attributed reliably; `signals` gives turns and output tokens per transcript file instead) and stale-PR retries (a stale review restarts the skill with a new run id). The docstring of `src/codecheck/shadow/handoff.py` lists the fields.

It also saves the raw reviewer and aggregator outputs with the panel and chunk metadata as `<run_id>.raw.json`, so the run can be replayed later.

**Blind mode.** In PR mode a hand-off run is blind: `context` does not give the reviewers the PR's existing comments or replies (no `existing-comments.md`) and reports `blind: true`, so their findings can be compared with what humans said independently. Blind reviews can repeat a point a human already made; turn `shadow.handoff` off for reviews you want to post as final.

**Privacy.** Transcripts hold everything the session saw: code, PR text, command output. They are copied with the same pattern scrub as the rest of the shadow data, which is best-effort and can also garble an occasional line, so the copies are for reading, not parsing. Treat the shadow directory, and the hand-off folders in particular, as sensitive.

Secrets are scrubbed before anything is written (private keys, GitHub, Anthropic, AWS, Slack, Stripe and Google tokens, bearer and authorization headers, JWTs, and `password=`/`token=`-style assignments). The scrub is pattern matching and therefore **best-effort**: it cannot guarantee that every secret is removed, so treat the shadow directory as sensitive.

```bash
code-check shadow collect --all                       # later: record what humans did with each comment
code-check shadow export-golden --out golden.jsonl    # auto-labelled examples (confirmed, disputed, replied, misses)
```

`collect` records resolved or outdated threads, human replies and reactions, and human comments the tool never raised. Findings nobody reacted to are false-positive candidates; human comments with no matching finding are misses. Labels are marked `auto` for a human to review.

### Golden cases and replay

A golden case is one JSON file in `tests/golden/` holding a review run frozen in time: the diff, each reviewer's stored findings, the aggregator's raw output, and what the pipeline posted for it as a human confirmed it (`expected_posted`: file, line, category, severity and whether it went inline or only into the summary; and `expected_decision`, the decision the approval policy would reach with `auto_approve` on, so `APPROVE` exactly when nothing critical or warning was posted). Only cases with `label_status: "confirmed"` are accepted. The repository ships one hand-built synthetic case, `seed-pipeline.json`.

```bash
code-check eval --replay                      # replay every case in the plugin repository's tests/golden
code-check eval --replay --cases path/to/dir  # or another directory of cases
```

**Promoting a hand-off run.** A run made with `shadow.handoff = true` keeps its raw reviewer and aggregator outputs, so it can become a golden case. Promotion is two steps and works offline from the shadow store:

```bash
code-check shadow promote --run <run_id> --out drafts/case.json   # 1. write a draft with label_status "auto"
code-check shadow promote --finalize drafts/case.json             # 2. after you confirmed it: write tests/golden/<repo>-<pr>-<sha8>.json
```

The draft (written 0600, since it holds the PR's code) labels each posted finding `expected` when its thread was resolved or outdated, a human comment matched it or a recorded verdict says `valid`, `not_expected` when a recorded verdict says `incorrect` (`shadow record-verdict`), and `unlabelled` otherwise. A reply alone is only a signal: it labels the finding `replied` in the export and leaves it `unlabelled` in the draft, because "thanks, fixed" and "this is not a bug" look the same to a deterministic rule; human comments with no matching finding become `misses`, and the hand-off's `signals` are copied in when a `handoff.json` exists (counts and per-agent stats only: each tool error keeps its agent and tool name, never the error text). `expected_posted` and `expected_decision` come from replaying the stored aggregator output, with absolute worktree paths made repository-relative as `post` does. Check every label, set each to `expected` or `not_expected`, and set `label_status` to `"confirmed"`. `--finalize` refuses a draft that is not confirmed, still has an unlabelled finding, or does not replay exactly, then writes the case scrubbed (0644, never through a symlink) into `--dest` (default: the plugin repository's `tests/golden`). If that case file already exists with different content it refuses; pass `--force` to overwrite it deliberately (identical content is left as is and succeeds). **License:** a golden case contains the PR's diff, which is someone else's code, so `--finalize` also refuses a case whose repository license is not in `golden.licenses` (a lowercase SPDX id, read from the host when the run is made and kept in the case as `repo_license`; unknown counts as not allowed). Pass `--allow-license` only when you have the right to publish that diff, for example for your own code. Every source project is credited in `THIRD_PARTY.md` (its license text is in `licenses/`), and a test fails until a new project is added there. The draft records its `run_id`, and `--finalize` refuses a draft whose `repo_license` differs from the one the run recorded, so editing the draft cannot widen it. **Privacy:** golden cases are committed, so a case from a private repository, or one whose visibility is unknown (`repo_private` null, for example a run made offline), is refused unless you pass `--allow-private` for that case. Runs recorded without `shadow.handoff` cannot be promoted. The command is developer tooling and is not listed in `code-check shadow --help`.

**Synthetic cases.** A case from a private repository, or from one whose license does not allow publishing its diff, can still teach the reviewer something. A synthetic case keeps the lesson and drops the code: a new, invented [live snippet](#live-snippet-eval) with the same kind of defect. Two deterministic steps bracket the part a model does:

```bash
code-check shadow promote --brief case.json --out brief.json           # 1. what is general about the case, nothing that identifies it
# 2. fill in each "pattern" with one sentence about the kind of mistake, then have a model write a snippet from brief.json ALONE
code-check shadow promote --check-synthetic snippets/<name> --source case.json --against brief.json   # 3. refuse a snippet that leaks
```

The brief holds the language, an empty `allow` list, and for each labelled finding its label (`expected` or `not_expected`), category and severity, with an empty `pattern`; it holds no file name, line, message, code, repository, PR or id, and is written 0600 (`--brief` refuses an `--out` that is the case itself, and a category that contains a name from the source). The check validates the snippet against the live-eval schema, then refuses it when it shares anything distinctive with the source (compound identifiers, words of 12 or more characters in code, paths, host names, the repository and owner names, and any copied line of 30 or more characters, in the name or in the base or change files; reviewer text, finding messages and human comments add the compound identifiers they mention but not plain long words; it refuses a `--source` that has no diff, since that comparison would pass vacuously; it prints the offending terms and copied lines, so run it where the source is, read it yourself, and never pass it to the model that writes the snippet), when it does not match the brief (its language, and the same number and categories of expected findings, while severity is advisory because the original's context set it, at least as many `must_not_report` entries as findings that were wrong), or when it is already marked `calibrated`. A defect often needs standard-library or well-known API names (a `try_lock` bug needs `try_lock`), which the source shares by necessity: list those in the brief's `allow` when you fill in the patterns (or pass `--allow <term>`), and read what the check prints before you allow anything. A name you allow is excused everywhere, so allow only generic API names, never a project's own. Run the snippet with `eval --live --only <name>` twice before marking it `calibrated`. The leak check finds shared names and copied lines; it cannot judge whether the structure is a close paraphrase, so read the snippet. Whether abstracting a pattern from a company's code is acceptable is that company's policy decision.

`eval --replay` runs each case's stored outputs through the same deterministic steps as `ingest` and `post` (validation, duplicate merging, anchoring to the diff, the inline cap, the approval policy and rendering the comments and summary, whose text is discarded) and compares the result with the case: file, line, category, severity and inline-or-summary of each posted finding, in order, and the decision. It prints `{"passed": ..., "cases": N, "failed": [{"case": ..., "problems": [...]}]}`, each problem naming the finding that changed; the exit status is 0 either way, so read `passed`. It needs no network and no model access: it runs inside `pytest` (one test per case file) and as its own step in CI, which fails when `passed` is false. An installed package has no `tests/` directory, so there the default replays nothing and passes.

### Live snippet eval

For how the evals fit together (replay, live, calibrate and the score history), see [docs/Evals.md](Evals.md).

Replay checks the deterministic part. `eval --live` checks the model part: it runs the real reviewer panel and aggregator on small code snippets in `tests/snippets/` and compares what they report with what each snippet expects.

```bash
code-check eval --live                        # every snippet in the plugin repository's tests/snippets
code-check eval --live --only off-by-one      # one snippet
code-check eval --live --snippets path/to/dir # another directory of snippets
code-check eval --live --if-reachable         # report a skip instead of an error when no model answers
```

**It is local only and costs real tokens.** Each snippet is one full `review-local` run through `claude -p`, run once, one snippet after another, never retried. It is not run in CI (the runners may have no model access, and fork PRs get no secrets); CI runs the unit tests and `eval --replay`.

A snippet is a folder holding `case.json`: `{"name", "base": {path: content}, "change": {path: content}, "expected": [{"file", "line", "category", "severity"}], "must_not_report": [{"file", "category"?, "line"?, "severity"?}], "calibrated"?}`. For each snippet the command builds a throwaway git repository in a temporary directory (a bare origin with `base` committed on `main`, and a clone holding `change` as an uncommitted edit), then runs `claude -p` in the clone with `--plugin-dir` (`$CLAUDE_PLUGIN_ROOT`, else this repository), `--permission-mode` from `run_pr.permission_mode`, `--permission-prompts none`, `--add-dir` for the temporary directory and `--allowedTools` from `run_pr.review_allowed_tools`. The run is isolated through environment variables: `CODE_CHECK_STATE_DIR` and `CODE_CHECK_SHADOW__STORE` point into the temporary directory and `CODE_CHECK_SHADOW__HANDOFF=1` turns hand-off on (plus `CODE_CHECK_REMOTE=origin` and `CODE_CHECK_BASE_BRANCH=main`, and `CODE_CHECK_REVIEW__SECURITY=0` so your `review.security` does not add a security agent to every snippet), so your own state, shadow store and review bookkeeping are never touched. Repository-selecting git variables such as `GIT_DIR`, `GIT_WORK_TREE` and `GIT_INDEX_FILE` (which git exports to hooks) are unset with `env -u` for the repository setup and for the review, so a run started from a git hook cannot commit into or push from your own repository. Snippet paths may not name `.git` in any spelling (any case, trailing dots or spaces, `GIT~1`). The findings are read back from the aggregator output the hand-off step saved, and the directory is deleted afterwards. Nothing from a snippet's content reaches a command line. Before any review, a one-line `claude -p` probe checks that a model is reachable; without `--if-reachable` an unreachable model is an error (`model not reachable: ...`), with it the output is `{"passed": true, "skipped": "...", "cases": []}`.

Matching is tolerant, because live reviews vary from run to run. A reported finding matches an expected one when the file is equal, the line is within 2 and the category is equal. Reviewers choose categories freely, so an expected `category` may be a list of acceptable names, compared case-insensitively. The severity must then be equal for the finding to count; a right-place, wrong-severity finding is listed under `severity_mismatches`. Each reported finding matches at most once. A case passes when at least 80% of its expected findings match, no `must_not_report` entry is hit (same file and, when given, a category in the list, a line within 2 and a severity in the list), and no Critical appears that matches nothing expected. A run that fails, that leaves no hand-off output, or whose aggregator findings are all invalid, fails its case. The output is `{"passed": ..., "cases": [{"name", "result": {"passed", "matched", "expected", "recall", "severity_mismatches", "unexpected_critical", "forbidden_hits"}, "got": [...], "claude": {...}}]}`; the exit status is 0 either way, so read `passed`.

**Not yet verified on a real machine.** Real-machine smoke tests of `eval --live` have not been done, so these real-CLI behaviours are unverified: whether `--plugin-dir` loads the plugin twice when it is also installed as a plugin, whether a headless `claude -p` discovers the `review-local` skill from `--plugin-dir`, and whether `bin/code-check` bootstraps its runtime correctly inside the nested `claude -p` process.

**Calibration.** Four of the five shipped snippets are marked `"calibrated": true`; `rust-shutdown-race` is not yet (`"calibrated": false`). The four were run by hand twice against a real model on 2026-11-13 and passed every time, with the expected finding at the expected line (`weaker-access-override` is reported under the category `compile`; `docs-only-api` yields at most a suggestion, such as the `isBlank` versus `trim()` semantics point). Live results still vary from run to run, so rerun a snippet that fails once before changing it.

### PR replay (developer tooling)

`python -m codecheck.evals.replay` re-reviews a real pull request with the real model and scores the findings against the human comments it received. It is a module, not a `code-check` subcommand, **it costs tokens**, and nothing runs it in CI or in the gates.

```
python -m codecheck.evals.replay manifest --shadow DIR --clone OWNER/REPO=PATH --owner YOU RUN_ID... > cases.json
python -m codecheck.evals.replay run cases.json --dry-run                 # rebuild and check every case, no model
python -m codecheck.evals.replay run cases.json --budget 10 --out results.jsonl
```

`manifest` builds one case per hand-off run (`shadow.handoff` must have been on) from a local clone: the head commit, its merge-base, the saved diff and the human comments with their file and line (no comment text). Bot logins (`name[bot]`, GitLab Duo, and a short list of known review bots, compared whole) and your own login are left out. `run` rebuilds a throwaway repository per case (`origin/main` at the base, `HEAD` at the head), refuses a case whose `git diff` is not the saved diff, runs one headless review in isolated state and counts a comment as found when a finding lands on the same file within `--tolerance` lines (default 3). It never touches your clones, state or shadow store.

A replay pins `CODE_CHECK_PYTHON` to the interpreter that runs it (unless you set one), so the review runs this checkout's CLI; `bin/code-check` would otherwise use a cached venv that is rebuilt only when `VERSION` changes, and an older build would drop a seed or a new setting without saying so. A replay reviews real PR contents with the shipped review tools (Bash, Write and Agent included) and your environment, so a PR can carry instructions aimed at the reviewer; the review also reads your own config and `CODE_CHECK_*` environment (security, performance, routing), so the panel you measure is the one you would run, and `--fixed-panel` pins it; replay only PRs you would let `run-pr` review, and only on a machine where that is acceptable.

Other options: `--limit N`, `--only NAME`, `--fixed-panel` (the live-eval panel with routing off, instead of the panel as shipped), `--max-per-author N` (so one reviewer cannot dominate the score). Each result is appended to `--out` as soon as its review ends, so a crash keeps the finished cases. `--budget USD` (finite, >= 0; `--limit`, `--tolerance` and `--max-per-author` take whole numbers >= 0) stops new cases once the total is reached; a review that reports no cost (a timeout, output that is not JSON) is listed under `unpriced`, and with a budget set no further case starts after one, because the spend is no longer known. The last case can overshoot the budget by one review.

A PR that was reviewed several times can be replayed at each head with `from-git`, which builds one case per head from a clone and a label file listing the findings the reviews made over time (see `evals/replay-labels/README.md`): `python -m codecheck.evals.replay from-git --clone PATH --repo OWNER/REPO --base SHA --labels FILE HEAD... > cases.json`. `run --raw-dir DIR` also saves each case's raw reviewer and aggregator output, and each result then has a `funnel` saying, per label, whether the finding was kept, dropped by the aggregator (with the reason) or never reported; `score` reports recall by review round (`by_round`) and by kind of label (`by_kind`): `first-head` (detectable in the code the first review saw), `fix` (the problem was added by an earlier fix, `introduced_in`) or `tail` (a rare edge case, `tail: true`, which the panel is not expected to find). A finding counts for every `file:line` its message names (a short path such as `plugin.py:41` counts for any reviewed file whose path ends with it), so `anchor_matched` and `anchor_recall` give the same figures counting only the line a finding is anchored to; compare arms on those when the prompts differ in how they word findings.

To replay a later round with the findings of the earlier ones in view, as a PR's real reviewer sees them, run the cases once, then `python -m codecheck.evals.replay seed cases.json results.jsonl > seeded.json` and run `seeded.json`. Cases are taken in file order: each one after the first gets the earlier cases' findings as `seed_comments`, which the replay writes to the workdir and hands to the review through `review.seed_comments`, so local `context` copies it in as `existing-comments.md` tagged `[ours]`. The author's replies are not reproduced, and a finding repeated by several rounds is one comment.

### Judge calibration (developer tooling)

`eval --calibrate` measures an LLM judge before anything relies on it. It asks the judge model (`models.judge`, default `opus`) about every human-labelled finding in `tests/golden` (`expected` means a person acted on or agreed with it, `not_expected` means a person disputed it): is this finding worth raising for this diff? It prints `{"passed", "agreement": {"n", "agree", "rate", "kappa", "errors"}, "controls": {"n", "said_same", "errors"}, "cost_usd", "cases": [...]}`. It passes when the judge agrees with the humans on at least 80% of the findings and no answer was unusable. The human comments the review missed are asked against the findings in the same file as negative controls; they are listed for a person to read and do not decide `passed`.

```bash
code-check eval --calibrate                    # every golden case
code-check eval --calibrate --only CASE_ID     # one case
code-check eval --calibrate --cases path/to/dir
```

Like `--live` it is local only, costs real tokens and probes that a model is reachable first. The judge gets no tools, its prompt goes on stdin, and findings, comments and diffs are fenced as untrusted data. A reply must be one JSON object with a real boolean and a short reason, otherwise it counts as an error: the run fails and that item is left out of the agreement rate and kappa. The judge is advisory: it is not run in CI or by the commit gates, and `handoff-queue check` does not depend on it. First real run (2026-11-15, `opus`): 14 findings judged, 50% agreement, kappa -0.32, so it fails the 80% bar; a diff-only "worth raising" question does not track human reactions, and nothing should rely on it yet. The run cost about $1 for 23 calls. Findings without message text are skipped and counted under `skipped`. Decision: `decisions/0023-the-llm-judge-is-advisory-only.md`.

### Eval score history (developer tooling)

Every full `eval --live` and `eval --calibrate` run appends one line to `docs/eval-history.jsonl` (time, commit, branch, whether the tree was dirty, the models in use, the score and the cost) and rebuilds `docs/eval-history.md`, which holds a Mermaid chart per mode with a rolling average of three runs and a table of the latest runs. Nothing is committed for you: the run prints a reminder on stderr, and you commit both files with your change, so the trend lands on `main` with the PR. The record is append-only and `.gitattributes` merges it with `merge=union`, so two branches that each ran an eval keep both lines; the report is generated and never edited by hand.

```bash
code-check eval --history               # rebuild docs/eval-history.md from the record (no model, no network)
code-check eval --live --no-record      # run without recording
```

The two charts mean different things. The live score is the share of snippets whose expected finding the real panel produced, so it is the one that follows the skills and agents. The calibration score is the judge's agreement with the human labels: it follows the judge, not the reviewers, and moves only when the judge, its prompt or the labels change. Live results vary from run to run, so read the rolling line and check the models column before crediting a prompt change. Runs with `--only`, a custom `--cases` or `--snippets` directory, a skipped run and a failed recording (a warning on stderr, never a changed result) are not part of the trend; `--only` runs stay in the record but out of the report.

### DeepEval metrics (advisory, developer tooling)

`evals/deepeval/` holds deterministic metrics (line anchoring, message length, process notes in kept messages, comment-cap findings, missing findings files) that run over the golden cases or your own shadow store with no model calls. They are an optional `evals` extra, never a gate, not run in CI and not part of the plugin. See `evals/deepeval/README.md` to run them and `evals/deepeval/FAILURE-MODES.md` for what they were built for and which judges did not work.

## Hand-off rounds (maintainers)

Hand-off folders pile up in `<shadow.store>`. The project skill `.claude/skills/handoff-round/` turns **one** pending hand-off into a pull request: it analyzes the hand-off, writes failing tests first, fixes the tooling (prompts only when a live snippet fails twice), promotes a golden case when every label is backed by human evidence, runs the tests, a review and the evals, opens the PR, waits for CI, merges it with `gh pr merge --squash --admin`, and marks the hand-off analyzed. With nothing pending it prints that and exits, so it is meant for `claude -p` in a loop you write yourself (`scripts/handoff-loop.example.sh` is a starting point; it sleeps and refreshes the plugin between rounds).

Where hand-offs come from and how a round runs:

```mermaid
flowchart TD
    review["A review runs (review-pr or review-local)<br/>with shadow.handoff = true"] --> record["The handoff step after post writes a<br/>hand-off folder: the run, findings,<br/>what the aggregator dropped, signals,<br/>transcripts"]
    record --> queue[("Pending hand-offs<br/>public repositories only")]

    script["scripts/handoff-loop.sh<br/>starts one round, sleeps, refreshes the plugin"] --> round["claude -p runs the handoff-round skill"]
    round --> pre{"0. Preflight<br/>checkout clean and on main?"}
    pre -->|no| exit1(["Exit, nothing claimed"])
    pre -->|yes| claim{"1. Claim<br/>handoff-queue next"}
    queue -.-> claim
    claim -->|nothing pending| exit2(["Exit"])
    claim -->|a hand-off| analyze["2. Analyze (untrusted data)<br/>tooling gaps, noise, misses,<br/>prompt candidates: act or note"]
    analyze -->|no act items| release["release the claim<br/>retried until 3 attempts, then left alone"]
    release --> exit3(["Exit"])
    analyze -->|act items| tests["3. Failing tests first"]
    tests --> fix["4. Fix with the smallest change<br/>prompts only if a live snippet fails twice"]
    fix --> golden["5. Golden case, only if every label<br/>is backed by human evidence"]
    golden --> verify["6. Verify<br/>ruff, pytest, eval --replay"]
    verify --> review2["7. review-local on the branch<br/>then eval --live"]
    review2 --> bump["8. Patch version bump if the plugin payload changed"]
    bump --> pr["9. Open the PR"]
    pr --> ci{"10. CI green?"}
    ci -->|"no, after 2 fixes"| release
    ci -->|yes| guard{"11. Merge guard<br/>handoff-queue check"}
    guard -->|violations| comment["PR comment, release the claim"]
    guard -->|ok| merge["gh pr merge --squash --admin<br/>--match-head-commit"]
    merge --> done["12. handoff-queue done<br/>hand-off marked analyzed"]
    done --> script
    exit3 --> script
    comment --> script

    classDef llm fill:#fff3d6,stroke:#b7791f,color:#000
    classDef cli fill:#e3f2fd,stroke:#1565c0,color:#000
    class analyze,tests,fix,golden,review2 llm
    class claim,guard,release,done cli
```

The queue and guard run from the **installed plugin**, not from the branch under test, so a round cannot change the code that judges it. Orange steps are LLM work and blue ones are deterministic CLI commands.

Two hidden commands do the exact parts (`code-check handoff-queue`, not listed in `--help`):

```bash
code-check handoff-queue next                          # claim the oldest pending public hand-off (JSON: run, pr, claim, handoff_dir, skipped counts)
code-check handoff-queue done --run ID --claim TOKEN --pr URL   # mark it analyzed; checks the PR is merged
code-check handoff-queue release --run ID --claim TOKEN --reason "..."   # give up for now; retried until 3 attempts, then left alone
code-check handoff-queue check --run ID --claim TOKEN --pr N   # the merge guard (see below)
```

State lives next to the data: `<run_id>.handoff/analysis.json` (0600). Private repositories, runs of unknown visibility and runs without a hand-off folder are never claimed. A claim older than four hours can be taken again. The guard allows changes under `agents/`, `skills/`, `src/codecheck/`, `tests/`, `docs/` and `README.md`, and a one-step patch version bump; it blocks automation, hooks, the installer, `CLAUDE.md`, the queue and guard code itself, edits to existing golden cases, deleted tests or snippets, and diffs over 2000 lines. `check` reads the PR itself, fetches the base itself and judges the PR head commit, which this checkout must be at, so the caller chooses nothing about the range. It also blocks renames of tests, symlinks, mode changes and binary files, odd or case-variant paths, lines that switch tests off, and changes to the guard's own dependencies, the config loader and the unattended-run code. Every queue command needs the claim token `next` printed. Run it from the installed plugin, never from the branch it judges.

## CI review pin

CI reviews pull requests with `subhiandrews/code-check-action`, which installs the code-check release named by its `code-check-ref` input (default `v0.1.5`). `.github/workflows/code-check.yml` sets it to a release tag. Dependabot cannot update a free-text input, so `.github/workflows/code-check-pin.yml` does: when `tag-release.yml` tags a release (it dispatches this workflow; a tag push or `workflow_run` cannot trigger it, because a tag pushed with `GITHUB_TOKEN` starts no workflow and zizmor rejects `workflow_run`), every 4 hours from 06:17 Pacific as a backstop, or on demand (`gh workflow run code-check-pin.yml`), it runs `scripts/update_ci_pin.py` over `git tag --merged origin/main`. When a strictly newer `vX.Y.Z` tag exists it pushes `bot/code-check-pin` and opens (or retitles) a PR `ci: pin code-check to vX.Y.Z`. It never merges, never downgrades, and reports without rewriting a pin that is a commit SHA or unparseable.

Setup: the secret `CI_PIN_BOT_TOKEN` is required for a bump (a fine-grained PAT or app token with Contents, Pull requests and Workflows write). `GITHUB_TOKEN` cannot push changes under `.github/workflows`, so without the secret the run fails with an error naming it; a run with nothing to bump needs no token. A token that is not `GITHUB_TOKEN` also lets the required checks run on the bot PR. The repository setting "Allow GitHub Actions to create and approve pull requests" must be on. The bot branch is bot-owned and is force-pushed with an explicit lease when a newer tag replaces the pin. The workflow cannot be run locally; the script and the pin are covered by `tests/test_update_ci_pin.py`.

## Development

Run `scripts/install-git-hooks.sh` once to enable the checked-in git hooks (pre-commit; `git config core.hooksPath .githooks`; this is repository-level config, so it applies to all worktrees). The pre-commit hook runs ruff and eslint, then `scripts/gates.sh`, which runs `code-check eval --replay` (fast and deterministic). The slow live eval does not run on commit. Nothing runs the live eval automatically when a pull request is opened. To run it by hand, call `scripts/gates.sh --pr`: the replay again, then `code-check eval --live --if-reachable --no-record` (a gate run never writes the score history) only when the branch differs from `origin/main` (override with `CODE_CHECK_BASE`) in a path of `scripts/gate-paths.txt`; without a base to compare with it runs. The live gate skips, and does not block, when no model is reachable. Claude Code runs the replay through `.claude/settings.json` on `git commit`, unless the git hooks are installed (then they run once); `git -C <other worktree> commit` is not covered by it. On success the gates print one summary line to stderr (for example `gates: replay passed; live skipped (no gated path)`), so a pass can be told apart from a hook that did not run. `CODE_CHECK_BIN` overrides the CLI the gates call; it is a command line that is split on whitespace (so it may carry arguments), not a path, which means a path containing spaces cannot be used there. The gates read the `passed` verdict with the CLI's own interpreter, or `python3` when `CODE_CHECK_BIN` is set, and fall back to a plain text match when no Python runs, so a missing parser never blocks a commit.

The manual live gate (`--pr`) can take up to about five times `run_pr.timeout_seconds` (one bounded run per snippet, and five snippets ship) plus the reachability probe. The Claude commit hook has a 900 second timeout, and if a hook times out Claude Code may let the command through, so the git hook is the authoritative gate; the Claude hook is a convenience for people who have not installed it.

## Extending

**A new code host.** Implement the `Provider` protocol in `src/codecheck/providers/base.py` (about a dozen methods that return the dataclasses in `models.py`), register it in `providers/__init__.py`, and run the shared contract suite against it by subclassing `ProviderContract` in `tests/providers/contract.py`. Nothing outside `providers/` needs to change. GitHub uses the `gh` CLI and GitLab the `glab` CLI (`providers/gitlab.py`); Bitbucket is not planned yet. A host whose repository paths can be nested sets `nested_repos = True`.

**The deep reviewer.** Enable `[deep_reviewer]` and give the `deep-reviewer` agent access to your MCP tools, memory or retrieval system. If it is disabled, missing or fails, the panel runs without it.

**Agents.** Prompts and defaults live in `src/codecheck/agentgen.py`; `agents/` is generated from it and a test fails if they drift.

