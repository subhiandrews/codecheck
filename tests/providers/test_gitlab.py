import json

import pytest
from fakes import FakeRunner

from codecheck.errors import ProviderError
from codecheck.models import (
    Check,
    Decision,
    PRRef,
    ReviewComment,
    Thread,
    ThreadComment,
)
from codecheck.providers.gitlab import (
    GitLabProvider,
    _docs,
    parse_remote_url,
    remote_host,
    split_thread_id,
    thread_id,
)
from providers.contract import ProviderContract

REF = PRRef("g/s/p", 7)  # a nested namespace on purpose
MR = "projects/g%2Fs%2Fp/merge_requests/7"
DISC = "a1b2c3d4e5f6a7b8"
TID = f"g/s/p:7:{DISC}"

MR_JSON = json.dumps({
    "iid": 7, "title": "t", "description": "d", "author": {"username": "alice"}, "target_branch": "main",
    "source_branch": "feat", "sha": "abc123", "draft": False, "state": "opened",
    "web_url": "https://gitlab.com/g/s/p/-/merge_requests/7", "source_project_id": 1, "target_project_id": 1,
    "reviewers": [{"id": 5}],
})


def _note(nid, body, user="alice", at="2026-01-01T00:00:00.000Z", type_="DiffNote", resolved=False, **extra):
    return {"id": nid, "type": type_, "body": body, "author": {"username": user}, "created_at": at, "system": False,
            "resolvable": type_ == "DiffNote", "resolved": resolved,
            "position": {"new_path": "a.py", "old_path": "a.py", "new_line": 3, "old_line": None}, **extra}


DISCUSSIONS = json.dumps([
    {"id": DISC, "notes": [_note(101, "bug", "me"), _note(102, "will fix", "alice", "2026-01-01T01:00:00.000Z")]},
    {"id": "deadbeefdeadbeef", "notes": [_note(103, "general", type_=None)]},
])
NOTES = json.dumps([
    {**_note(1, "summary\n\n<!-- code-check:review:abc123 -->", "me", type_=None)},
    {**_note(2, "thanks", "alice", "2026-01-02T00:00:00.000Z", type_=None)},
    {**_note(3, "inline", "alice", type_="DiffNote")},
    {"id": 4, "system": True, "body": "approved this merge request", "author": {"username": "alice"},
     "created_at": "2026-01-03T00:00:00.000Z"},
])
VERSIONS = json.dumps([{"base_commit_sha": "b1", "start_commit_sha": "s1", "head_commit_sha": "abc123def",
                        "created_at": "2026-01-01T00:00:00.000Z"}])
JOBS = json.dumps([
    {"id": 9, "name": "test", "status": "failed", "allow_failure": False, "web_url": "u9"},
    {"id": 10, "name": "lint", "status": "success", "allow_failure": False, "web_url": "u10"},
    {"id": 11, "name": "opt", "status": "failed", "allow_failure": True, "web_url": "u11"},
    {"id": 12, "name": "slow", "status": "running", "allow_failure": False, "web_url": "u12"},
])


def runner() -> FakeRunner:
    return (
        FakeRunner()
        .on("glab", "api", "user", stdout='{"username": "me"}')
        .on("glab", "api", MR, stdout=MR_JSON)
        .on("glab", "api", f"{MR}/diffs?per_page=100", "--paginate", stdout='[{"new_path": "a.py"}][{"new_path": "b.py"}]')
        .on("glab", "api", f"{MR}/discussions?per_page=100", "--paginate", stdout=DISCUSSIONS)
        .on("glab", "api", f"{MR}/notes?sort=asc&order_by=created_at&per_page=100", "--paginate", stdout=NOTES)
        .on("glab", "api", f"{MR}/pipelines", stdout='[{"id": 55}]')
        .on("glab", "api", "projects/g%2Fs%2Fp/pipelines/55/jobs?per_page=100", "--paginate", stdout=JOBS)
        .on("glab", "api", f"{MR}/versions", stdout=VERSIONS)
        .on("glab", "api", "users?username=alice", stdout='[{"id": 9}]')
        .on_prefix("glab", "api", f"{MR}/discussions", stdout="{}")
        .on_prefix("glab", "api", f"{MR}/notes", stdout="{}")
        .on_prefix("glab", "api", f"{MR}/approve", stdout="{}")
        .on_prefix("glab", "api", MR, stdout="{}")
        .on_prefix("glab", "api", f"{MR}/discussions/{DISC}/notes", stdout="{}")
        .on_prefix("glab", "api", f"{MR}/discussions/{DISC}", stdout="{}")
    )


