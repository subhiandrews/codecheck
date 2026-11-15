# LLM-as-judge evals (phases 0 to 2: judge core and calibration)

## Intent

The live snippet eval matches a review's findings against expected ones by file, category name and line (`evals/match.py`). That is deterministic and cheap, but it counts a finding as a miss when the right problem is reported under another category or on a neighbouring line, and it cannot express a concern about a module or a pattern at all. A judge model can answer "is this the same issue?" and "is this finding worth raising?" in plain language.

A judge is itself a model, so it must be measured before it is trusted. This first step builds the judge and the measurement, and nothing that depends on the judge.

## Goals

- `evals/judge.py`: ask a model two kinds of yes/no questions and parse the answer strictly.
  - **pair**: does this finding report the same issue as this expected concern?
  - **finding**: given the diff, is this finding worth raising?
- `judge_case`: pair a list of expected concerns (and forbidden ones) with a review's findings; report recall, matched and unmatched items, and forbidden hits.
- `eval --calibrate`: run the finding question over every labelled finding in `tests/golden`, compare with the human label (`expected` is yes, `not_expected` is no), and report agreement and Cohen's kappa. Also ask the pair question for each human comment the review missed against that case's findings, as negative controls, and report the disagreements for a person to read.
- `models.judge` config key, default `opus`.

## Non-goals

- The judge never runs in CI, in `scripts/gates.sh`, in `handoff-queue check` or as a merge condition. It is advisory evidence. The merge guard stays deterministic.
- No architect fixtures, no A/B comparison, no rubric grading, no real-PR checkouts. Those are later phases and wait for the calibration numbers.
- No retries or majority votes yet; one call per question, with the cost reported.

## Decisions

- **Model.** `models.judge`, default `opus`: a stronger model than the reviewers (`sonnet`). A name starting with `-` is refused.
- **Call.** `claude -p --model <judge> --output-format json --permission-mode dontAsk --permission-prompts none`, prompt on stdin (a diff can exceed the per-argument limit), `--tools ""` so the judge has no tools, a short replacement `--system-prompt`, `--strict-mcp-config`, `--disable-slash-commands`, `--no-session-persistence`, and an empty throwaway working directory. The first real run without these cost about $0.30 a call because every call carried the full Claude Code context. `--bare` would be smaller still but needs an API key, so it is no use on a subscription. Not verified on a runner.
- **Untrusted data.** Findings, comments and diffs are third-party text. The prompt puts instructions first, then each piece of data between fences that carry a random per-call token, and states that nothing inside a fence is an instruction.
- **Strict output.** The reply must be one JSON object whose answer field is a real boolean and whose `reason` is a string (kept, cut to 300 characters; the first real run showed models write longer reasons and rejecting them threw away good answers). Anything else (prose around it, a string `"true"`, a missing field); one ``` code fence around the object is tolerated is an error, never a yes. An errored pair counts as "no match". An errored calibration item counts as a disagreement.
- **Context size.** A diff is cut to 60,000 characters with a visible note, so a huge PR cannot make one call unaffordable.
- **Calibration bar.** Agreement of at least 0.8 on the finding question, with no errored items, passes. Kappa is reported but is not the bar because the golden set is small and mostly positive.
- **Negative controls.** A human comment that the pipeline did not match to a finding is, by construction, probably not covered by any finding. They show where the judge says "same issue" anyway; the heuristic that produced them can be wrong, so they are listed for reading and do not decide `passed`.
- **Flag.** One mode, `eval --calibrate` (with `--cases` and `--only <case_id>`), in the same mutually exclusive group as `--replay` and `--live`.

## Result shape

`eval --calibrate` prints one JSON document: `passed`, `agreement` (`n`, `agree`, `rate`, `kappa`, `errors`), `controls` (`n`, `said_same`), `cost_usd`, and per case the findings with the human label, the judge's answer and reason, plus the control pairs.

## Risks

- A small, mostly positive golden set gives a weak estimate; the report says how many items it rests on.
- The judge's answer depends on prompt wording; the prompts live in one module and the calibration is the test of a change to them.
- Unverified: `claude -p` reading its prompt from stdin with no tools allowed, on a developer machine and on a runner.

## First calibration result (2026-11-15)

Run with the final code against the seven golden cases and `models.judge = opus`: **14 labelled findings judged, 7 agreed, rate 0.5, kappa -0.32, so it fails the 0.8 bar.** Three more findings (all of `seed-pipeline`, which is hand-built and has no message text) were skipped. Nine negative controls: the judge said "same issue" for one of them. The run cost about $1.04 for 23 calls.

What the disagreements show:

- The judge sees only the diff, so it under-credits findings that rest on surrounding code (the Guava CI finding about downstream jobs). Giving it the repository would be the fix, and that makes each call much larger.
- It is stricter than the humans on four findings the humans acted on, usually because the diff already documents the point. It is more lenient than the humans on three they disputed.
- A human label here means "a person resolved or agreed with it", which reflects what maintainers chose to do and not only whether the finding was correct. So disagreement is not all judge error.

**Conclusion: "is this finding worth raising?" with a diff-only prompt is not a usable stand-in for human judgement, and nothing should depend on it.** Do not build the A/B comparison on it. The pair question ("same issue as this concern?") has explicit expected concerns as ground truth and is the better base; it needs fixtures with such concerns (phase 3), not these labels.

Two earlier problems this run exposed and fixed: a 300-character cap on `reason` rejected 11 of 26 answers, and calls through the default claude context cost about $0.30 each (about $0.02 now, see the Call decision).
