import pytest

from codecheck.errors import CodeCheckError
from codecheck.runids import check_run_id, new_run_id
from codecheck.shadow.scrub import scrub, scrub_text
from codecheck.shadow.store import ShadowStore

RID = "abcdef012345"


@pytest.mark.parametrize("secret", [
    "ghp_" + "A1b2C3d4E5" * 4,
    "github_pat_" + "A1b2C3d4E5" * 4,
    "sk-ant-api03-" + "abcDEF123_-" * 3,
    "AKIA" + "ABCDEFGHIJKLMNOP",
])
def test_scrub_text_redacts_token_shapes(secret):
    out = scrub_text(f"prefix {secret} suffix")
    assert secret not in out and "[REDACTED]" in out
    assert out.startswith("prefix") and out.endswith("suffix")


def test_scrub_redacts_assignments_headers_and_private_keys():
    text = ("Authorization: Bearer abc123def456ghi\napi_key = 'abcdefgh12345'\n"
            "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n")
    out = scrub_text(text)
    for leaked in ("abc123def456ghi", "abcdefgh12345", "MIIE"):
        assert leaked not in out
    assert out.count("[REDACTED]") >= 3


def test_scrub_leaves_ordinary_text_and_walks_structures():
    assert scrub_text("token count is 5 and secret sauce") == "token count is 5 and secret sauce"
    assert scrub({"a": ["ghp_" + "x" * 36, 3], "b": {"c": None}}) == {"a": ["[REDACTED]", 3], "b": {"c": None}}


def test_run_id_validation():
    assert check_run_id(new_run_id())
    for bad in ("../../etc", "ABCDEF012345", "short", ""):
        with pytest.raises(CodeCheckError):
            check_run_id(bad)


def test_append_read_scrubs_and_survives_corrupt_lines(tmp_path):
    store = ShadowStore(tmp_path, now=lambda: 123.0)
    store.append(RID, {"type": "run", "pr": "o/r#7", "note": "ghp_" + "x" * 36})
    with open(tmp_path / f"{RID}.jsonl", "a") as fh:
        fh.write("{truncated\n")
    store.append(RID, {"type": "decision", "event": "COMMENT"})
    recs = store.read(RID)
    assert [r["type"] for r in recs] == ["run", "decision"]
    assert recs[0]["schema"] == 1 and recs[0]["ts"] == 123.0 and recs[0]["run_id"] == RID
    assert "ghp_" not in recs[0]["note"]


def test_run_ids_runs_for_pr_and_diff(tmp_path):
    store = ShadowStore(tmp_path)
    store.append(RID, {"type": "run", "pr": "o/r#7"})
    store.append("111111111111", {"type": "run", "pr": "o/r#8"})
    store.save_diff(RID, "diff --git a/x b/x\n+token = ghp_" + "y" * 36 + "\n")
    assert store.run_ids() == sorted([RID, "111111111111"])
    assert store.runs_for_pr("o/r#7") == [RID]
    assert "ghp_" not in store.read_diff(RID)
    assert store.read(("2" * 12)) == [] and store.read_diff("2" * 12) == ""
