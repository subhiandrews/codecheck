# Synthetic cases: learn from a private or non-permissive PR without copying it

Status: brief and check built (this change); the authoring skill is not. Complements the license gate in #50 (`shadow promote --finalize` refuses a diff whose repository license is not allowed).

## Intent

A real PR from a company's private repository, or from a copyleft one, cannot go into the public golden set. But what the reviewer got wrong on it (a missed null check, a false alarm about a pattern) is a general lesson. A **synthetic case** keeps the lesson and drops the code: a new, small, invented snippet that has the same kind of defect.

## Where it lives

Not in `tests/golden` (replay cases hold recorded model output for a real diff). Synthetic cases are **live snippets** (`tests/snippets/<name>/case.json`, run by `eval --live`): a hand-made `base` and `change`, the findings expected, and what must not be reported. That format already exists, holds no recorded output, and is meant to be invented code. Replay stays for real, licensed diffs.

## Flow

1. **Brief** (`shadow promote --brief <case.json> --out brief.json`, deterministic). The input is a confirmed or draft case, which stays private. The brief holds only what is general: the language (from the labelled files' extensions), and for each labelled finding its label (`expected` or `not_expected`), category and severity, with an empty `pattern` for a person to fill with one sentence ("a lookup result is used without checking it exists"). It holds no file names, lines, messages, code, repository, PR or ids.
2. **Author** (a model; a project skill that runs it is a follow-up, today a person does this step by hand). Given only the brief, a subagent writes `case.json` for an invented project. It never reads the source case or the shadow store, and its tools are limited to writing under a scratch directory. Expected findings become `expected`, false alarms become `must_not_report`.
3. **Check** (`shadow promote --check-synthetic <dir> --source <case.json> [--brief brief.json]`, deterministic). Validates the snippet with the live-eval schema, then:
   - **Leak check.** Collects what is distinctive in the source (in the diff: identifiers with an underscore or a case change, plain words of 12+ characters, paths, host names; in reviewer text, finding messages and human comments: compound identifiers, quoted code, paths and host names, but not plain long words; the repository and owner names; any whole diff line of 30+ characters after whitespace is collapsed) and refuses the snippet if any of it appears anywhere in the snippet, name included, scanning base and change files separately. A host needs a real top-level domain (so `os.path.join` is not one) and a path needs two slashes or a file extension (so `read/write` is not one). It prints the offending terms, which is safe because the check runs where the source already is. A person decides which shared terms are generic API names (`try_lock`, `notify_all`): the brief carries an `allow` list for them, `--allow <term>` adds more, and the output prints an `allow_hint` to paste after reading it.
   - **Fidelity to the brief.** The snippet is in the brief's language (when it names one), has as many `expected` entries, with the same severities, as the brief's `expected` findings, lists each brief category among its expected categories, and has at least as many `must_not_report` entries as the brief's `not_expected` findings.
   - **Uncalibrated.** `calibrated` must be absent or false. A person runs `eval --live --only <name>` twice, as the README already requires, and then sets it.
4. A person reads the snippet, runs it live, and adds it to `tests/snippets` (public) or to a private snippets directory. Nothing is written to the public repo by any step above.

## Why this is safe, and where it is not

- The LLM never sees the source, so it cannot copy it. The leak check backs that up deterministically and does not depend on the model behaving.
- The leak check finds shared names and copied lines. It cannot judge whether an invented snippet is a close paraphrase of the original's *structure*. That stays a human review, and the brief's one-sentence pattern is deliberately about the kind of mistake, not the code.
- Whether pattern-level abstraction from a company's code is acceptable is the company's policy decision, and this is not legal advice.

## Tried and rejected: exempting terms that other public material also uses

The obvious way to stop standard-library names from blocking snippets is to treat a term as generic when it also appears in other public cases and snippets in the repository. Measured on the real corpus (11 other sources, 4 of them from the same project as the test case), it does not work: exempting a term seen in one other source exempts 122 of the case's 138 terms, project names such as `park_condvar` included, because sibling cases from the same project share them; requiring two sources exempts 35 and still leaves `try_lock`, `debug_assert` and `notify_all` blocking. Revisit when the public corpus spans many projects.

## Non-goals

- Generating snippets unattended in `handoff-round`. The authoring skill is run by a person for now.
- Changing the replay golden set, the license gate or `eval --live`.
- Scoring how realistic a synthetic snippet is. It tests the reviewer on the defect, not on realism.

## Testing

Brief: holds no source file name, line, message, repository or id (asserted against a case full of them); skips unlabelled findings; picks the language. Check: accepts a clean snippet; refuses a shared distinctive identifier, a shared path or host, a copied long line, and a shared term in the snippet's name; `--allow` clears one term; refuses a count or severity mismatch with the brief; refuses `calibrated: true`; refuses an invalid snippet.