class TestGitLabContract(ProviderContract):
    def make_provider(self):
        return GitLabProvider(runner()), REF


def test_get_pr_maps_fields():
    pr = GitLabProvider(runner()).get_pr(REF)
    assert (pr.base_ref, pr.head_ref, pr.head_sha, pr.state, pr.is_draft, pr.is_cross_repo) == ("main", "feat", "abc123", "open", False, False)
    assert pr.author == "alice" and pr.url.endswith("/-/merge_requests/7")


def test_cross_project_merge_request_is_cross_repo():
    body = json.loads(MR_JSON) | {"source_project_id": 2}
    r = FakeRunner().on("glab", "api", MR, stdout=json.dumps(body))
    assert GitLabProvider(r).get_pr(REF).is_cross_repo is True


def test_changed_files_read_every_page():
    assert GitLabProvider(runner()).list_changed_files(REF) == ["a.py", "b.py"]


def test_refspec_is_the_merge_request_head_ref():
    assert GitLabProvider(runner()).fetch_refspec(REF) == ("+refs/merge-requests/7/head:refs/code-check/pr/7", "refs/code-check/pr/7")


def test_only_diff_discussions_are_threads_and_ids_carry_the_merge_request():
    threads = GitLabProvider(runner()).list_threads(REF)
    assert [t.id for t in threads] == [TID]
    t = threads[0]
    assert (t.path, t.line, t.is_resolved) == ("a.py", 3, False)
    assert [(c.id, c.author) for c in t.comments] == [(101, "me"), (102, "alice")]


def test_thread_is_resolved_only_when_every_resolvable_note_is():
    done = json.dumps([{"id": DISC, "notes": [_note(1, "x", resolved=True), _note(2, "y", resolved=True)]}])
    r = FakeRunner().on("glab", "api", f"{MR}/discussions?per_page=100", "--paginate", stdout=done)
    assert GitLabProvider(r).list_threads(REF)[0].is_resolved is True


def test_our_reviews_are_found_by_marker_and_comments_exclude_them():
    p = GitLabProvider(runner())
    reviews = p.list_reviews(REF)
    assert [(r.author, r.commit_id, r.state) for r in reviews if r.state == "COMMENTED"] == [("me", "abc123", "COMMENTED")]
    assert [(c.author, c.body) for c in p.list_issue_comments(REF)] == [("alice", "thanks")]


def _vote_runner(notes, versions):
    return (FakeRunner().on("glab", "api", f"{MR}/notes?sort=asc&order_by=created_at&per_page=100", "--paginate", stdout=json.dumps(notes))
            .on("glab", "api", f"{MR}/versions", stdout=json.dumps(versions)))


def _vote(body, at, user="alice"):
    return {"id": 9, "system": True, "body": body, "author": {"username": user}, "created_at": at}


def _version(head, at):
    return {"base_commit_sha": "b", "start_commit_sha": "s", "head_commit_sha": head, "created_at": at}


def test_an_approval_is_pinned_to_the_head_that_was_current_when_it_was_given():
    notes = [_vote("approved this merge request", "2026-01-02T00:00:00.000Z")]
    versions = [_version("old", "2026-01-01T00:00:00.000Z"), _version("new", "2026-01-03T00:00:00.000Z")]
    [review] = GitLabProvider(_vote_runner(notes, versions)).list_reviews(REF)
    assert (review.author, review.state, review.commit_id) == ("alice", "APPROVED", "old")


def test_an_approval_after_the_latest_push_counts_for_the_current_head():
    from codecheck.core.precheck import approved_by

    notes = [_vote("approved this merge request", "2026-01-04T00:00:00.000Z")]
    versions = [_version("old", "2026-01-01T00:00:00.000Z"), _version("new", "2026-01-03T00:00:00.000Z")]
    reviews = GitLabProvider(_vote_runner(notes, versions)).list_reviews(REF)
    assert approved_by(reviews, "new") == "alice" and approved_by(reviews, "old") is None


