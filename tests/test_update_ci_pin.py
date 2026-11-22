import importlib.util
import io
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("update_ci_pin", ROOT / "scripts" / "update_ci_pin.py")
pin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pin)

WF = "with:\n  code-check-ref: v1.2.3 # keep me\n  other: x\n"


def test_newest_is_numeric_not_lexical():
    assert pin.newest_tag(["v1.9.0", "v1.10.0", "v1.2.3"]) == "v1.10.0"


def test_newest_ignores_prerelease_and_junk():
    assert pin.newest_tag(["v2.0.0-rc1", "latest", "1.5.0", "v1.2", "v1.2.3.4", "v1.2.3"]) == "v1.2.3"
    assert pin.newest_tag(["junk", ""]) is None


def test_current_pin_reads_value_and_missing():
    assert pin.current_pin(WF) == "v1.2.3"
    assert pin.current_pin("nothing here") is None


def test_bump_changes_only_that_line_and_keeps_comment():
    out = pin.bump(WF, "v1.3.0")
    assert out == "with:\n  code-check-ref: v1.3.0 # keep me\n  other: x\n"


def test_bump_rejects_non_release_tag():
    with pytest.raises(ValueError):
        pin.bump(WF, "main")


def test_needs_bump_only_when_strictly_newer():
    assert pin.needs_bump("v1.2.3", "v1.2.4")
    assert not pin.needs_bump("v1.2.3", "v1.2.3")
    assert not pin.needs_bump("v1.2.3", "v1.2.2")
    assert not pin.needs_bump("v1.10.0", "v1.9.9")


def test_sha_and_unparseable_pins_are_skipped():
    for value in ("c7ea220d8033092d50d1ff10306d84dd24d64aac", "main", "v1.2"):
        r = pin.decide(f"code-check-ref: {value}\n", ["v9.9.9"])
        assert r["action"] == "skip" and r["pin"] == value
    assert pin.decide("nothing", ["v1.0.0"])["action"] == "skip"


def run_cli(monkeypatch, capsys, path, tags, *extra):
    monkeypatch.setattr(sys, "stdin", io.StringIO(tags))
    assert pin.main(["--workflow", str(path), "--tags-from-stdin", *extra]) == 0
    return json.loads(capsys.readouterr().out)


def test_cli_dry_run_then_write_then_idempotent(tmp_path, monkeypatch, capsys):
    f = tmp_path / "wf.yml"
    f.write_text(WF)
    tags = "v1.2.2\nv1.2.3\nv1.2.4\nv1.3.0-rc1\n"
    r = run_cli(monkeypatch, capsys, f, tags)
    assert r["action"] == "bump" and r["pin"] == "v1.2.3" and r["newest"] == "v1.2.4"
    assert f.read_text() == WF  # no --write: untouched
    run_cli(monkeypatch, capsys, f, tags, "--write")
    assert "code-check-ref: v1.2.4 # keep me" in f.read_text()
    again = run_cli(monkeypatch, capsys, f, tags, "--write")
    assert again["action"] == "none"
    assert "code-check-ref: v1.2.4 # keep me" in f.read_text()


def test_cli_never_downgrades(tmp_path, monkeypatch, capsys):
    f = tmp_path / "wf.yml"
    f.write_text(WF)
    r = run_cli(monkeypatch, capsys, f, "v1.0.0\n", "--write")
    assert r["action"] == "none" and f.read_text() == WF


def test_real_workflow_is_pinned_to_a_release_not_ahead_of_version():
    text = (ROOT / ".github" / "workflows" / "code-check.yml").read_text()
    value = pin.current_pin(text)
    assert value and re.fullmatch(r"v\d+\.\d+\.\d+", value), value
    version = (ROOT / "VERSION").read_text().strip()
    assert pin.parse_tag(value) <= pin.parse_tag("v" + version)
