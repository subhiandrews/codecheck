import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from clihelp import call
from test_handoff import _pr_handoff_run, handoff_files, human, local_run

from codecheck.config import TelemetryConfig, load_config
from codecheck.errors import CodeCheckError, ConfigError
from codecheck.shadow.handoff import near_finding
from codecheck.telemetry import otlp
from codecheck.telemetry.build import build_payload, summarize, trace_id

pytestmark = pytest.mark.usefixtures("git_env")

ENV = {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.example.com:4318", "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer%20tok-0"}
HANDOFF = {
    "run_id": "r1", "skill": "review-pr", "mode": "pr", "repo": "o/r", "pr": "o/r#7", "pr_url": "https://github.com/o/r/pull/7",
    "head_sha": "abc123", "head_moved": False, "pr_state": "open", "blind": True, "tool_version": "0.2.0",
    "prompts_hash": "p" * 12, "config_hash": "c" * 12, "models": {"aggregator": "opus"}, "panel": {"members": ["skeptic"]},
    "decision": {"event": "COMMENT", "reason": "warnings", "incomplete": False},
    "reviewers": {"skeptic": {"output": True, "valid": 1, "errors": ["bad line SECRETISH"]}},
    "findings": [{"id": "f1", "file": "app.py", "line": 2, "severity": "warning", "category": "bug"}],
    "dropped": [{"id": "f9", "file": "app.py", "line": 9, "reason": "not a bug in this repo"}],
    "ignored_guidance": ["x"],
    "signals": {"tool_calls": 5, "retries": 1, "permission_denials": 1, "tool_errors": [
        {"agent": "agent-a1", "tool": "Bash", "error": "permission denied: /home/me/secret"}],
        "agents": [{"name": "sess-1", "turns": 3, "output_tokens": 90, "tool_calls": 2, "tool_errors": 0},
                   {"name": "agent-a1", "turns": 4, "output_tokens": 120, "tool_calls": 3, "tool_errors": 1}]},
    "transcript": True, "transcript_files": ["sess-1.jsonl", "agent-a1.jsonl"],
}


def spans_of(payload):
    return {s["name"] + (":" + s["attributes"][0]["value"]["stringValue"] if s["name"] == "code_check.agent" else ""): s
            for s in payload["resourceSpans"][0]["scopeSpans"][0]["spans"]}


def attr(span, key):
    for a in span["attributes"]:
        if a["key"] == key:
            return next(iter(a["value"].values()))
    return None


def event_attrs(event):
    return {a["key"]: next(iter(a["value"].values())) for a in event["attributes"]}


# -- config ---------------------------------------------------------------------------------------------------


def test_config_defaults_off_and_repo_cannot_set_it(tmp_path):
    cfg = load_config(tmp_path / "repo", tmp_path / "home", {})
    assert cfg.telemetry == TelemetryConfig() and cfg.telemetry.enabled is False and cfg.telemetry.content is False
    assert load_config(tmp_path / "repo", tmp_path / "home", {"CODE_CHECK_TELEMETRY__ENABLED": "1"}).telemetry.enabled
    (tmp_path / "r2").mkdir()
    (tmp_path / "r2/.code-check.toml").write_text("[telemetry]\nenabled = true\n")
    with pytest.raises(ConfigError, match=r"telemetry.*repository"):
        load_config(tmp_path / "r2", tmp_path / "home", {})


@pytest.mark.parametrize("env", [{"CODE_CHECK_TELEMETRY__ENDPOINT": "ftp://x"}, {"CODE_CHECK_TELEMETRY__TIMEOUT_SECONDS": "0"}])
def test_config_rejects_a_bad_endpoint_or_timeout(tmp_path, env):
    with pytest.raises(ConfigError, match="telemetry"):
        load_config(tmp_path / "repo", tmp_path / "home", env)


# -- endpoint and headers -------------------------------------------------------------------------------------


def target(cfg=None, env=None):
    return otlp.resolve_target(cfg or TelemetryConfig(), env if env is not None else ENV)


def test_resolve_target_sources_and_precedence():
    assert target(env={}) is None
    assert target(env={**ENV, "OTEL_SDK_DISABLED": "true"}) is None
    assert target().url == "https://collector.example.com:4318/v1/traces"
    assert target(env={**ENV, "OTEL_EXPORTER_OTLP_ENDPOINT": "https://c.example.com/otlp/"}).url == "https://c.example.com/otlp/v1/traces"
    assert target(env={**ENV, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://t.example.com/x"}).url == "https://t.example.com/x"
    assert target(TelemetryConfig(endpoint="http://localhost:4318/v1/traces"), ENV).url == "http://localhost:4318/v1/traces"


@pytest.mark.parametrize("env,match", [
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example.com"}, "https:// unless it is localhost"),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://user:pw@collector.example.com"}, "credentials"),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "file:///etc/passwd"}, "http:// or https://"),
    ({**ENV, "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, "gRPC"),
])
def test_resolve_target_refuses_unsafe_or_unusable_endpoints(env, match):
    with pytest.raises(CodeCheckError, match=match):
        target(env=env)


def test_a_grpc_default_does_not_block_an_explicit_http_endpoint():
    env = {**ENV, "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}
    assert target(TelemetryConfig(endpoint="http://127.0.0.1:4318"), env).url == "http://127.0.0.1:4318/v1/traces"


def test_headers_come_from_the_environment_and_never_leak_in_errors():
    t = target(env={**ENV, "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer%20tok-123, x-team=core"})
    assert t.headers == {"Authorization": "Bearer tok-123", "x-team": "core"}
    assert "tok-123" not in repr(t)
    with pytest.raises(CodeCheckError) as e:
        target(env={**ENV, "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=tok-123\r\nX-Evil: 1"})
    assert "tok-123" not in str(e.value)
    with pytest.raises(CodeCheckError):
        target(env={**ENV, "OTEL_EXPORTER_OTLP_HEADERS": "Host=evil.example.com"})
    t = target(env={**ENV, "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "Authorization=Bearer%20t2,a=1"})
    assert t.headers == {"Authorization": "Bearer t2", "a": "1"}


# -- the token is mandatory for a remote collector -------------------------------------------------------------

REMOTE = {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.example.com:4318"}


@pytest.mark.parametrize("headers", [None, "", "x-team=core", "Authorization=", "Authorization=Bearer", "Authorization=Bearer%20",
                                     "authorization=%20%20"])
def test_a_remote_endpoint_without_a_token_is_refused(headers):
    env = REMOTE if headers is None else {**REMOTE, "OTEL_EXPORTER_OTLP_HEADERS": headers}
    with pytest.raises(CodeCheckError, match="token is required"):
        target(env=env)


def test_localhost_needs_no_token_for_testing():
    assert target(TelemetryConfig(endpoint="http://localhost:4318"), {}).headers == {}


def test_a_token_file_supplies_the_token_and_must_be_private(tmp_path):
    f = tmp_path / "tok"
    f.write_text("s3cret-token\n")
    f.chmod(0o600)
    t = target(TelemetryConfig(token_file=str(f)), REMOTE)
    assert t.headers == {"Authorization": "Bearer s3cret-token"} and "s3cret" not in repr(t)
    t = target(TelemetryConfig(token_file=str(f), auth_header="x-api-key"), REMOTE)
    assert t.headers == {"x-api-key": "s3cret-token"}
    f.chmod(0o644)
    with pytest.raises(CodeCheckError, match="readable by other users") as e:
        target(TelemetryConfig(token_file=str(f)), REMOTE)
    assert "s3cret" not in str(e.value)


@pytest.mark.parametrize("text", ["", "  \n", "two words", "tok\x00en"])
def test_an_empty_or_malformed_token_file_is_refused(tmp_path, text):
    f = tmp_path / "tok"
    f.write_text(text)
    f.chmod(0o600)
    with pytest.raises(CodeCheckError, match="token_file"):
        target(TelemetryConfig(token_file=str(f)), REMOTE)


def test_a_missing_token_file_is_refused(tmp_path):
    with pytest.raises(CodeCheckError, match="cannot read telemetry.token_file"):
        target(TelemetryConfig(token_file=str(tmp_path / "nope")), REMOTE)


def test_the_header_from_the_environment_wins_over_the_token_file(tmp_path):
    f = tmp_path / "tok"
    f.write_text("from-file")
    f.chmod(0o600)
    t = target(TelemetryConfig(token_file=str(f)), ENV)
    assert t.headers == {"Authorization": "Bearer tok-0"}


def test_config_validates_the_auth_header_and_repo_cannot_set_the_token_file(tmp_path):
    with pytest.raises(ConfigError, match="auth_header"):
        load_config(tmp_path / "repo", tmp_path / "home", {"CODE_CHECK_TELEMETRY__AUTH_HEADER": "bad header\r\n"})
    (tmp_path / "r3").mkdir()
    (tmp_path / "r3/.code-check.toml").write_text('[telemetry]\ntoken_file = "/etc/passwd"\n')
    with pytest.raises(ConfigError, match="telemetry"):
        load_config(tmp_path / "r3", tmp_path / "home", {})


def test_resource_attributes_come_from_the_standard_variables():
    t = target(env={**ENV, "OTEL_RESOURCE_ATTRIBUTES": "team=core,deployment.environment=ci", "OTEL_SERVICE_NAME": "cc-ci"})
    assert t.resource["team"] == "core" and t.resource["service.name"] == "cc-ci" and "service.version" in t.resource


# -- the payload ----------------------------------------------------------------------------------------------


def payload(content=False, feedback=None, handoff=HANDOFF):
    return build_payload(handoff, started=1_000.0, ended=1_060.0, resource={"service.name": "code-check"},
                         content=content, feedback=feedback)


def test_payload_links_run_pr_commit_session_and_agents():
    spans = spans_of(payload())
    root = spans["code_check.run"]
    assert root["traceId"] == trace_id("r1") and "parentSpanId" not in root
    assert (attr(root, "code_check.pr.url"), attr(root, "code_check.head_sha")) == ("https://github.com/o/r/pull/7", "abc123")
    assert attr(root, "claude.session.id") == "sess-1" and attr(root, "code_check.decision") == "COMMENT"
    assert attr(root, "code_check.tool_errors") == "1" and attr(root, "code_check.permission_denials") == "1"
    assert root["startTimeUnixNano"] == "1000000000000" and root["endTimeUnixNano"] == "1060000000000"
    sub = spans["code_check.agent:agent-a1"]
    assert sub["parentSpanId"] == root["spanId"] and attr(sub, "code_check.agent.subagent") is True
    assert attr(sub, "code_check.agent.output_tokens") == "120"
    (err,) = sub["events"]  # the failed tool call sits on the agent that made it
    assert err["name"] == "code_check.tool_error" and event_attrs(err)["code_check.tool"] == "Bash"
    findings = [e for e in root["events"] if e["name"] == "code_check.finding"]
    assert [event_attrs(e)["code_check.finding.id"] for e in findings] == ["f1"]
    assert event_attrs(findings[0])["code.lineno"] == "2"


def test_free_text_is_exported_only_with_content():
    off, on = json.dumps(payload(content=False)), json.dumps(payload(content=True))
    for text in ("permission denied", "not a bug in this repo", "SECRETISH"):
        assert text not in off and text in on


def test_ids_are_deterministic_so_a_resend_is_idempotent():
    assert payload() == payload()
    assert summarize(payload()) == {"trace_id": trace_id("r1"), "spans": 3, "events": 4}
    assert trace_id("r1") != trace_id("r2") and len(trace_id("r1")) == 32


def test_feedback_is_its_own_span_and_a_later_snapshot_adds_a_span():
    fb = {"at": 1_500.0, "bodies": {"T1": "y is never read"}, "labels": {"f1": "confirmed"}, "comparison": {
        "human_comments": [{"thread_id": "T1", "path": "app.py", "line": 1, "author": "bob", "matched_finding": "f1",
                            "human_severity": "warning", "finding_severity": "warning", "severity_agrees": True}],
        "misses": [], "unraised_findings": [], "general_comments": [{"kind": "review", "author": "carol", "state": "APPROVED", "body": "ok"}]}}
    p = payload(feedback=fb)
    span = spans_of(p)["code_check.feedback"]
    assert span["parentSpanId"] == spans_of(p)["code_check.run"]["spanId"] and span["startTimeUnixNano"] == "1500000000000"
    assert attr(span, "code_check.feedback.matched") == "1" and attr(span, "code_check.feedback.label.confirmed") == "1"
    assert "bob" not in json.dumps(p) and "never read" not in json.dumps(p)
    assert "bob" in json.dumps(payload(content=True, feedback=fb)) and "never read" in json.dumps(payload(content=True, feedback=fb))
    fb2 = {**fb, "labels": {"f1": "disputed"}}
    assert spans_of(payload(feedback=fb2))["code_check.feedback"]["spanId"] != span["spanId"]


def test_near_finding_is_the_collect_rule():
    ours = [{"id": "a", "file": "x.py", "line": 10}]
    assert near_finding(ours, "x.py", 13) == "a" and near_finding(ours, "x.py", 14) is None and near_finding(ours, "y.py", 10) is None
    assert near_finding(ours, "x.py", None) is None


# -- the transport, against a real local server ---------------------------------------------------------------


class Collector:
    def __init__(self, status=200, reply="{}", redirect=None):
        self.requests, outer = [], self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append((self.path, dict(self.headers), body))
                if redirect:
                    self.send_response(302)
                    self.send_header("Location", redirect)
                    self.end_headers()
                    return
                self.send_response(status)
                self.end_headers()
                self.wfile.write(reply.encode())

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def collector():
    made = []

    def make(**kw):
        made.append(Collector(**kw))
        return made[-1]

    yield make
    for c in made:
        c.close()


def test_post_json_sends_headers_and_body(collector):
    c = collector()
    status, _ = otlp.post_json(c.url + "/v1/traces", {"Authorization": "Bearer t"}, b'{"a":1}', 5)
    (path, headers, body) = c.requests[0]
    assert status == 200 and path == "/v1/traces" and body == b'{"a":1}'
    assert headers["Authorization"] == "Bearer t" and headers["Content-Type"] == "application/json"


def test_post_json_does_not_follow_a_redirect_with_the_token(collector):
    other = collector()
    c = collector(redirect=other.url + "/steal")
    status, _ = otlp.post_json(c.url, {"Authorization": "Bearer t"}, b"{}", 5)
    assert status == 302 and other.requests == []


def test_post_json_reports_an_unreachable_endpoint_without_the_path():
    with pytest.raises(CodeCheckError, match="cannot reach the OTLP endpoint 127.0.0.1:9") as e:
        otlp.post_json("http://127.0.0.1:9/v1/traces?token=abc", {}, b"{}", 1)
    assert "token=abc" not in str(e.value)


# -- end to end through the CLI -------------------------------------------------------------------------------


def telemetry_on(ctx, url, **over):
    ctx.config = replace(ctx.config, telemetry=replace(ctx.config.telemetry, enabled=True, endpoint=url, **over))


def test_nothing_is_exported_or_changed_when_telemetry_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(otlp, "post_json", lambda *a, **k: pytest.fail("the network was used with telemetry off"))
    ctx, out = local_run(tmp_path)
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True and "telemetry" not in res
    folder, _ = handoff_files(ctx, out["run_id"])
    assert not (folder / "telemetry.json").exists()
    code, res = call(ctx, "telemetry", "push", "--run", out["run_id"])
    assert code == 0 and res == {"telemetry": False, "reason": "telemetry.enabled is false"}


def test_handoff_exports_the_run_and_a_second_push_is_a_noop(tmp_path, collector):
    c = collector()
    ctx, out = local_run(tmp_path)
    telemetry_on(ctx, c.url)
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True
    (r,) = res["telemetry"]["results"]
    assert r["status"] == "sent" and r["run_id"] == out["run_id"] and r["spans"] >= 1
    (_, _, body) = c.requests[0]
    root = spans_of(json.loads(body))["code_check.run"]
    assert attr(root, "code_check.run_id") == out["run_id"] and attr(root, "code_check.skill") == "review-local"
    folder, data = handoff_files(ctx, out["run_id"])  # the local record is untouched
    assert data["run_id"] == out["run_id"] and json.loads((folder / "telemetry.json").read_text())["last_error"] is None
    before = len(c.requests)
    code, res = call(ctx, "telemetry", "push", "--run", out["run_id"])
    assert res["results"][0]["status"] == "unchanged" and len(c.requests) == before
    code, res = call(ctx, "telemetry", "push", "--run", out["run_id"], "--force")
    assert res["results"][0]["status"] == "sent" and len(c.requests) == before + 1


def test_an_unreachable_collector_never_fails_the_handoff_and_push_all_retries(tmp_path, collector):
    ctx, out = local_run(tmp_path)
    telemetry_on(ctx, "http://127.0.0.1:9")
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True
    (r,) = res["telemetry"]["results"]
    assert r["status"] == "failed" and "cannot reach" in r["error"]
    folder, _ = handoff_files(ctx, out["run_id"])
    state = json.loads((folder / "telemetry.json").read_text())
    assert state["attempts"] == 1 and "sent" not in state
    c = collector()
    telemetry_on(ctx, c.url)
    code, res = call(ctx, "telemetry", "push", "--all")
    assert [x["status"] for x in res["results"]] == ["sent"] and len(c.requests) == 1
    assert json.loads((folder / "telemetry.json").read_text())["attempts"] == 0


def test_a_rejecting_collector_is_a_failure(tmp_path, collector):
    c = collector(status=401, reply="unauthorized")
    ctx, out = local_run(tmp_path)
    call(ctx, "handoff", "--run", out["run_id"])
    telemetry_on(ctx, c.url)
    code, res = call(ctx, "telemetry", "push", "--run", out["run_id"])
    assert code == 0 and res["results"][0]["status"] == "failed" and "HTTP 401" in res["results"][0]["error"]
    c2 = collector(reply='{"partialSuccess": {"rejectedSpans": 2}}')
    telemetry_on(ctx, c2.url)
    code, res = call(ctx, "telemetry", "push", "--run", out["run_id"])
    assert res["results"][0]["status"] == "failed"


def test_dry_run_prints_the_payload_and_contacts_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(otlp, "post_json", lambda *a, **k: pytest.fail("dry run used the network"))
    ctx, out = local_run(tmp_path)
    call(ctx, "handoff", "--run", out["run_id"])
    code, res = call(ctx, "telemetry", "push", "--run", out["run_id"], "--dry-run")  # telemetry is not even enabled
    assert code == 0 and res["dry_run"] is True and res["endpoint"] is None
    (r,) = res["results"]
    assert r["status"] == "dry-run" and spans_of(r["payload"])["code_check.run"]["traceId"] == trace_id(out["run_id"])


def test_enabled_without_an_endpoint_says_so(tmp_path):
    ctx, out = local_run(tmp_path)
    ctx.config = replace(ctx.config, telemetry=replace(ctx.config.telemetry, enabled=True))
    call(ctx, "handoff", "--run", out["run_id"])
    code, res = call(ctx, "telemetry", "push", "--all")
    assert code == 0 and res["telemetry"] is False and "no OTLP endpoint" in res["reason"]


def test_a_pr_run_exports_the_pr_link_commit_and_late_feedback(tmp_path, collector):
    c = collector()
    ctx, provider, pr, out = _pr_handoff_run(tmp_path)
    telemetry_on(ctx, c.url)
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["telemetry"]["results"][0]["status"] == "sent"
    first = spans_of(json.loads(c.requests[0][2]))
    root = first["code_check.run"]
    assert attr(root, "code_check.pr.url") == pr.url and attr(root, "code_check.head_sha")
    assert "code_check.feedback" not in first  # no human has commented yet
    provider.threads[pr.ref] = [human("T1", 1, "**Warning**: y is never read"), human("T2", 300, "rename this")]
    code, res = call(ctx, "shadow", "collect", "--pr", str(pr.ref))  # a late comment arrives
    assert code == 0 and res["telemetry"]["results"][0]["status"] == "sent"
    second = spans_of(json.loads(c.requests[1][2]))
    fb = second["code_check.feedback"]
    assert attr(fb, "code_check.feedback.human_comments") == "2" and attr(fb, "code_check.feedback.matched") == "1"
    assert attr(fb, "code_check.feedback.missed") == "1"
    assert fb["traceId"] == root["traceId"] and fb["parentSpanId"] == second["code_check.run"]["spanId"]
    assert "bob" not in c.requests[1][2].decode()  # content is off


def test_content_flag_adds_free_text_to_a_pr_run(tmp_path, collector):
    c = collector()
    ctx, provider, pr, out = _pr_handoff_run(tmp_path)
    telemetry_on(ctx, c.url, content=True)
    provider.threads[pr.ref] = [human("T1", 1, "**Warning**: y is never read")]
    call(ctx, "handoff", "--run", out["run_id"])
    body = c.requests[-1][2].decode()
    assert "bob" in body and "y is never read" in body


def test_push_all_stops_at_the_first_failed_send(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from codecheck.shadow.store import ShadowStore, write_private
    from codecheck.telemetry.push import push_runs, pushable_runs

    shadow = ShadowStore(tmp_path / "shadow")
    for rid in ("aaaaaaaa1111", "bbbbbbbb2222"):
        shadow.append(rid, {"type": "run", "repo": "o/r"})
        write_private(shadow.handoff_dir(rid) / "handoff.json", json.dumps({**HANDOFF, "run_id": rid, "pr": None}))
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise CodeCheckError("cannot reach the OTLP endpoint x: URLError")

    monkeypatch.setattr(otlp, "post_json", boom)
    cfg = replace(TelemetryConfig(), enabled=True)
    ctx = SimpleNamespace(shadow=shadow, config=SimpleNamespace(telemetry=cfg), env=ENV)
    res = push_runs(ctx, pushable_runs(shadow))
    assert [r["status"] for r in res["results"]] == ["failed", "not_attempted"] and len(calls) == 1
