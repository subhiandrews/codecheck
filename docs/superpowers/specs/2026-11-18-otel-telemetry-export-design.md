# Team telemetry: OpenTelemetry export of hand-offs

## Intent

The hand-off loop (`2026-11-13-handoff-and-golden-eval-design.md`) is built for one person's laptop. A team needs the failures, findings and human feedback from everyone's runs in one place, linked to the PR and the commit that was reviewed. This adds an opt-in export of each hand-off to an OpenTelemetry collector.

## Amendment to the hand-off spec

The hand-off spec's anti-goal "Nothing leaves the machine" holds **unless `telemetry.enabled` is true**. With it false (the default) nothing changes. With it true, only what this document lists leaves the machine, and free text only with `telemetry.content`.

## Goals

- Local mode is untouched: the hand-off folder is still written and still the source of truth, `handoff-queue` still reads it.
- One trace per run with the PR URL, the commit reviewed (`head_sha`), the decision, the build, tool-call failures per agent, findings, and later human feedback.
- Standard OpenTelemetry configuration (`OTEL_EXPORTER_OTLP_*`), so a team points it at a collector it already runs.
- Export never changes a command's result, and a dead collector never blocks a review for more than `telemetry.timeout_seconds`.

## Non-goals

- No shared work queue. OTLP has no claim primitive; hand-off rounds stay per maintainer.
- No transcripts and no finding messages leave. Hand-offs do not hold finding messages.
- No dependency on the OpenTelemetry SDK (it is built for live instrumentation; these spans are built afterwards from known data).
- No live streaming. Claude Code's own telemetry covers that; the two join on `claude.session.id`.

## Design

- `telemetry/otlp.py`: resolves the endpoint and headers, and POSTs OTLP/HTTP JSON with the standard library. Headers (the token) come only from `OTEL_EXPORTER_OTLP_(TRACES_)HEADERS`; `[telemetry]` is rejected in a repository's `.code-check.toml`. `http://` only for localhost, no credentials in the URL, redirects are not followed, gRPC protocol settings are refused unless `telemetry.endpoint` is given.
- `telemetry/build.py`: pure payload builder. Trace and span ids are hashes of the run id, so a resend is idempotent. Spans: `code_check.run`, one `code_check.agent` per transcript, and `code_check.feedback` (its id includes its content, since traces cannot be edited).
- `telemetry/push.py`: reads `handoff.json` and the shadow records, builds the payload, skips an unchanged digest, sends, and records the result in `<run>.handoff/telemetry.json` (the outbox). `push_runs` stops at the first failed send.
- `telemetry push --run|--all [--dry-run] [--force]`. `handoff` and `shadow collect` push automatically when enabled, inside a catch-all, and add a `telemetry` key to their output only then.

## Token

- **A token is mandatory for a remote collector.** `resolve_target` refuses an endpoint whose host is not loopback unless the auth header (`telemetry.auth_header`, default `Authorization`) carries a non-empty credential; `Bearer` or `Basic` with nothing after it counts as empty. Loopback is exempt only so a local test collector works.
- The token comes from `OTEL_EXPORTER_OTLP_(TRACES_)HEADERS` or `telemetry.token_file` (a regular file readable only by its owner, one non-empty line). The environment header wins. The token is never a config value, is not in `repr`, and is not in errors. `telemetry.*` stays rejected in a repository's `.code-check.toml`.

## Verified and not verified

- Verified: a transcript file's name equals the `sessionId` inside it (15 of 15 sampled sessions), and a subagent's entries carry its parent's `sessionId`.
- Verified (2026-11-19): the real OpenTelemetry Collector v0.165.0 (`otel/opentelemetry-collector`, OTLP/HTTP receiver, debug exporter) accepted the payload from `telemetry push` and decoded the hex trace and span ids, parent links, typed attributes, events and resource attributes (`team`, `service.name`). The Authorization header was sent but the collector under test has no auth, so the mandatory-token rule and header sending are covered only by unit tests and the local-server tests.
- Not verified: that Claude Code's native OTel `session.id` attribute equals that `sessionId`; the `grafana/otel-lgtm` image the README suggests for a quick test (the plain collector was used instead).
- Known gap: subagent transcripts do not name their agent type (the first message holds `RUN_DIR` and the task), so agent spans are per transcript file, not per panel member. A subagent's `Write` of `findings/<member>.json` would identify it; not built.
