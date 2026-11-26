# Failure taxonomy for the review panel (draft, Step 1)

Status: **draft for the owner to correct.** Written from the stored golden cases, not from running anything.

## How this was made, and what it cannot tell you

- Source: `tests/golden/*.json`. Six cases come from real PRs (2 Guava, 4 Tokio). `seed-pipeline` is hand-built, so it is **excluded** from the evidence below.
- Volume in the six real cases: 46 raw reviewer findings, 14 kept by the aggregator, 18 drop entries (each entry is a merged cluster, so the numbers do not add up). 14 labelled findings (11 `expected`, 3 `not_expected`) and 15 human comments the panel missed.
- I read short excerpts of each finding and the diff line ranges. **I did not check any finding's claim against the code.** So "is this finding true?" is unassessed. Nothing below says a finding was wrong on the merits.
- Labels are weak truth. `expected` means a person acted on it. `not_expected` means a person disputed it, which can be a preference and not a mistake (`docs/Evals.md`). Labels carry no human opinion on severity.
- Every count is tiny. Read them as "seen at least once", not as rates.

Each mode has an evidence grade: **observed** (several examples), **single** (one example), **suspected** (no example yet, should be checked).

## Modes

### F1. The aggregator drops a finding that a human later raises. (single: one case, not replicated, see the 2026-11-20 update below)
- In `tokio-local`, 2 of the 4 missed human comments match a finding the panel raised and the aggregator dropped as `low-value`: the `park.rs:323` lock-contention branch ("should be `expect()`") and the loose `< ROUNDS` leak threshold in `rt_shutdown_fds.rs`. Both were confirmed by `f1_baseline.py` (same file, within 5 lines) and by reading the text.
- A third (a human at `driver.rs:306` asking for a test of `unpark` racing) is mentioned in the text of the dropped `rt_shutdown_fds.rs:57` finding, which is in a different file. Location matching cannot see that, so the baseline counts it as `kept` (a different concern sits at that line). An earlier version of this note said "3 of 4" and counted it.
- **Baseline over all 6 real cases** (`f1_baseline.py`, 15 missed comments, window 5): 2 kept, 2 dropped, 1 raw only, 9 same file but not nearby, 1 not raised. The 9 "same file" are mostly the F2 design comments. The window matters: kept goes from 1 (window 0–2) to 4 (window 10+). A nearby finding is a candidate, not proof of the same concern: the `raw_only` match (`uring.rs:283`, a deleted-paragraph finding) is a different concern from the human's `uring.rs:288` comment about use after free.
- Location matching alone is too coarse for a metric. It finds candidates, and a judge or a person has to decide "same concern". That is the next step.
- The same `park.rs:323` concern is **kept** in `tokio-8735` and a human labelled it `expected`. It is **dropped as low-value** in `tokio-local`. The aggregator is inconsistent on the same issue.
- Two more drops are marked `not-verified` (`8735` test threshold, `8779` doc paragraph deleted by the PR). I did not check whether humans raised them.
- Caveat: some drops are surely right, because noise needs filtering. The question is the rate of wrong drops, which nobody measures today.
- **This is an aggregator problem, not a reviewer problem.** It is outside the "reviewer findings quality" target we agreed on. Decision for the owner below.

### F2. Reviewers lack project knowledge that maintainers apply. (observed, probably out of scope)
- `tokio-8757`: 8 of the 15 missed comments are maintainers steering the design ("don't change this signature, it is a breaking change", "keep the old `create_io_stack`"). The panel did flag the behaviour change but not the maintainers' preferred shape.
- Not fixable from the diff alone. A metric would only record a ceiling on what a diff-only reviewer can do. Candidate for `CODE_CHECK.md` guidance, not for a metric.

### F3. The first-order problem is found, the second-order consequence is not. (single)
- `tokio-8779`: the panel caught that the doc overstates a panic-safety guarantee (`uring.rs:279`). Humans also raised what follows from it: state is not recorded when `after_push` panics, and the slot can then be used after free (`op.rs`, `uring.rs:288`).
- One case, so this may be luck. Worth a snippet where a bug has a follow-on effect.

