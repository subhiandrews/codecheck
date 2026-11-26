from __future__ import annotations

import json
import subprocess

import pytest
from outcomes import (
    code_change,
    collect,
    gh_api,
    main,
    run,
    signal,
    summary,
    touched_old_lines,
)

PATCH = "@@ -10,4 +10,5 @@\n keep\n-old\n+new\n+newer\n keep\n"
REVIEW = {"submitted_at": "2026-11-24T20:00:00Z", "commit_id": "old1"}


def _gh(*, head="new1", replies=(), issue=(), files=()):
    data = {
        "user": {"login": "me"},
        "repos/o/r/pulls/1/reviews/7": REVIEW,
        "repos/o/r/pulls/1": {"head": {"sha": head}, "state": "open", "merged_at": None},
        "repos/o/r/pulls/1/reviews/7/comments": [
            {"id": 100, "path": "a.py", "line": 11, "original_line": 11, "body": "**Warning** · tests\n\ntext",
             "reactions": {"total_count": 1}}],
        "repos/o/r/pulls/1/comments": list(replies),
        "repos/o/r/issues/1/comments": list(issue),
        "repos/o/r/pulls/1/reviews": [],
        "repos/o/r/compare/old1...new1": {"files": list(files)},
    }
    return data.__getitem__


ENTRY = {"repo": "o/r", "pr": 1, "review_id": 7, "status": "posted"}
REPLY = {"id": 5, "in_reply_to_id": 100, "user": {"login": "author", "type": "User"}, "created_at": "2026-11-25T00:00:00Z"}


def test_a_file_missing_from_a_capped_compare_is_unknown_not_untouched():
    assert code_change([], "a.py", 11, 3, capped=True) == "unknown"
    assert code_change([], "a.py", 11, 3) == "file_untouched"
    assert code_change([{"filename": "b.py", "patch": PATCH}], "a.py", 11, 3, capped=True) == "unknown"


def test_collect_treats_a_full_list_of_files_as_capped():
    files = [{"filename": f"f{i}.py", "patch": PATCH} for i in range(300)]
    assert collect(ENTRY, _gh(files=files), "me")["findings"][0]["code"] == "unknown"
    assert collect(ENTRY, _gh(files=files[:299]), "me")["findings"][0]["code"] == "file_untouched"


def test_the_reviewed_commit_line_is_used_not_the_current_head_line():
    base = _gh(files=[{"filename": "a.py", "patch": PATCH}])

    def gh(endpoint):  # a push added 40 lines above the comment: the head says 51, the reviewed commit said 11
        if endpoint.endswith("/reviews/7/comments"):
            return [{"id": 100, "path": "a.py", "line": 51, "original_line": 11, "body": "**Warning** · x"}]
        return base(endpoint)

    f = collect(ENTRY, gh, "me")["findings"][0]
    assert (f["line"], f["code"]) == (11, "changed_near")


def test_a_mention_is_found_whatever_the_case_of_the_login():
    note = {"user": {"login": "author", "type": "User"}, "created_at": "2026-11-25T00:00:00Z", "body": "thanks @ME"}
    assert collect(ENTRY, _gh(issue=[note]), "me")["mentions_us"] is True


def test_the_size_of_a_renamed_file_is_found_under_its_old_path():
    renamed = {"filename": "new.py", "previous_filename": "a.py", "patch": PATCH, "additions": 2, "deletions": 1}
    f = collect(ENTRY, _gh(files=[renamed]), "me")["findings"][0]
    assert (f["code"], f["file_delta"]) == ("changed_near", "+2/-1")


def test_a_comment_with_only_a_head_side_line_is_unknown_not_guessed():
    base = _gh(files=[{"filename": "a.py", "patch": PATCH}])

    def gh(endpoint):
        if endpoint.endswith("/reviews/7/comments"):
            return [{"id": 100, "path": "a.py", "line": 11, "original_line": None, "body": "**Warning** · x"}]
        return base(endpoint)

    assert collect(ENTRY, gh, "me")["findings"][0]["code"] == "unknown"