def test_withdrawing_an_approval_cancels_it():
    from codecheck.core.precheck import approved_by

    notes = [_vote("approved this merge request", "2026-01-02T00:00:00.000Z"),
             _vote("unapproved this merge request", "2026-01-02T01:00:00.000Z")]
    reviews = GitLabProvider(_vote_runner(notes, [_version("new", "2026-01-01T00:00:00.000Z")])).list_reviews(REF)
    assert [r.state for r in reviews] == ["APPROVED", "DISMISSED"] and approved_by(reviews, "new") is None


def test_other_system_notes_are_not_reviews_and_cost_no_versions_call():
    r = FakeRunner().on("glab", "api", f"{MR}/notes?sort=asc&order_by=created_at&per_page=100", "--paginate",
                        stdout=json.dumps([_vote("added 1 commit", "2026-01-02T00:00:00.000Z")]))
    assert GitLabProvider(r).list_reviews(REF) == [] and len(r.calls) == 1


def test_a_null_username_does_not_break_listing():
    note = {**_note(1, "hi", type_=None), "author": {"username": None}}
    r = FakeRunner().on("glab", "api", f"{MR}/notes?sort=asc&order_by=created_at&per_page=100", "--paginate", stdout=json.dumps([note]))
    [comment] = GitLabProvider(r).list_issue_comments(REF)
    assert comment.author == "ghost" and comment.is_bot is False


def test_checks_map_job_states_and_ignore_allowed_failures():
    checks = GitLabProvider(runner()).get_checks(REF)
    assert [(c.name, c.state, c.run_id) for c in checks] == [
        ("test", "fail", "9"), ("lint", "pass", "10"), ("opt", "skipped", "11"), ("slow", "pending", "12")]


def test_no_pipeline_means_no_checks():
    r = FakeRunner().on("glab", "api", f"{MR}/pipelines", stdout="[]")
    assert GitLabProvider(r).get_checks(REF) == []


def test_check_log_is_the_job_trace_tail():
    r = FakeRunner().on("glab", "api", "projects/g%2Fs%2Fp/jobs/9/trace", stdout="x" * 30_000 + "END")
    log = GitLabProvider(r).get_check_log(REF, Check("test", "fail", "u", "9"))
    assert len(log) == 20_000 and log.endswith("END")
    assert GitLabProvider(r).get_check_log(REF, Check("test", "fail", "u", None)) == ""


def test_rerun_check_retries_the_job():
    r = FakeRunner().on("glab", "api", "projects/g%2Fs%2Fp/jobs/9/retry", "--method", "POST")
    GitLabProvider(r).rerun_check(REF, Check("test", "fail", "u", "9"))
    assert r.argvs() == [("glab", "api", "projects/g%2Fs%2Fp/jobs/9/retry", "--method", "POST")]
    with pytest.raises(ProviderError, match="no job"):
        GitLabProvider(r).rerun_check(REF, Check("test", "fail", "u", None))
    with pytest.raises(ProviderError):
        GitLabProvider(r).rerun_check(REF, Check("test", "fail", "u", "-1"))


def test_post_review_anchors_each_comment_to_the_matching_version_then_posts_the_marked_summary_last():
    r = runner()
    comments = [ReviewComment("a.py", 3, "RIGHT", "**Warning** bug"), ReviewComment("b.py", 9, "LEFT", "old")]
    result = GitLabProvider(r).post_review(REF, "abc123", "summary", comments, Decision("COMMENT", "r"))
    assert (result.posted, result.degraded) == (2, False)
    posts = [c for c in r.calls if "--input" in c.argv]
    assert [c.argv[2].rsplit("/", 1)[1] for c in posts] == ["discussions", "discussions", "notes"]
    first = json.loads(posts[0].input)
    assert first["body"] == "**Warning** bug"
    assert first["position"] == {"position_type": "text", "base_sha": "b1", "start_sha": "s1", "head_sha": "abc123def",
                                 "new_path": "a.py", "old_path": "a.py", "new_line": 3}
    assert json.loads(posts[1].input)["position"]["old_line"] == 9
    assert json.loads(posts[2].input)["body"] == "summary\n\n<!-- code-check:review:abc123 -->"
    assert not [c for c in r.calls if c.argv[2].endswith("/approve")]  # a COMMENT decision never approves


def test_approve_decision_approves_the_reviewed_head_only():
    r = runner()
    GitLabProvider(r).post_review(REF, "abc123", "ok", [], Decision("APPROVE", "clean"))
    approve = [c for c in r.calls if c.argv[2] == f"{MR}/approve"]
    assert len(approve) == 1 and json.loads(approve[0].input) == {"sha": "abc123"}


