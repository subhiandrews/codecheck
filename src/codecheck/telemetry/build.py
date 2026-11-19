"""Turn a hand-off (and the human feedback on it) into an OTLP/HTTP JSON trace. Pure: no I/O, no clock.

One trace per run. Ids are derived from the run id, so sending a run again is idempotent. The root span
`code_check.run` carries the run (PR URL, commit reviewed, build, decision, session id); each transcript is a child
span `code_check.agent`; findings, reviewers and failed tool calls are span events. Human feedback is a separate
`code_check.feedback` span whose id includes its content, so comments that arrive later add a span instead of
needing an edit (traces cannot be edited).

Free text (tool error messages, human comment bodies and authors, dropped-finding reasons) is exported only when
`content` is true. Hand-offs hold no finding messages, so none can leave. Agent spans reuse the run's time window:
the signals carry no per-transcript timestamps.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence

from codecheck import __version__

STR_CAP = 1000
MAX_EVENTS = 500  # per span; the span says when it was cut
_SPAN_INTERNAL = 1


def _hex(seed: str, n: int) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:n]


def trace_id(run_id: str) -> str:
    return _hex(f"code-check:trace:{run_id}", 32)


def _value(v) -> dict | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v[:STR_CAP]}
    return {"stringValue": json.dumps(v, sort_keys=True, ensure_ascii=False)[:STR_CAP]}


def attrs(items: Mapping[str, object]) -> list[dict]:
    return [{"key": k, "value": val} for k, v in items.items() if (val := _value(v)) is not None]


def _nanos(epoch: float) -> str:
    return str(int(epoch * 1_000_000_000))


class _Span:
    def __init__(self, run_id: str, key: str, name: str, parent: str | None, start: float, end: float, attributes: Mapping):
        self.id = _hex(f"code-check:span:{run_id}:{key}", 16)
        self.name, self.parent, self.start, self.end = name, parent, start, end
        self.attributes = dict(attributes)
        self.events: list[dict] = []
        self.run_id = run_id

    def event(self, name: str, attributes: Mapping, at: float | None = None) -> None:
        if len(self.events) < MAX_EVENTS:
            self.events.append({"timeUnixNano": _nanos(self.end if at is None else at), "name": name, "attributes": attrs(attributes)})
        else:
            self.attributes["code_check.events_truncated"] = True

    def to_otlp(self) -> dict:
        span = {
            "traceId": trace_id(self.run_id), "spanId": self.id, "name": self.name, "kind": _SPAN_INTERNAL,
            "startTimeUnixNano": _nanos(self.start), "endTimeUnixNano": _nanos(self.end),
            "attributes": attrs(self.attributes), "events": self.events, "status": {"code": 0},
        }
        if self.parent:
            span["parentSpanId"] = self.parent
        return span


def _session_id(handoff: Mapping) -> str | None:
    files = handoff.get("transcript_files")
    main = files[0] if isinstance(files, list) and files and isinstance(files[0], str) else None
    return main.removesuffix(".jsonl") if main and not main.startswith("agent-") else None


def _text(v, content: bool):
    return v if content and isinstance(v, str) else None


def build_payload(
    handoff: Mapping,
    *,
    started: float,
    ended: float,
    resource: Mapping[str, str],
    content: bool,
    feedback: Mapping | None = None,
) -> dict:
    """`feedback`, when there is some: {"at": epoch, "comparison": build_comparison(...), "labels": {finding_id: label},
    "bodies": {thread_id: body}}."""
    run_id = handoff["run_id"]
    started = min(started, ended)
    signals = handoff.get("signals") or {}
    decision = handoff.get("decision") or {}
    reviewers = handoff.get("reviewers") or {}
    findings = handoff.get("findings") or []
    dropped = handoff.get("dropped") or []
    root = _Span(run_id, "run", "code_check.run", None, started, ended, {
        "code_check.run_id": run_id, "code_check.skill": handoff.get("skill"), "code_check.mode": handoff.get("mode"),
        "code_check.repo": handoff.get("repo"), "code_check.pr": handoff.get("pr"), "code_check.pr.url": handoff.get("pr_url"),
        "code_check.head_sha": handoff.get("head_sha"), "code_check.head_moved": handoff.get("head_moved"),
        "code_check.pr_state": handoff.get("pr_state"), "code_check.blind": handoff.get("blind"),
        "code_check.tool_version": handoff.get("tool_version"), "code_check.prompts_hash": handoff.get("prompts_hash"),
        "code_check.config_hash": handoff.get("config_hash"), "code_check.models": handoff.get("models"),
        "code_check.panel": (handoff.get("panel") or {}).get("members"),
        "code_check.decision": decision.get("event"), "code_check.decision_reason": decision.get("reason"),
        "code_check.decision_incomplete": decision.get("incomplete"),
        "code_check.findings": len(findings), "code_check.dropped": len(dropped),
        "code_check.tool_calls": signals.get("tool_calls"), "code_check.tool_errors": len(signals.get("tool_errors") or []),
        "code_check.retries": signals.get("retries"), "code_check.permission_denials": signals.get("permission_denials"),
        "code_check.ignored_guidance": len(handoff.get("ignored_guidance") or []),
        "code_check.transcript": handoff.get("transcript"), "claude.session.id": _session_id(handoff),
    })
    spans = [root]
    agents = {}
    for agent in signals.get("agents") or []:
        name = str(agent.get("name"))
        span = _Span(run_id, f"agent:{name}", "code_check.agent", root.id, started, ended, {
            "code_check.agent.name": name, "code_check.agent.subagent": name.startswith("agent-"),
            "code_check.agent.turns": agent.get("turns"), "code_check.agent.output_tokens": agent.get("output_tokens"),
            "code_check.agent.tool_calls": agent.get("tool_calls"), "code_check.agent.tool_errors": agent.get("tool_errors"),
        })
        agents[name] = span
        spans.append(span)
    for err in signals.get("tool_errors") or []:
        target = agents.get(str(err.get("agent")), root)
        target.event("code_check.tool_error", {"code_check.tool": err.get("tool"), "code_check.agent.name": err.get("agent"),
                                               "exception.message": _text(err.get("error"), content)})
    for name, r in reviewers.items():
        root.event("code_check.reviewer", {
            "code_check.reviewer": name, "code_check.reviewer.output": r.get("output"), "code_check.reviewer.valid": r.get("valid"),
            "code_check.reviewer.errors": len(r.get("errors") or []),
            "code_check.reviewer.error_text": _text("; ".join(map(str, r.get("errors") or [])), content) or None})
    for f in findings:
        root.event("code_check.finding", {
            "code_check.finding.id": f.get("id"), "code_check.finding.severity": f.get("severity"),
            "code_check.finding.category": f.get("category"), "code.filepath": f.get("file"), "code.lineno": f.get("line")})
    for d in dropped:
        item = d if isinstance(d, dict) else {"reason": d}
        reason = item.get("reason") if isinstance(item.get("reason"), str) else json.dumps(item, sort_keys=True, default=str)
        root.event("code_check.finding.dropped", {
            "code_check.finding.id": item.get("id") if isinstance(item.get("id"), str) else None,
            "code.filepath": item.get("file") if isinstance(item.get("file"), str) else None,
            "code.lineno": item.get("line") if isinstance(item.get("line"), int) else None,
            "code_check.finding.drop_reason": _text(reason, content)})
    if feedback:
        spans.append(_feedback_span(run_id, root, feedback, ended, content))
    return {"resourceSpans": [{
        "resource": {"attributes": attrs(resource)},
        "scopeSpans": [{"scope": {"name": "codecheck", "version": __version__}, "spans": [s.to_otlp() for s in spans]}],
    }]}


def _feedback_span(run_id: str, root: _Span, fb: Mapping, ended: float, content: bool) -> _Span:
    at = max(float(fb.get("at") or ended), root.start)
    cmp = fb.get("comparison") or {}
    comments: Sequence[Mapping] = cmp.get("human_comments") or []
    general: Sequence[Mapping] = cmp.get("general_comments") or []
    labels: Mapping[str, str] = fb.get("labels") or {}
    bodies: Mapping[str, str] = fb.get("bodies") or {}
    digest = _hex(json.dumps([comments, general, labels, cmp.get("unraised_findings")], sort_keys=True, default=str), 12)
    span = _Span(run_id, f"feedback:{digest}", "code_check.feedback", root.id, at, at, {
        "code_check.feedback.human_comments": len(comments),
        "code_check.feedback.matched": sum(1 for c in comments if c.get("matched_finding")),
        "code_check.feedback.missed": len(cmp.get("misses") or []),
        "code_check.feedback.unraised_findings": len(cmp.get("unraised_findings") or []),
        "code_check.feedback.severity_disagreements": sum(1 for c in comments if c.get("severity_agrees") is False),
        "code_check.feedback.general_comments": len(general),
        **{f"code_check.feedback.label.{label}": sum(1 for v in labels.values() if v == label)
           for label in ("confirmed", "disputed", "no_signal", "unreviewed")},
    })
    for c in comments:
        span.event("code_check.human_comment", {
            "code_check.thread": c.get("thread_id"), "code.filepath": c.get("path"), "code.lineno": c.get("line"),
            "code_check.matched_finding": c.get("matched_finding"), "code_check.human_severity": c.get("human_severity"),
            "code_check.finding_severity": c.get("finding_severity"), "code_check.severity_agrees": c.get("severity_agrees"),
            "enduser.id": _text(c.get("author"), content), "code_check.comment.body": _text(bodies.get(c.get("thread_id")), content)})
    for g in general:
        span.event("code_check.general_comment", {
            "code_check.comment.kind": g.get("kind"), "code_check.comment.state": g.get("state"),
            "enduser.id": _text(g.get("author"), content), "code_check.comment.body": _text(g.get("body"), content)})
    for fid, label in labels.items():
        span.event("code_check.finding_label", {"code_check.finding.id": fid, "code_check.finding.label": label})
    return span


def summarize(payload: Mapping) -> dict:
    spans = [s for rs in payload["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]
    return {"trace_id": spans[0]["traceId"] if spans else None, "spans": len(spans), "events": sum(len(s["events"]) for s in spans)}
