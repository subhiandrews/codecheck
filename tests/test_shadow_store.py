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
    assert store.read("2" * 12) == [] and store.read_diff("2" * 12) == ""


@pytest.mark.parametrize("text", [
    "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI", "GITHUB_TOKEN=abc", "DB_PASSWORD=hunter2",
    "client_secret=xyz", "access_token: tok123",
    '{"token": "abc"}', '{"password":"p w d"}', "secret: 'two words here'", "api_key: \"k\"",
])
def test_scrub_keyword_assignments_env_and_json_forms(text):
    out = scrub_text(text)
    assert "[REDACTED]" in out
    for leaked in ("wJalrXUtnFEMI", "abc", "hunter2", "xyz", "tok123", "p w d", "two words", '"k"'):
        if leaked in text:
            assert leaked not in out


_SHAPES = [
    "xoxb-" + "1234567890-abcdefghij",
    "sk-" + "A1b2C3d4E5" * 3,
    "AIza" + "A1b2C3d4E5" * 3 + "abcde",
    "sk_live_" + "A1b2C3d4E5" * 2,
    "rk_live_" + "A1b2C3d4E5" * 2,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.abcDEF_-123",
]


@pytest.mark.parametrize("secret", _SHAPES)
def test_scrub_extra_token_shapes_bare_env_and_json(secret):
    for text in (f"x {secret} y", f"MY_VALUE={secret}", f'{{"v": "{secret}"}}'):
        assert secret not in scrub_text(text)


def test_permissions_are_private(tmp_path):
    root = tmp_path / "shadow"
    store = ShadowStore(root)
    store.append(RID, {"type": "run"})
    store.save_diff(RID, "d")
    assert (root.stat().st_mode & 0o777) == 0o700
    assert ((root / f"{RID}.jsonl").stat().st_mode & 0o777) == 0o600
    assert ((root / f"{RID}.diff").stat().st_mode & 0o777) == 0o600


def test_unicode_line_separators_round_trip(tmp_path):
    store = ShadowStore(tmp_path)
    msg = "a\u2028b\u2029c\u0085d\x0be\x0cf"
    store.append(RID, {"type": "finding", "message": msg})
    store.append(RID, {"type": "decision"})
    recs = store.read(RID)
    assert [r["type"] for r in recs] == ["finding", "decision"] and recs[0]["message"] == msg


def test_run_id_rejects_trailing_newline():
    with pytest.raises(CodeCheckError):
        check_run_id(RID + "\n")


def test_append_after_truncated_line_keeps_next_record(tmp_path):
    store = ShadowStore(tmp_path)
    (tmp_path / f"{RID}.jsonl").write_text('{"type": "run", "tru')
    store.append(RID, {"type": "decision"})
    assert [r["type"] for r in store.read(RID)] == ["decision"]


@pytest.mark.parametrize("text", [
    "a" * 200_000,
    "a_" * 100_000,
    "token" * 40_000,
    'password: "' * 20_000,
    "-----BEGIN PRIVATE KEY-----" * 8_000,
    "eyJ-" * 50_000,
    "eyJ." * 50_000,
    "secret=" * 30_000,
])
def test_scrub_text_is_fast_on_pathological_input(text):
    import time
    start = time.perf_counter()
    scrub_text(text)
    assert time.perf_counter() - start < 2.0


@pytest.mark.parametrize("n", [600, 5000])
@pytest.mark.parametrize("fmt", ['password: "{}" tail', '{{"api_key": "{}"}} tail', "secret = '{}' tail"])
def test_long_quoted_secret_fully_removed(n, fmt):
    secret = ("xyzQ" * n)[:n] + "ENDMARK"
    out = scrub_text(fmt.format(secret))
    assert "xyz" not in out and "ENDMARK" not in out and "[REDACTED]" in out


@pytest.mark.parametrize("n", [5000, 20000])
def test_long_pem_block_fully_removed(n):
    body = ("MIIEvQIBADANBgkqhkiG9w0B" * (n // 24 + 1))[:n]
    out = scrub_text(f"before\n-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\nafter")
    assert "MIIE" not in out and out.startswith("before") and out.endswith("after")


def test_pem_without_end_redacted_to_end_of_text():
    out = scrub_text("keep\n-----BEGIN RSA PRIVATE KEY-----\nMIIEsecretbody\nmore")
    assert out == "keep\n[REDACTED]"


def test_long_jwt_segment_fully_removed():
    seg = "A1b2" * 750
    jwt = f"eyJhbGciOiJIUzI1NiJ9.{seg}.sig"
    out = scrub_text(f"x {jwt} y")
    assert "A1b2" not in out and "sig" not in out and out == "x [REDACTED] y"


def test_long_keyword_suffix_still_matched():
    assert "val" not in scrub_text("secret" + "x" * 100 + "=val123456")


def test_million_char_inputs_fast():
    import time
    for text in ("-----BEGIN PRIVATE KEY-----" * 40_000, "a" * 1_000_000, "token" * 200_000, "eyJ-" * 250_000):
        start = time.perf_counter()
        scrub_text(text)
        assert time.perf_counter() - start < 2.0