def test_a_refused_approval_is_reported_in_the_summary_and_the_review_is_still_recorded():
    r = (FakeRunner().on_prefix("glab", "api", f"{MR}/approve", returncode=1, stderr="401 Unauthorized")
         .on_prefix("glab", "api", f"{MR}/notes", stdout="{}"))
    result = GitLabProvider(r).post_review(REF, "abc123", "summary", [], Decision("APPROVE", "clean"))
    assert result.posted == 0
    order = [c.argv[2].rsplit("/", 1)[1] for c in r.calls]
    assert order == ["approve", "notes"]  # the marker note is last, after the approval attempt
    note = json.loads(r.calls[1].input)["body"]
    assert "could not approve" in note and note.endswith("<!-- code-check:review:abc123 -->")


def test_a_rejected_anchor_is_folded_into_the_summary():
    r = (FakeRunner().on("glab", "api", f"{MR}/versions", stdout=VERSIONS)
         .on_prefix("glab", "api", f"{MR}/discussions", returncode=1, stderr="400 Bad request: position is invalid")
         .on_prefix("glab", "api", f"{MR}/notes", stdout="{}"))
    result = GitLabProvider(r).post_review(REF, "abc123", "summary", [ReviewComment("a.py", 3, "RIGHT", "bug")], Decision("COMMENT", "r"))
    assert (result.posted, result.degraded) == (0, True)
    note = json.loads(next(c for c in r.calls if c.argv[2].endswith("/notes")).input)["body"]
    assert "a.py:3" in note and "bug" in note and note.startswith("summary")
    assert "The host rejected the inline anchors" in note


def test_an_unknown_head_folds_everything_without_posting_discussions():
    r = (FakeRunner().on("glab", "api", f"{MR}/versions", stdout=VERSIONS)
         .on_prefix("glab", "api", f"{MR}/notes", stdout="{}"))
    result = GitLabProvider(r).post_review(REF, "ffff", "summary", [ReviewComment("a.py", 3, "RIGHT", "bug")], Decision("COMMENT", "r"))
    assert (result.posted, result.degraded) == (0, True)
    assert not [c for c in r.calls if c.argv[2].endswith("/discussions")]
    note = json.loads(next(c for c in r.calls if c.argv[2].endswith("/notes")).input)["body"]
    assert "No diff version of this merge request matches the reviewed head" in note
    assert "host rejected" not in note  # nothing was sent, so the host cannot be blamed


def test_a_real_failure_is_not_swallowed():
    r = (FakeRunner().on("glab", "api", f"{MR}/versions", stdout=VERSIONS)
         .on_prefix("glab", "api", f"{MR}/discussions", returncode=1, stderr="401 Unauthorized"))
    with pytest.raises(ProviderError, match="401"):
        GitLabProvider(r).post_review(REF, "abc123", "s", [ReviewComment("a.py", 3, "RIGHT", "bug")], Decision("COMMENT", "r"))


def test_reply_and_resolve_use_the_discussion_in_the_thread_id():
    r = runner()
    p = GitLabProvider(r)
    thread = Thread(TID, "a.py", 3, False, False, (ThreadComment(101, "alice", "x", "t"),))
    p.reply(REF, thread, "done")
    p.resolve(thread)
    reply, resolve = r.calls
    assert reply.argv[2] == f"{MR}/discussions/{DISC}/notes" and json.loads(reply.input) == {"body": "done"}
    assert resolve.argv[2] == f"{MR}/discussions/{DISC}" and "PUT" in resolve.argv and json.loads(resolve.input) == {"resolved": True}


def test_reply_to_a_thread_of_another_merge_request_is_refused():
    thread = Thread(f"g/s/p:8:{DISC}", "a.py", 3, False, False, ())
    with pytest.raises(ProviderError, match="does not belong"):
        GitLabProvider(runner()).reply(REF, thread, "x")


def test_request_review_keeps_existing_reviewers_and_label_is_additive():
    r = runner()
    p = GitLabProvider(r)
    p.request_review(REF, ["alice"])
    assert json.loads(next(c for c in r.calls if "PUT" in c.argv).input) == {"reviewer_ids": [5, 9]}
    p.add_label(REF, "code-check")
    assert json.loads([c for c in r.calls if "PUT" in c.argv][1].input) == {"add_labels": "code-check"}