def test_main_reports_malformed_input_in_one_line_and_an_unwritable_out_path(tmp_path, capsys):
    posted = tmp_path / "posted.json"
    posted.write_text(json.dumps(["not", "an", "object"]))
    assert main(["--posted", str(posted)]) == 1
    assert capsys.readouterr().err.count("\n") == 1
    posted.write_text(json.dumps({"prs": []}))
    assert main(["--posted", str(posted), "--out", str(tmp_path / "no-such-dir" / "out.json")]) == 1
    assert "FileNotFoundError" in capsys.readouterr().err


def test_an_entry_with_a_review_id_but_no_usable_pr_is_reported_not_fatal():
    no_key = {"repo": "o/r", "review_id": 7, "status": "posted"}
    null_pr = {"repo": "o/r", "pr": None, "review_id": 7, "status": "posted"}
    result = run({"prs": [no_key, null_pr, ENTRY]}, _gh())
    assert [p.get("error") is not None for p in result["prs"]] == [True, True, False]
    assert [p["error"].split(":")[0] for p in result["prs"][:2]] == ["KeyError", "TypeError"]


def test_the_error_row_keeps_gh_stderr():
    def gh(endpoint):
        if endpoint == "user":
            return {"login": "me"}
        raise subprocess.CalledProcessError(1, ["gh"], stderr="gh: Not Found (HTTP 404)\n")

    assert "HTTP 404" in run({"prs": [ENTRY]}, gh)["prs"][0]["error"]


def test_main_gives_one_line_and_exit_1_when_gh_itself_fails(tmp_path, monkeypatch, capsys):
    posted = tmp_path / "posted.json"
    posted.write_text(json.dumps({"prs": [ENTRY]}))

    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(4, ["gh"], stderr="To get started with GitHub CLI, run: gh auth login")

    monkeypatch.setattr(subprocess, "run", boom)
    assert main(["--posted", str(posted)]) == 1
    err = capsys.readouterr().err
    assert "gh auth login" in err and err.count("\n") == 1


def test_touched_old_lines_marks_removals_and_insertions():
    assert touched_old_lines(PATCH) == {11, 12}


def test_code_change_levels():
    files = [{"filename": "a.py", "patch": PATCH}]
    assert code_change(files, "a.py", 11, 3) == "changed_near"
    assert code_change(files, "a.py", 90, 3) == "file_changed"
    assert code_change(files, "b.py", 11, 3) == "file_untouched"


@pytest.mark.parametrize(("replied", "change", "expected"), [
    (True, "changed_near", "candidate_acted_on"),
    (False, "changed_near", "changed_without_reply"),
    (True, "file_untouched", "replied_no_change"),
    (False, "file_changed", "no_response_yet"),
])
def test_signal(replied, change, expected):
    assert signal(replied, change) == expected


def test_reply_plus_change_is_only_a_candidate():
    out = collect(ENTRY, _gh(replies=[REPLY], files=[{"filename": "a.py", "patch": PATCH}]), "me")
    f = out["findings"][0]
    assert (f["signal"], f["replies"], f["reactions"], f["kind"]) == ("candidate_acted_on", 1, 1, "Warning")


def test_claim_without_a_change_is_not_a_fix():
    out = collect(ENTRY, _gh(replies=[REPLY], files=[]), "me")
    assert out["findings"][0]["signal"] == "replied_no_change"


def test_our_own_and_bot_replies_do_not_count():
    ours = {**REPLY, "user": {"login": "me", "type": "User"}}
    bot = {**REPLY, "id": 6, "user": {"login": "ci", "type": "Bot"}}
    out = collect(ENTRY, _gh(replies=[ours, bot]), "me")
    assert out["findings"][0]["signal"] == "no_response_yet"


def test_mention_is_reported_for_the_pr_but_not_counted_as_a_reply():
    note = {"user": {"login": "author", "type": "User"}, "created_at": "2026-11-25T00:00:00Z", "body": "thanks @me"}
    out = collect(ENTRY, _gh(issue=[note], files=[{"filename": "a.py", "patch": PATCH}]), "me")
    assert out["mentions_us"] and out["findings"][0]["signal"] == "changed_without_reply"


