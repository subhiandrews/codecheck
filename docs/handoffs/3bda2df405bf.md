# Hand-off analysis: run 3bda2df405bf

Run: `review-pr` (pr mode, blind) on google/guava#8808, head `9616e3c`, tool version 0.1.2. Decision: COMMENT (`auto_approve` is off).

Outcome: every item below is `note`. Nothing is concrete and reproducible enough in a test to act on, so this round changes only this file.

## Tooling gaps

- **Tool errors in the session** (`signals.tool_errors`, 3 of 42 calls).
  - Evidence: one `Bash` error from the orchestrator that listed plugin cache directories, and two `Read` errors from `agent-a13055a41e78afe5d` ("File does not exist ... cwd is /Users/subhiandrews/opensource/guava").
  - Reading: the reviewer tried paths that do not exist in the checkout. Retries are 0 and permission denials are 0. That is ordinary exploration, not a capture defect.
  - Decision: `note`.
- **`ignored_guidance`** has one entry (`258:- \`handoff.json\`: the run ...`), a line from code-check's own documentation, not from guava.
  - Evidence: `ignored_guidance[0]`. The entry is truncated at "(up to".
  - Reading: it looks like a capture of a tool-session artefact rather than a guava guidance line. The hand-off does not say which source file it came from, so a test would have to invent that. Not reproducible from this data.
  - Decision: `note`. If it recurs with a source file named, it is a candidate for a capture fix.

## Noise

- Finding `02e03a47b37d` (suggestion, category `ci`, `.github/workflows/ci.yml:31`) is the only finding. No human commented, so nothing was disputed (`comparison.human_comments` is empty).
- Decision: `note`. There is no evidence that it was noise.

## Misses

- `comparison.misses` is empty and `human_comments` is empty. Nothing to learn a bug class from.
- `unraised_findings` is `["02e03a47b37d"]`. With no human comments this says nothing about the finding's quality.
- Decision: `note`.

## Prompt candidates

- None. Reviewer status is clean: architect 1 valid, pragmatist 0 valid, skeptic 1 valid, no ingest errors, `dropped` is empty.
- Decision: `note`.

## Golden case

Not promoted. The run has no human comments, so its one finding cannot be labelled `expected` or `not_expected`.
