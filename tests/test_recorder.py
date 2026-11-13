from codecheck.config import Config
from codecheck.core.findings import finding_id
from codecheck.models import Decision, Finding, Severity
from codecheck.shadow.recorder import config_hash, record_post, record_run
from codecheck.shadow.store import ShadowStore

RID = "abcdef012345"


def test_record_run_stores_metadata_and_scrubbed_diff(tmp_path):
    store = ShadowStore(tmp_path)
    meta = {"run_id": RID, "skill": "review-pr", "repo": "o/r", "pr": "o/r#7", "head_sha": "abc", "mode": "pr",
            "panel": {"members": ["architect", "lang-python"]}, "models": {"architect": "sonnet"}}
    record_run(store, meta, "diff text ghp_" + "z" * 36, Config())
    (rec,) = store.read(RID)
    assert rec["type"] == "run" and rec["panel"] == ["architect", "lang-python"]
    assert rec["config_hash"] == config_hash(Config()) and rec["tool_version"]
    assert "ghp_" not in store.read_diff(RID)


def test_config_hash_changes_with_config():
    from dataclasses import replace

    from codecheck.config import ReviewConfig
    assert config_hash(Config()) != config_hash(replace(Config(), review=ReviewConfig(auto_approve=True)))


def test_record_post_writes_all_record_types_with_kept_flags(tmp_path):
    store = ShadowStore(tmp_path)
    kept = Finding("a.py", 3, Severity.WARNING, "bug", "msg", reviewers=("skeptic",), anchor="x")
    near = Finding("a.py", 4, Severity.SUGGESTION, "style", "nit", reviewers=("architect",))
    far = Finding("zzz.py", 9, Severity.WARNING, "bug", "unrelated", reviewers=("lang-python",))
    record_post(
        store, {"run_id": RID},
        reviewer_findings={"architect": [near], "lang-python": [far]},
        findings=[kept], inline_ids={finding_id(kept)}, decision=Decision("COMMENT", "r"),
        incomplete=False, dropped=[{"finding": {"file": "zzz.py"}, "reason": "duplicate"}],
    )
    recs = store.read(RID)
    types = [r["type"] for r in recs]
    assert types.count("reviewer_finding") == 2 and types.count("finding") == 1
    assert types.count("decision") == 1 and types.count("dropped") == 1
    assert {r["file"]: r["kept"] for r in recs if r["type"] == "reviewer_finding"} == {"a.py": True, "zzz.py": False}
    finding = next(r for r in recs if r["type"] == "finding")
    assert finding["id"] == finding_id(kept) and finding["inline"] is True
    assert next(r for r in recs if r["type"] == "decision")["event"] == "COMMENT"