### F4. Findings that humans disputed. (observed, n=3)
- `google-guava-8838` `TypeVisitor.java:57`: a new public API with no `@since` or `@Beta`. **All four reviewers raised it.**
- `tokio-8735` `mod.rs:314`: an invariant that holds only by convention, not by types.
- `tokio-8779` `tests.rs:184`: a test that hard-codes 512 operations.
- Common thread: hardening, API hygiene and test-brittleness remarks, not defects. All are `suggestion` or `warning`.
- **Reviewer agreement is not a quality signal here.** 4 of 4 agreed on a finding a human disputed, so "multiple reviewers raised it" cannot stand in for "worth raising".
- In the validity sample all three were **TRUE**: the claims were accurate about the code. So the maintainers did not dispute an error, they declined to act. A "worth raising?" judge and an "is it true?" judge measure different things.
- Caveat: n=3, and a dispute may be taste.

### F5. Reviewers disagree on severity for the same finding. (observed, correctness unmeasurable)
- `google-guava-8808`: the same CI-matrix finding is `suggestion` (architect) and `warning` (skeptic). The aggregator chose `suggestion`.
- `tokio-8757` `Err(_)` swallowing: three reviewers say `suggestion`, one says `warning`. The aggregator chose `warning`.
- The labels carry no human severity, so **we cannot say who was right.** A severity metric needs new human labels first. Do not build one on the current data.

