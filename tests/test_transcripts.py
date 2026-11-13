import json
import os
import stat

from codecheck.shadow.transcripts import (
    copy_scrubbed,
    extract_signals,
    find_transcript,
    subagent_transcripts,
)

TOKEN = "ghp_" + "a1B2c3D4e5" * 3 + "XyZ123"


def _write(path, entries, raw_tail=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n" + raw_tail)
    return path


def _use(uid, name, inp, tokens=5):
    return {"type": "assistant", "message": {
        "content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}],
        "usage": {"output_tokens": tokens}}}


def _res(uid, text, err=True):
    block = {"type": "tool_result", "tool_use_id": uid, "content": text}
    if err:
        block["is_error"] = True
    return {"type": "user", "message": {"content": [block]}}


def test_find_transcript_picks_newest_containing_run_id(tmp_path):
    proj = tmp_path / "proj"
    old = _write(proj / "a.jsonl", [{"type": "x", "t": "RUN1"}])
    new = _write(proj / "b.jsonl", [{"type": "x", "t": "RUN1"}])
    other = _write(proj / "c.jsonl", [{"type": "x", "t": "nope"}])
    stale = _write(proj / "d.jsonl", [{"type": "x", "t": "RUN1"}])
    sub = _write(proj / "b" / "subagents" / "agent-1.jsonl", [{"t": "RUN1"}])
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    os.utime(other, (3000, 3000))
    os.utime(sub, (4000, 4000))
    os.utime(stale, (10, 10))
    assert find_transcript(tmp_path, "RUN1", max_age_s=2500, now=lambda: 3500) == new
    assert find_transcript(tmp_path, "RUN1", max_age_s=100, now=lambda: 3500) is None


def test_find_transcript_missing_dir_returns_none(tmp_path):
    assert find_transcript(tmp_path / "nope", "RUN1") is None


def test_subagent_transcripts_sorted(tmp_path):
    main = _write(tmp_path / "s.jsonl", [{"type": "x"}])
    assert subagent_transcripts(main) == []
    b = _write(tmp_path / "s" / "subagents" / "agent-b.jsonl", [])
    a = _write(tmp_path / "s" / "subagents" / "agent-a.jsonl", [])
    assert subagent_transcripts(main) == [a, b]


def test_extract_signals_counts_errors_retries_denials(tmp_path):
    main = _write(tmp_path / "main.jsonl", [
        {"type": "system"},
        _use("1", "Bash", {"command": "ls"}, 10),
        _res("1", "Permission denied for tool"),
        _use("2", "Bash", {"command": "ls"}, 7),
        _res("2", "boom"),
        _use("3", "Read", {"path": "x"}),
        _res("3", "ok", err=False),
    ])
    sub = _write(tmp_path / "agent-x.jsonl", [_use("9", "Grep", {"p": 1}), _res("9", "fine", err=False)])
    s = extract_signals([main, sub])
    assert s["tool_calls"] == 4
    assert len(s["tool_errors"]) == 2
    assert s["tool_errors"][0] == {"agent": "main", "tool": "Bash", "error": "Permission denied for tool"}
    assert s["retries"] == 1
    assert s["permission_denials"] == 1
    assert s["unreadable_lines"] == 0
    assert [a["name"] for a in s["agents"]] == ["main", "agent-x"]
    assert s["agents"][0] == {"name": "main", "tool_calls": 3, "tool_errors": 2, "turns": 3, "output_tokens": 22}


def test_extract_signals_skips_garbled_lines(tmp_path):
    p = _write(tmp_path / "m.jsonl", [_use("1", "Bash", {})], raw_tail='{"type": "assis\n[1,2]\n')
    s = extract_signals([p, tmp_path / "missing.jsonl"])
    assert s["tool_calls"] == 1
    assert s["unreadable_lines"] >= 2


def test_extract_and_copy_scrub_secrets(tmp_path):
    p = _write(tmp_path / "m.jsonl", [_use("1", "Bash", {"c": TOKEN}), _res("1", f"bad {TOKEN} " + "z" * 500)])
    s = extract_signals([p])
    assert TOKEN not in json.dumps(s)
    assert len(s["tool_errors"][0]["error"]) <= 300
    dest = tmp_path / "out"
    names = copy_scrubbed([p], dest)
    assert names == ["m.jsonl"]
    assert TOKEN not in (dest / "m.jsonl").read_text()
    assert stat.S_IMODE((dest / "m.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700


def test_extract_signals_survives_a_deeply_nested_line(tmp_path):
    # Deep enough that the decoder raises RecursionError (a stack-depth guard), not a ValueError.
    depth = 1_000_000
    p = _write(tmp_path / "m.jsonl", [_use("1", "Bash", {})], raw_tail="[" * depth + "]" * depth + "\n")
    s = extract_signals([p])
    assert s["tool_calls"] == 1 and s["unreadable_lines"] == 1


def test_extract_signals_collects_ignored_guidance(tmp_path):
    text = {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "ignored guidance: private thought"},
        {"type": "text", "text": f"done: 2 findings\nignored guidance: rule says {TOKEN} " + "q" * 400}]}}
    result = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "9", "content": [{"type": "text", "text": "done: 0; Ignored guidance: style rule"}]}]}}
    prompt = {"type": "user", "message": {"content": "ignored guidance: in a user prompt"}}
    many = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "\n".join(f"ignored guidance: reason {i}" for i in range(30))}]}}
    s = extract_signals([_write(tmp_path / "m.jsonl", [text, result, prompt, many])])
    got = s["ignored_guidance"]
    assert len(got) == 20 and all(len(g) <= 300 for g in got)
    assert got[0].startswith("ignored guidance: rule says") and TOKEN not in json.dumps(got)
    assert got[1] == "done: 0; Ignored guidance: style rule"
    assert not any("private thought" in g or "user prompt" in g for g in got)


def test_extract_signals_dedupes_ignored_guidance(tmp_path):
    line = "done: 1 kept; ignored guidance: x"
    a = _write(tmp_path / "a.jsonl", [{"type": "assistant", "message": {"content": [{"type": "text", "text": line}]}}])
    b = _write(tmp_path / "b.jsonl", [{"type": "user", "message": {"content": [{"type": "tool_result", "content": line}]}}])
    assert extract_signals([a, b])["ignored_guidance"] == [line]
