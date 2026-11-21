# Failure modes in code-check reviews

What the metrics in this folder are for, how well each failure mode is supported, and what did not work. Written in November 2026 from stored review runs, with one maintainer reading the results.

## How to read this

- **Evidence grade.** *observed*: several examples. *single*: one example, not replicated. *suspected*: no example yet.
- Every count is small. Read it as "seen at least once", not as a rate.
- Labels are weak truth. A finding a person acted on is `expected`; one a person disputed is `not_expected`, which can be a preference and not a mistake (`docs/Evals.md`). Labels from pull requests in languages or repositories the labeller does not read are weaker still.
- Figures marked *golden* come from the committed `tests/golden` cases and can be reproduced with `scorecard.py --source golden`. Figures marked *maintainer store* come from a local shadow store of 74 runs (tool 0.1.2 to 1.1.2) that other contributors do not have.

## Modes

**F1. The aggregator drops a finding a human later raises. (single)** In one local case (`tokio-local`), 2 of 4 missed human comments matched findings the aggregator dropped as `low-value`. Seven more blind reviews (17 human comments, 11 dropped findings) found no dropped finding that was the same concern as a human comment, so it is not replicated. Location matching alone is too coarse to measure it; deciding "same concern" needs a judge or a person.

**F2. Reviewers lack project knowledge that maintainers apply. (observed)** Most missed human comments are design steering ("keep the old signature", "de-indent this"). That cannot be fixed from the diff and is a job for a repository's `CODE_CHECK.md`, not a metric.

**F3. The first-order problem is found, the second-order consequence is not. (single)** In one case the panel caught an overstated panic-safety doc but not the use after free that follows from it.

**F4. Findings that humans disputed. (observed, n=3)** All three are hardening or API-hygiene remarks. A check against the code found all three accurate, so the maintainers declined to act on a true claim. Four of four reviewers agreed on one of them, so reviewer agreement is not a quality signal, and "is it true?" and "is it worth raising?" are different questions.

**F5. Reviewers disagree on severity. (observed, unmeasurable)** The labels carry no human severity, so nobody can say who was right.

**F6. A reviewer cites a line that is not in the diff. (observed, deterministic)** `anchoring.py` measures it with the tool's own `anchor_finding`. *Golden*, 46 findings: 31 on an added line (67%), 5 context, 4 relocated, 6 unanchored (13%). *Maintainer store*, 433 findings: 340 added (78.5%), 52 unanchored (12%). Of those 52, 29 are more than 50 lines from any changed line, so widening the 3-line window would rescue only 9. This counts findings that cannot be posted inline; it does not say they are wrong, because a finding about a deletion or about unchanged code the change breaks can legitimately sit elsewhere.

**F7. The same concern raised several times. (observed, handled)** Twelve of 18 aggregator drops in the golden cases are `duplicate`. No duplicate survived aggregation, so a regression check is enough.

**Format problems (observed, *maintainer store*, filed as issues #64, #65 and #66).**
- Reviewers that find nothing sometimes skip the findings file: 4 of 16 zero-finding reviewers in one batch, 21 of 319 reviewers overall, and a single retry fixed 4 of 4.
- Kept messages are long and sometimes carry the aggregator's reasoning: median 517 characters against a human median of 165, and 20 of 181 kept messages with process notes such as "Downgraded from...".
- The built-in comment cap produced warnings on repositories that never asked for one: 5 of 13 kept findings in the last batch, and 38% of kept findings in tool 1.1.2.

## What did not work

**Validity needs the code, and a hand-picked sample hides the problem.** A hand-picked sample of 12 findings checked against the code found 9 true, 3 partly true, 0 false. A blind check of 22 `not-verified` drops, with four checkers reading the repository at the PR head commit, found 4 true, 6 partly true, 9 false and 3 unverifiable. A second independent pass agreed on 21 of 22, but both passes are the same model on the same protocol, so agreement shows repeatability, not correctness. Verdicts pasted in from another LLM disagreed with the code check on most items, so they were not used as labels.

**The "same concern?" judge is a filter, not a score.** On 16 pairs a person labelled, rubric v1 matched exactly on 8 and ordered pairs correctly 59% of the time, where chance is 50%. Every pair it scored 0.2 or lower was "different" (4 of 4), so it is usable only to leave a few pairs for a person to label.

**The code-validity judge missed its bar.** With a bar fixed before the run (8 of 9 on items two check passes agreed on), claude-opus-5-5 scored 5 of 9, no better than an always-FALSE judge. Every miss was UNCERTAIN, and three of the four turned on code outside the judge's context (the cited file plus the diff). It was dropped and is not included here. A judge with repository access might do better; this test did not try it.

**Do not score a judge against labels it can match by always agreeing.** On one sheet an always-"right" judge agreed with 87% of the labels, so an 80% bar there would mean nothing.

## Not observed

- A wrong category (`compat`, `build` and `compile` all name the same Java failure, so category names are not consistent across reviewers).
- Severity inflation (see F5).
- Language gaps: there is no evidence outside Java and Rust.
- Whether kept findings are true. No kept finding has been checked against the code this way yet.