### F6. A reviewer cites a line that is not in the diff. (observed, deterministic, measured by `anchoring.py`)
- `tokio-8757`: three of four reviewers cite `uring.rs:58`, which is not an added line in the diff. The aggregator moved the finding to `:87`, which is, and which matches the human-confirmed label. `builder.rs:419` is also not an added line, though it could be a context line in a hunk. I did not settle that.
- This does not need an LLM judge. A function in `src/codecheck/core/diffmap.py` (`anchor_finding`) already knows. See the metric ideas below.
- **Measured** (`anchoring.py`, no judge, no tokens, uses the tool's own `anchor_finding`). Each raw reviewer finding is added (an added line), context (an unchanged line in a hunk), relocated (moved up to 3 lines), unanchored (more than 3 lines from any hunk line, so summary only) or file-not-in-diff.
  - **Golden, 6 real cases, 46 findings:** 31 added (67%), 5 context, 4 relocated, 6 unanchored (13%).
  - **Shadow store, 74 runs, 433 findings:** 340 added (78.5%), 30 context (7%), 11 relocated (2.5%), 52 unanchored (12%), 0 file-not-in-diff. These two sets overlap (all 6 golden diffs are among the 74 runs, and the 74 runs cover 66 distinct diffs), so do not add them.
  - **By reviewer (store):** language experts land more often on an added line (lang-python 55/64, lang-go 22/24) than the generalists (pragmatist 64/88, skeptic 85/110, doc-skeptic 19/27). Small counts per reviewer.
  - **The unanchored ones are mostly far away:** of the 52, 29 are more than 50 lines from any changed line, 14 are 11 to 50 away and 9 are 4 to 10 away. Widening the 3-line window would rescue only the 9. Most point at unchanged code the change affects, which the prompts ask the reviewer to name in the message and not anchor on.
  - **Not measured:** whether a cited line that is not an added line is a wrong choice. A finding about a deletion or about unchanged code the change breaks can legitimately sit elsewhere. This counts how many findings cannot be posted inline, and does not judge them.
  - **Decision (2026-11-25, owner): no prompt change to push reviewers toward changed lines. Declined.** The prompts already say changed lines, and no harm from the unanchored findings was measured. See `docs/decisions/0054-reviewers-are-not-told-again-to-anchor-on-changed-lines.md`. Reopen only with a measurement that authors missed or misread unanchored findings.

### F7. The same concern raised several times. (observed, handled)
- 12 of the 18 aggregator drops are `duplicate`. One finding is often raised by 3–4 reviewers, and the aggregator merges them.
- `lang-java` also emitted two findings at the same file and line (`CollectionRemoveIfTester.java:43`, one `correctness` and one `scope`).
- I found no duplicate that survived aggregation in the real cases. This is working, so a regression check is enough.

### Not observed. These are hypotheses, not findings.
- **Findings that are false** (a hallucinated claim or code that does not exist). **Observed on 2026-11-21, so this is no longer a hypothesis.** The blind code check of the 22 decided `not-verified` drops found 9 false claims and 6 true only in part (see the 2026-11-21 update). The 12-finding validity sample below (0 false) was hand-picked, which is why it missed this. All 22 were drops, so this says nothing about false claims among kept findings, and no kept finding has been checked this way yet.
- **A wrong category** (`compat`, `build` and `compile` all name the same Java failure, so the category names are not consistent across reviewers).
- **Severity inflation**, for the reason in F5.
- **Language gaps.** There is no evidence outside Java and Rust.

## Validity sample (12 findings, checked against the code)

Question: is the claim a finding makes true of the code? Not whether it is worth raising or correctly rated.

Method: I chose 12 findings from the golden cases (7 kept, 5 dropped by the aggregator). A subagent checked each against the code at the PR head commit in the local clones (`git show <head_sha>:<file>`, plus the PR diff). It saw only the claim text, location and commit. It did **not** see the human labels or whether the aggregator kept or dropped the finding. The "outcome" column below is my addition after the check. Verdicts: TRUE, PARTLY (a core fact holds, a detail or consequence does not), FALSE, UNVERIFIABLE.

| # | Finding | Outcome | Verdict | Line right? |
|---|---|---|---|---|
| V1 | Guava `TypeVisitor.java:95`, package-private overrides will not compile | kept, human-confirmed | TRUE | yes |
| V2 | Guava `TypeVisitor.java:57`, new public API without `@since` | kept, human-disputed | TRUE | yes |
| V3 | Guava `ci.yml:31`, macOS matrix entry blocks publish and docs jobs | kept, human-confirmed | TRUE | yes |
| V4 | Tokio `mod.rs:314`, driver kept alive after `shutdown2` | kept, human-disputed | TRUE | yes |
| V5 | Tokio `park.rs:323`, failed `try_lock` is silent in release builds | kept, human-confirmed | TRUE | yes |
| V6 | Tokio `park.rs:94`, `expect("driver missing")` | dropped, low-value | TRUE | yes |
| V7 | Tokio `rt_shutdown_fds.rs:59`, loose threshold and untested signal path | dropped, not-verified | PARTLY | near |
| V8 | Tokio `uring.rs:87`, `Err(_)` swallows setup errors | kept, human-confirmed | TRUE | yes |
| V9 | Tokio `tests.rs:158`, test returns early when io_uring is missing | dropped, low-value | TRUE | near |
| V10 | Tokio `tests.rs:184`, hard-coded 512 operations | kept, human-disputed | TRUE | yes |
| V11 | Tokio `uring.rs:279`, doc paragraph deleted | dropped, not-verified | PARTLY | yes |
| V12 | Tokio `rt_shutdown_fds.rs:57`, loose `< ROUNDS` assertion | dropped, low-value | PARTLY | near |

Totals: 9 TRUE, 3 PARTLY, 0 FALSE, 0 UNVERIFIABLE. Cited line: 9 yes, 3 near.

What the PARTLY verdicts mean:
- V7: the PR does add a multi-thread signal-after-drop test (`signal_drop_rt.rs`), so "signal path untested" is wrong. The threshold half is right (`SLACK` is 2, so a leak of 2 or fewer fds passes). The cited line 59 is past the end of the 57-line file. I confirmed these points myself.
- V11: the deleted paragraph was already stale for that function. I did not confirm this independently.
- V12: the "only the waker" example does not hold, since that leak would be caught. The threshold is still loose. I did not confirm this independently.

What it suggests, and what it does not:
- No finding was false. The three human-disputed findings were accurate, which is why F4 reads as "not worth acting on" and not "wrong".
- Both `not-verified` drops (V7, V11) were PARTLY. **Revised 2026-11-21:** that read rested on two items. On all 22 decided `not-verified` drops the blind check found 9 false and 6 partly true claims against 4 true, so those drops are mostly defensible, not only cautious. The `low-value` drops are mixed here (V6 and V9 are TRUE, V12 is PARTLY), but on the owner's drops sheet none of the 18 `low-value` drops was called wrong (16 right, 2 not sure).
- It is a single checker, a single pass and 12 hand-picked findings, so it is a spot check, not a rate. The checker did not compile the Java in V1, check the macOS label in V3, or check kernel behaviour in V10 and per-runtime fd counts in V12. V4's reasoning comes from reading the code, not running it.
- Raw results: `validity_results.json` in the session scratchpad (not committed).

## What each mode suggests

| Mode | Kind of check | Needs an LLM judge? | Data needed |
|---|---|---|---|
| F1 | Was each human-raised concern in the raw pool, and did it survive aggregation? | Only to decide "same concern". Location match first. | Existing `misses` and `dropped` |
| F4 | "Worth raising?" with surrounding code as context | Yes (the existing judge failed here, kappa -0.32) | More `not_expected` labels |
| F5 | Severity agreement | Not yet | New human severity labels |
| F6 | Fraction of cited lines that anchor to the diff | **No**, deterministic | None |
| F3 | Recall on snippets with a known follow-on effect | Yes | New snippets |
| F7 | Duplicates surviving aggregation | No | Existing |
| Unobserved | Factual validity | Yes | A hand-checked sample first |

## Questions for the owner

1. **Scope. Decided: yes.** The target is now reviewers plus aggregator, so F1 is in scope. It can be measured on data we already have.
2. **Validity. First sample done.** 12 findings were checked against the code (see "Validity sample"): 9 TRUE, 3 PARTLY, 0 FALSE. Open: do you want a larger random sample before we build a validity metric, and would you spot-check any of the 12 verdicts yourself? *(2026-11-21: a second, blind code check covered 22 `not-verified` drops and found false claims, see the update at the end. Still no kept finding has been checked this way.)*
3. **F4.** Do these three disputed findings look like taste to you, or like real misses by the panel?
4. **F6.** Is a deterministic line-anchoring check worth building first? It is a good counter-example for learning: not every metric needs a judge.

## Update 2026-11-20: seven more PRs, and calibrating the judge

- **New data.** Seven blind, unposted reviews with tool 1.1.2 (django#22211, #22249, #22251, #22262; express#7467; tokio#8804, #8679): 17 human comments, 13 kept and 11 dropped findings. Kept messages are cut at 300 characters in `handoff.json`, so the judge read the full text from `<run>.raw.json`.
- **F1 is not replicated.** The "same concern?" judge scored all 76 comment-by-finding pairs: 0 same, 6 related, 70 different, highest score 0.5. No dropped finding was the same concern as a human comment. The owner judged one drop (django#22211, "reuse `now`") correct but low value. F1 now rests on the single `tokio-local` case, so it is graded **single**, not observed.
- **What the human comments are.** Mostly design, formatting and test requests ("de-indent this", "implement these on the classes themselves", "cover unbounded sends in Loom"), which fits F2. At least two "human" comments were AI-assisted (an AI disclosure on django#22249, an AI-review format on django#22211), so "human" is leaky.
- **Judge calibration** (16 pairs the owner labelled: 3 same, 4 related, 9 different; P2 is unlabelled). The owner uses "related" for the same area with a different problem. Rubric v1 matches the owner exactly on 8 of 16 and v2 on 7 of 16. With "related" counted as different, both match on 13 of 16. Neither separates the owner's labels well: v1's "related" bucket (11 pairs) holds 2 owner-same, 4 owner-related and 5 owner-different pairs, and across the pairs of pairs with different owner labels v1 orders them correctly 44 times out of 75 (59%) and v2 46 out of 75 (61%), where chance is 50%. v2 is not better: it sent two of the owner's "related" tokio#8804 pairs to "different" (0.3 and 0.2) and called P8, which the owner labels different, "same" (0.9). P3 (owner: same) is "different" under both at 0.3. My own labels were more lenient than the owner's: they differed on 4 of the 8 pairs the owner answered (P1, P4, P8, P9), so earlier agreement figures against my labels should not be used.
- **What the judge is good for.** Under v1, every pair scored 0.2 or lower was "different" for the owner (4 of 4: P6, P7, C1, C2), and 70 of the 76 real pairs scored below 0.3. So it works as a filter that leaves a few pairs for a person to label, not as a score to trust. v2 weakens that: it scored one owner-"related" pair at 0.2. All 6 real pairs that v1 scored 0.4 or higher are now labelled by the owner: 1 same (a kept finding, django#22211), 4 related and 1 different. The two dropped findings among them (django#22211 `d3` and `d8`) are "related", not "same", and the owner called the `d8` drop correct. That settles the F1 check on these 7 PRs.
- **Origin of the hand-built pairs.** P4 to P9, C1 and C2 come from the `tokio-local` case, a local review of commit `53de1f6b` with no PR. At least P7's human comment is from tokio#8735, so that comment and the finding were not made on the same commit.
- **Reviewers skip the findings file.** 4 of the 16 reviewers that found nothing wrote no file, so 25%, not "rarely" (issue #64). An earlier message said "4 of 8", which was wrong. Findings are long and carry the aggregator's reasoning (issue #65), and 5 of 13 kept findings were comment-cap warnings (issue #66).
- **Coverage.** Several reviewers listed unread files on the larger PRs, so some "misses" are files nobody opened.
- Raw material (judgements, labels, side-by-side pages) is outside the repo, in `~/.local/share/code-check-evals/`.

### Step 1: the DeepEval scorecard (deterministic metrics)

- **What it is.** `metrics.py` holds six `BaseMetric`s with no judge, `scorecard.py` turns each hand-off or golden case into an `LLMTestCase` inside an `EvaluationDataset` and runs them, and `_env.py` sets DeepEval's telemetry opt-out before it is imported. Run it with `~/.venvs/codecheck-evals/bin/python -B evals/deepeval/scorecard.py [--source golden] [--by-version] [--misses]`. It makes no model calls, and its anchoring figures match `anchoring.py` exactly (340/433 store, 31/46 golden).
- **Targets.** Only where the owner agreed one: no aggregator process notes, no comment-cap findings, every reviewer writes its file (all at 1.0). Anchoring, message length and accounting only report.
- **Store, 74 runs, tool 0.1.2 to 1.1.2.** Reviewer findings on an added line 340/433 (78.5%). Kept messages without process notes 161/181 (89%). Kept messages within 600 characters 128/181 (71%). Kept findings that are not comment-cap findings 164/181 (91%). Reviewers that wrote a findings file 298/319 (93%). Reviewer findings named by a kept or dropped item 431/433 (99.5%).
- **By version.** Comment-cap findings are absent before 1.0.1, then 1 of 7 (1.0.1), 11 of 58 (1.1.0, 19%) and 5 of 13 (1.1.2, 38%), which fits the docs reviewer arriving. Process notes in kept messages show up in most versions (20 of 181 overall, 11%), so that leak is long-standing. The share of messages within 600 characters stays near 70% in every version with enough runs. Accounting is near 100% throughout. Missing findings files total 21 of 319 (6.6%), across 0.1.4 to 1.1.0. The 1.1.2 figure of 34/34 is not an improvement: those hand-offs were written after the skipping reviewers were re-dispatched.
- **Caveat.** Versions are not a controlled comparison: the PRs differ and most versions have few runs, so read it as a rough history.
- **The aggregator.** Accounting (431/433) was the only aggregator measure at the time of this step. Drop quality is now partly measured, see the 2026-11-21 update below.

## Update 2026-11-21: three review sheets and a blind code check

> **Correction, confirmed by the owner on 2026-11-21:** every label from the kept, drops and `not-verified` sheets below (and every "owner label" in this section) is a verdict from Gemini that the owner pasted into the sheets, not the owner's own judgement. Read "owner label" as "Gemini verdict". The one possible exception is `a1e7f0af1363:d1` on the drops sheet, which came with no pasted note; the owner has not said whether that one is their own. No owner judgement exists yet on these 63 items except through the re-read sheet (`review-sheet-reread.html`, 13 items).

### What was built, and what the draw is
- `review_sheets.py` (no model calls) draws a seeded random sample (seed 20261120) from the whole shadow store, 74 runs, at most 2 items per PR. Sheets: 25 non-duplicate drops, 20 kept findings, and a third sheet of the 18 `not-verified` drops not already on the first. Answer keys and labels are outside the repo in `~/.local/share/code-check-evals/`.
- The pool: 100 non-duplicate drops (69 `low-value`, 24 `not-verified`, 4 `out-of-scope`, 2 `linter`, 1 `unsupported-style`) and 181 kept findings. `duplicate` drops (47) were left out because they are merges, not judgement calls.
- **Correction.** An earlier message said the samples came from 20 and 19 PRs. That counted local runs as PRs. 12 of the 25 drops and 5 of the 20 kept findings come from local reviews of the owner's own branches, which have no PR and no human comments. The tool versions run from 0.1.2 to 1.1.2, so some items judge older prompts.

### Owner labels
- **Kept sheet (20):** 13 worth posting as written, 5 worth raising but unclear or too long, 2 not worth posting. No finding was rejected as wrong. The 4 comment-cap findings were 1 worth, 1 unclear, 2 not worth. The 3 messages with aggregator process notes were 0 worth, 2 unclear, 1 not worth. Messages over 600 characters (6): 3 worth, 3 unclear. All counts are tiny, and the cap and process-note groups were found by simple text matching.
- **Drops sheet (25):** 20 right, 3 wrong, 2 not sure. `low-value`: 16 right, 0 wrong, 2 not sure. `not-verified`: 3 right, 3 wrong.
- **`not-verified` sheet (18):** 14 wrong, 2 right, 2 not sure. Over all 24 `not-verified` drops: 17 wrong, 5 right, 2 not sure.
- **These are Gemini's verdicts, so they are not owner labels (confirmed).** Each note's verdict matched its label. Gemini also made factual mistakes (below). Read against the code check, its verdicts on the 22 `not-verified` drops agree with the checkers on only 4: it called 17 wrong drops, and 7 of those claims are false in the code and only 2 are clearly true. We do not know what code Gemini was shown (its notes quote diff lines), so this is evidence about that judge on this set, not about the owner.

### Blind code check
- **Method.** Four subagents checked the 22 decided `not-verified` drops (the 17 called wrong and the 5 called right, mixed and shuffled). Each saw only the claim, the location and the commit, and read the code with `git show` at the PR head commit in the local clones, read-only, with no labels and no aggregator reason. Verdicts: TRUE, PARTLY, FALSE, UNVERIFIABLE (whether the claim is true of the code, not whether it is worth posting). Results are in `~/.local/share/code-check-evals/code-check/results-1.json` to `results-4.json`.
- **Result.** The 17 called wrong: 2 TRUE, 6 PARTLY, 7 FALSE, 2 UNVERIFIABLE. The 5 called right: 2 TRUE, 2 FALSE, 1 UNVERIFIABLE. Overall 4 TRUE, 6 PARTLY, 9 FALSE, 3 UNVERIFIABLE. Agree: 4 (`25120fefa3e8:d6` and `bb3d3e91b5b0:d0` TRUE and called wrong; `25120fefa3e8:d0` and `7bfd4e47dfd5:d0` FALSE and called right). Conflict: 7 called wrong but FALSE. Open: 9. Compatible: 2 called right but TRUE (accurate and still not worth posting, as in F4).
- **Spot-checks by me.** Two FALSE verdicts were confirmed in the clones: Guava's `assertFailsToDecode` already asserts `canDecode` is false, and Tokio's `BlockingPool::drop` calls `shutdown(None)` and joins every worker, so `drop(rt)` does wait. The pasted note that said otherwise was wrong, and the two Tokio findings (`84048732c6dd:d0`, `:d1`) depend on it.
- **Limits.** One checker per item, one pass, no code run. 5 verdicts leaned partly on the checker's memory of external tools. Three items (`b47813709de3:d0`, `56b299765054:d0`, `7c17cbd6e13b:d0`) are the same GEOS `copyToBuffer` finding, and the repo cannot settle it. Two items cite the wrong line (`503d8c4ce18e:d1`, `84048732c6dd:d1`) and 5 more are within 5 lines.

### Second code-check pass
- **Method.** The same 22 items went to four new checkers in a fresh shuffle, with the same rules (read-only `git show` at the head commit, no code run, no network), told to ignore everything else in the eval folder. Results are in `~/.local/share/code-check-evals/code-check-pass2/`, the comparison in `agreement.json` there.
- **Result.** 21 of 22 verdicts are identical, with no TRUE-versus-FALSE flip. First pass: 4 TRUE, 6 PARTLY, 9 FALSE, 3 UNVERIFIABLE. Second pass: 4 TRUE, 7 PARTLY, 8 FALSE, 3 UNVERIFIABLE. The one difference is `503d8c4ce18e:d1` (the Tokio comment about `Release` and `Acquire`): FALSE, then PARTLY, because the two replacement lines still give a short reason for each ordering. Of the owner-conflict items, 6 of 7 stay FALSE in both passes.
- **Softer judgements.** The two passes disagree on whether the cited line was right for 8 of 22 items (a first message said 7, which was a miscount). 7 of 22 verdicts leaned on the checker's memory in at least one pass, including the three GEOS items, which both passes mark UNVERIFIABLE.
- **What agreement does and does not show.** It shows the verdicts are repeatable. It does not show they are correct: both passes are the same model on the same protocol, and I confirmed only 2 FALSE verdicts by hand.

### Corrections to earlier claims
- "Half of the `not-verified` drops are wrong" and the 77% figure (17 of 22): **not supported.** The labels followed a model's verdicts, and the blind check found only 2 of the 17 clearly TRUE. The plausible range is 2 to 8 of 17 (12% to 47%), and the 6 PARTLY items need the owner's judgement.
- The first drops sheet's 3 wrong drops shrink to 1 (`bb3d3e91b5b0:d0`).
- F1 and the validity sample are revised above (the false-findings bullet and the V7/V11 bullet).

### What follows for the judge (Step 3)
- **Do not score it against the owner labels.** On the drops sheet an always-"right" judge agrees with 20 of 23 (87%), so an 80% bar means nothing there.
- **Use the code-checked verdicts as ground truth,** and set the bar before running. Revised twice before any run. After the second pass: 12 items (4 TRUE, 8 FALSE) and at least 10 of 12 correct. Then a check of the judge's context found that the code deciding 3 of the 8 FALSE items (`2448c039ebb0:d1`, `84048732c6dd:d0`, `84048732c6dd:d1`) is in neither the cited file nor the diff, which made 10 of 12 unreachable, so the owner chose to remove those 3 (2026-11-21). Now: ground truth is 9 items (4 TRUE, 5 FALSE) and the bar is at least 8 of 9 correct (89%), including at least 3 of the 4 TRUE (already implied by 8 of 9); an always-"false" judge scores 5 of 9 (56%). The judge sees the finding, the whole cited file (up to 60,000 characters, else 300 lines each side) and the PR diff. It is built in `code_validity.py` (78 eval tests pass) and has not been run. Run once, do not tune on these 12, and drop the judge if it misses. The owner accepted this bar on 2026-11-21. It is a small set and the two passes are the same model on the same protocol, so it can only show a judge is clearly bad, not that it is good.
- A pattern worth one more sample, not a finding: claims that something is untested (Guava x2, Tokio `8c75582f3970:d3` and glab `4c155c6fa57b:d0` partly) and claims about runtime internals (Tokio x2).

### Step 3 result: the code-validity judge missed its bar (run once, 2026-11-21)
- **Run.** `code_validity.py --yes`, claude-opus-5-5, 9 items, about 78.5k input tokens, one run, nothing tuned. Output: `~/.local/share/code-check-evals/judge-runs/code_validity_run1.json`. This replaces "built, not run" in the section above.
- **Result: 5 of 9 correct, bar was 8 of 9. It fails.** It found 2 of the 4 TRUE items and all 3 FALSE items it committed to were right, but 4 items came back UNCERTAIN, which counts as wrong. An always-FALSE judge also scores 5 of 9, so this judge added nothing over a constant.
- **Wrong items, all UNCERTAIN, and why.** `25120fefa3e8:d0` (truth FALSE, 0.7): the mechanism held, but the cited line was off (338 vs 332) and the repost claim needed caller code outside its context. `25120fefa3e8:d6` (TRUE, 0.5): could not verify "never read anywhere" because a file and 31 diff sections were not in context. `47c1559a32ce:d1` (TRUE, 0.7): the structural claims held, but the router library code the risk depends on was not in context. `7bfd4e47dfd5:d0` (FALSE, 0.5): it saw that 0.7s is itself a sub-second gap, but said the stored timestamp's fractional part was not settled by its context.
- **What this does and does not show.** It never gave a confident wrong TRUE or FALSE; every miss was a refusal to commit. Three of the four turn on code it was not shown (the fourth, `7bfd4e47dfd5:d0`, turns on a fact its context left open), which is the same limit that made us remove 3 other items before the run. We cannot say whether a judge with repo access would pass; this test was not built to answer that. Per the pre-registered rule the judge is dropped as a metric. `code_validity.py` stays in the folder as a record, not as a gate or scorecard input. Do not re-run it to chase the bar; a second run is exploratory only.
- **Consequence.** For truth about a finding, the code-grounded checker (a subagent reading the repo with `git show` at the head commit) remains the only method that has worked, and it is a manual spot check, not a metric.

### Hand-off comparison on the ten 2026-11-26 PR runs (plugin 1.7.0, blind, `--full`)
- **Setup.** 10 PRs (angular, Avalonia, prometheus, opentelemetry-collector, typescript-eslint), 68 reviewer findings, 25 kept, 30 dropped, and 25 human inline comments already on the PRs. Numbers are from the hand-offs; the comparison was made before anyone saw our review.
- **Location matching undercounts.** Same file within 3 lines matched 3 of 25 human comments. Reading the rest by hand, about five more name the same concern from other lines (a test file against the code it tests, a description against its fix). `related` (1.7.1) lists same-file kept or dropped findings sharing 3 distinctive words with an unmatched comment: it gave candidates for 7 of 22, of which about five were the same concern. It is a pointer, not recall, and it misses a comment whose words differ from ours (an angular maintainer's "Do we still need the bypass here?").
- **Anchor errors, measured.** A reviewer's cited line was compared with the line of the kept finding it reached (paired by shared words): 18 of 49 were more than 3 lines away, 12 of them 70 or more (skeptic 7 of 14, pragmatist 5 of 7; the other reviewers 0 to 2). Some of that is a merged finding spanning two sites, not a mistake. A deterministic check, "no identifier of the message appears within 10 lines of the cited line at the reviewed head", caught 8 of 12 clear errors (more than 30 lines) but flagged 7 of 31 correct anchors, so about half of its flags were wrong. It is not safe to move a finding on, and was not added. A cheaper fix that needs a prompt change is a short quote of the cited line in each finding, checked against the file; not tried.
- **Dropped findings a person also raised** (`dropped_raised.py`, over all 109 hand-offs in the store): 2 of 2 `out-of-scope` drops have a related human comment (angular #71754 compat subscriptions, Avalonia #22236 BeginMoveDrag/BeginResizeDrag); 0 of the other 210 drops (85 low-value, 84 duplicate, 34 not-verified, 4 linter, 3 unsupported-style). Two is too few to change the aggregator's rule that a pre-existing issue is out of scope; keep counting on later runs.
- **Test files.** 6 of the 25 human comments are on test files, and several reviewers reported the test patches as unread.

### Open
- The owner re-reads the 7 conflicts with the checkers' evidence, and the 6 PARTLY items.
- No kept finding has been checked against the code. The kept sheet only says the owner would post 13 of 20.
