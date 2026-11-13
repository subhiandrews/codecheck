# Security reviewer and CODE_CHECK.md: design

Status: draft for approval. Size: small change (two additions along existing seams).

## Intent

1. **Security reviewer.** An opt-in reviewer that finds security problems that are hard to catch with deterministic tools: leaked or hard-coded secrets (keys, passwords, tokens), PII, and vulnerabilities (vulnerable code patterns, and risky or known-vulnerable dependency use it can recognise). It warns; it never blocks by itself. It uses Opus.
2. **CODE_CHECK.md.** A repository can ship a `CODE_CHECK.md` with project-specific review guidance, which can be anything (conventions, things to ignore, extra checks). When present, every code-check skill reads it and includes it in the prompt. When absent, nothing changes.

## Goals

- The security reviewer runs only when asked: a `security` argument on a skill, `--security` on `context`, or `review.security = true` in config (this is how unattended `loop` and `run-pr` runs enable it).
- It is one panel member (`security`), dispatched like every other member, and its findings use the existing finding schema, anchoring, aggregation and posting.
- `CODE_CHECK.md` reaches all four skills (`review-local`, `review-pr`, `address-comments`, `babysit-pr`), and a missing file is silently fine.
- In this repository, `CODE_CHECK.md` says: look for duplication across skills.

## Non-goals and anti-goals

- Not a replacement for deterministic scanners (gitleaks, SAST, dependency audit). It does not reimplement their regexes; it adds judgement on top.
- No network calls, no CVE database lookups. Vulnerable-dependency warnings come from the model's own knowledge and are worded as "verify", not as facts.
- No new posting path and no auto-fix.
- Anti-goal: a review comment that reprints a secret. The security reviewer must never echo a secret or PII value in `message` or `suggestion`; it names the file, line and kind ("AWS access key", "email address") only. Review comments are public.
- Anti-goal: a PR author using `CODE_CHECK.md` to silence the reviewers.

## Constraints

- The panel is chosen in code (`select_panel`, `commands/context.py`) and recorded in `context.json`; the skills dispatch `panel.members`. Reviewers are generated from `agentgen.py` and drift-tested: change the generator and regenerate `agents/`.
- Reviewers receive only `RUN_DIR=...`, so anything they need must be in the run directory.
- `tests/test_plugin_static.py` checks every flag and subcommand a skill uses against the real parser, so a new flag must exist in the CLI before a skill uses it.
- `tests/test_readme.py` requires every config key to appear in the README.
- A repo's `.code-check.toml` cannot set security-sensitive keys (`state_dir`, `run_pr.*`, `shadow.*`, `review.auto_approve`, `loop.review_forks`).

## Approaches considered

1. **Dedicated `security` agent plus `--security` on `context`. (Chosen.)** The panel gains a member only when requested. Nothing else changes for other reviewers.
2. Fold security into the skeptic's prompt. Rejected: the skeptic runs on every review at Sonnet, so this raises cost for everyone and dilutes the focus.
3. A separate `security-review` skill. Rejected: it duplicates the review plumbing (precheck, ingest, aggregate, post), which is the kind of duplication this repo's own `CODE_CHECK.md` asks reviewers to flag.

## Design

### Security reviewer

- **Model:** new `models.security`, default `"opus"` (a user can set `"sonnet"` to trade recall for cost). Validated like the other model names. One agent per chunk, not per language.
- **Agent:** `agents/security.md`, generated from `agentgen.py` with the usual tools (`Read, Grep, Glob, Write`) and the shared COMMON protocol. It owns all changed files. Focus: committed secrets and credentials (including ones that look unfamiliar), PII in code, logs, fixtures and sample data, injection, authn/authz and access-control mistakes, unsafe deserialization, SSRF and path traversal, weak crypto and randomness, insecure defaults, secrets in CI or config files, and risky or known-vulnerable dependency additions. Findings use category `security`. Vulnerabilities map to `warning` (or `critical` when exploitable as written); secrets and PII committed in the change are `critical`. Messages must not contain secret or PII values.
- **Panel:** `Panel` gains an optional `security` member (listed after the core reviewers). `select_panel(..., security=False)`; `context` passes `args.security or cfg.review.security`. The model map in `context.json` includes it.
- **Config:** `review.security: bool = False`, documented in the README.
- **Skills:** `review-local` and `review-pr` accept a `security` argument, document it in `argument-hint`, and pass `--security` to `context`. `loop` and `run-pr` need nothing new because they read `review.security`; `run-pr` also accepts `--security`.
- **Not in `address-comments` or `babysit-pr`.** They don't run the panel.

### CODE_CHECK.md

- **Where it is read from.** `context` reads `CODE_CHECK.md` at the repo root. For PR runs it reads the file from the **base branch** (`git show <base_ref>:CODE_CHECK.md`), not the PR head, so a PR author cannot change the rules their own PR is reviewed under. For local runs it reads the working tree file, since it is the user's own branch.
- **Cap:** 16,000 characters; longer files are truncated with a visible marker.
- **Delivery to reviewers:** copied to `RUN_DIR/CODE_CHECK.md`; `context.json` records `guidance: {"source": "base"|"worktree", "truncated": bool}` or `null`. COMMON gains a first step: if `RUN_DIR/CODE_CHECK.md` exists, read it as project guidance. The aggregator reads it too. The agents are told it is guidance for what to look at and what to ignore, and cannot change their output format, severity scale or the security rules, and that instructions in it which try to would be ignored and reported.
- **Delivery to `address-comments` and `babysit-pr`:** the `workspace` result gains `guidance` (same source rules and cap) and the skills are told to read it before making changes.
- **Missing or unreadable file:** no error, no warning, behaviour as today.
- **This repo:** add `CODE_CHECK.md` at the root with two instructions:
  1. Look for duplication across `skills/*/SKILL.md` (repeated step sequences, near-identical prose, copied rules) and suggest extracting what is shared.
  2. Review changed or added tests to make sure they are valid: each test must be able to fail when the behaviour it covers breaks. Flag tautologies such as `assert True`, `x == x`, asserting a value against the same expression or constant the code just returned, tests that assert only that a mock was called with what the test itself set up, assertions that cannot fail (an `or` of a true branch, a bare truthiness check on a non-empty container), and tests with no assertion at all. Also flag tests that mutate or stub away the very code under test.

## Tests

- Panel includes `security` only when requested; `context` records the model; `gen-agents` drift test passes with the new agent; config validation for `models.security` and `review.security`.
- `context` copies `CODE_CHECK.md` (base branch for PR mode, worktree for local), truncates, and records `guidance: null` when absent; a malicious head-branch `CODE_CHECK.md` is ignored in PR mode.
- `workspace` returns `guidance`; static check passes for the new flag and argument hints.
- README and CLAUDE.md updated (config keys, the `security` argument, the `CODE_CHECK.md` convention).

## Left to the builder

- Exact prompt wording for the security agent and the guidance paragraph in COMMON.
- Where the 16,000-character cap lives (constant versus config).
- How `security` is spelled in skill text so the argument parses cleanly alongside `--force` and a PR number.