def test_unknown_line_or_missing_patch_is_unverified_not_near():
    files = [{"filename": "a.py", "patch": PATCH}, {"filename": "big.bin"}]
    assert code_change(files, "a.py", 0, 3) == "unknown"
    assert code_change(files, "big.bin", 5, 3) == "unknown"
    assert signal(True, "unknown") == "replied_unverified"
    assert signal(False, "unknown") == "unverified"


def test_a_renamed_file_is_found_by_its_old_path():
    files = [{"filename": "new.py", "previous_filename": "a.py", "patch": PATCH}]
    assert code_change(files, "a.py", 11, 3) == "changed_near"


def test_one_failing_pr_does_not_lose_the_others():
    good = _gh()
    bad_entry = {"repo": "o/r", "pr": 9, "review_id": 8, "status": "posted"}

    def gh(endpoint):
        if "/9" in endpoint:
            raise subprocess.CalledProcessError(1, ["gh"])
        return good(endpoint)

    result = run({"prs": [bad_entry, ENTRY]}, gh)
    assert [p.get("error") is not None for p in result["prs"]] == [True, False]
    assert any("ERROR" in line for line in summary(result))


def _fake_gh(monkeypatch, stdout):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=stdout))


def test_gh_api_merges_a_paged_compare(monkeypatch):
    pages = '{"files": [{"filename": "a"}, {"filename": "b"}]}\n{"files": [{"filename": "b"}, {"filename": "c"}]}'
    _fake_gh(monkeypatch, pages)
    assert [f["filename"] for f in gh_api("repos/o/r/compare/x...y")["files"]] == ["a", "b", "c"]


def test_gh_api_joins_paged_arrays_and_refuses_other_endpoints(monkeypatch):
    _fake_gh(monkeypatch, "[1, 2]\n[3]")
    assert gh_api("repos/o/r/pulls/1/comments") == [1, 2, 3]
    with pytest.raises(ValueError, match="unexpected endpoint"):
        gh_api("graphql")


def test_unchanged_head_skips_compare():
    out = collect(ENTRY, _gh(head="old1"), "me")  # compare endpoint is absent for head == reviewed
    assert out["findings"][0]["code"] == "file_untouched"


def test_run_skips_unposted_and_summarises():
    posted = {"posted_on": "2026-11-24", "prs": [ENTRY, {"repo": "o/r", "pr": 2, "status": "not_posted", "review_id": None}]}
    result = run(posted, _gh())
    assert len(result["prs"]) == 1
    assert summary(result) == [
        "o/r#1 a.py:11 Warning: no_response_yet (replies 0, reactions 1, code file_untouched +0/-0, pr open)"]


def test_null_line_on_the_review_listing_falls_back_to_the_pr_listing():
    data = {
        "user": {"login": "me"}, "repos/o/r/pulls/1/reviews/7": REVIEW,
        "repos/o/r/pulls/1": {"head": {"sha": "new1"}, "state": "open", "merged_at": None},
        "repos/o/r/pulls/1/reviews/7/comments": [{"id": 100, "path": "a.py", "line": None, "original_line": None, "body": ""}],
        "repos/o/r/pulls/1/comments": [{"id": 100, "path": "a.py", "line": None, "original_line": 11, "user": {"login": "me"}}],
        "repos/o/r/issues/1/comments": [], "repos/o/r/pulls/1/reviews": [],
        "repos/o/r/compare/old1...new1": {"files": [{"filename": "a.py", "patch": PATCH, "additions": 2, "deletions": 1}]},
    }
    f = collect(ENTRY, data.__getitem__, "me")["findings"][0]
    assert (f["line"], f["code"], f["file_delta"]) == (11, "changed_near", "+2/-1")


def test_bad_repo_name_is_refused():
    with pytest.raises(ValueError, match="bad repo"):
        collect({**ENTRY, "repo": "--flag/x"}, _gh(), "me")