def test_list_prs_reads_the_namespace_from_the_reference():
    body = json.dumps([{"iid": 4, "references": {"full": "g/s/p!4"}}, {"iid": 5, "references": {}}])
    r = (FakeRunner().on("glab", "api", "user", stdout='{"username": "me"}')
         .on("glab", "api", "merge_requests?state=opened&scope=all&per_page=50&reviewer_username=me", stdout=body))
    assert GitLabProvider(r).list_prs("review-requested", []) == [PRRef("g/s/p", 4)]
    with pytest.raises(ProviderError, match="unknown scope"):
        GitLabProvider(r).list_prs("nope", [])


def test_list_prs_open_scope_is_newest_first_and_needs_a_project():
    body = json.dumps([{"iid": 8, "references": {"full": "g/s/p!8"}}])
    r = FakeRunner().on("glab", "api", "projects/g%2Fs%2Fp/merge_requests?state=opened&scope=all&per_page=50&order_by=created_at&sort=desc", stdout=body)
    assert GitLabProvider(r).list_prs("open", ["g/s/p"]) == [PRRef("g/s/p", 8)]
    with pytest.raises(ProviderError, match="needs a project"):
        GitLabProvider(r).list_prs("open", [])


def test_login_override_skips_the_user_call():
    assert GitLabProvider(FakeRunner(), login="bot").whoami() == "bot"


def test_repo_is_private_follows_visibility():
    r = FakeRunner().on("glab", "api", "projects/g%2Fp", stdout='{"visibility": "public"}')
    assert GitLabProvider(r).repo_is_private("g/p") is False
    r = FakeRunner().on("glab", "api", "projects/g%2Fp", stdout='{"visibility": "internal"}')
    assert GitLabProvider(r).repo_is_private("g/p") is True
    r = FakeRunner().on("glab", "api", "projects/g%2Fp", returncode=1, stderr="404")
    assert GitLabProvider(r).repo_is_private("g/p") is None


@pytest.mark.parametrize("url,repo", [
    ("https://gitlab.com/g/p.git", "g/p"),
    ("https://gitlab.com/g/s/p", "g/s/p"),
    ("https://oauth2:tok@gitlab.com/g/s/p.git/", "g/s/p"),
    ("git@gitlab.com:g/s/p.git", "g/s/p"),
    ("ssh://git@gitlab.com:2222/g/p.git", "g/p"),
])
def test_remote_urls(url, repo):
    assert parse_remote_url(url) == repo and remote_host(url) == "gitlab.com"


@pytest.mark.parametrize("url", ["", "::: not a url", "https://gitlab.com/justone", "https://gitlab.com/-x/y"])
def test_bad_remote_urls_give_none(url):
    assert parse_remote_url(url) is None


@pytest.mark.parametrize("bad", ["-x/y", "a", "a/../b", "a/./b", "a//b", "a/b c", "a/b\n", ""])
def test_repository_names_are_validated_before_they_reach_glab(bad):
    r = FakeRunner()
    with pytest.raises(ProviderError):
        GitLabProvider(r).get_pr(PRRef(bad, 1))
    assert r.calls == []


def test_clone_argv_guards_the_destination():
    assert GitLabProvider(FakeRunner()).clone_argv("g/s/p", "/d") == ["glab", "repo", "clone", "g/s/p", "/d"]
    with pytest.raises(ProviderError):
        GitLabProvider(FakeRunner()).clone_argv("g/p", "-d")


def test_numbers_labels_users_and_commits_are_validated():
    p = GitLabProvider(FakeRunner())
    for call in (lambda: p.get_pr(PRRef("g/p", True)), lambda: p.add_label(REF, "-x"), lambda: p.request_review(REF, ["a b"]),
                 lambda: p.post_review(REF, "--bad", "b", [], Decision("COMMENT", "r"))):
        with pytest.raises(ProviderError):
            call()


def test_thread_id_roundtrip_and_rejection():
    assert split_thread_id(thread_id("g/s/p", 7, DISC)) == ("g/s/p", 7, DISC)
    for bad in ("", "T1", "g/p:x:" + DISC, "g/p:7:ZZ", "-x/y:7:" + DISC):
        with pytest.raises(ProviderError):
            split_thread_id(bad)


def test_docs_flattens_concatenated_pages():
    assert _docs('[1, 2]\n[3]\n{"a": 1}') == [1, 2, 3, {"a": 1}]
    assert _docs("") == []
