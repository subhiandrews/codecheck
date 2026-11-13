import json
import stat
from dataclasses import replace

import pytest
from clihelp import call, finish_review, pr_setup, run_pr_context

from codecheck.cli import build_parser
from codecheck.errors import CodeCheckError
from codecheck.evals.golden import check_case, load_case
from codecheck.models import Thread, ThreadComment
from codecheck.shadow.promote import draft_case, finalize_case

pytestmark = pytest.mark.usefixtures("git_env")

FINDING = {"file": "app.py", "line": 2, "severity": "warning", "category": "bug", "message": "y is unused"}
FAR = {"file": "app.py", "line": 500, "severity": "suggestion", "category": "style", "message": "far away"}


@pytest.fixture(autouse=True)
def _fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # never read the real ~/.claude


def own(tid, fid, *, resolved=False, replies=()):
    body = f"**Warning**: finding\n\n<!-- code-check:finding:{fid} -->"
    comments = (ThreadComment(1, "me", body, "2026-01-01T00:00:00Z"),
                *(ThreadComment(i + 2, "bob", r, "2026-01-02T00:00:00Z") for i, r in enumerate(replies)))
    return Thread(tid, "app.py", 2, resolved, False, comments)


def human(tid, line, body):
    return Thread(tid, "app.py", line, False, False, (ThreadComment(1, "bob", body, "2026-01-02T00:00:00Z"),))


def review_run(tmp_path, *, handoff=True, private=False, aggregated=None, threads=None):
    """A posted PR review made through the real commands, with the PR's threads collected by `handoff`."""
    ctx, provider, pr = pr_setup(tmp_path)
    provider.private = private
    if handoff:
        ctx.config = replace(ctx.config, shadow=replace(ctx.config.shadow, handoff=True))
        ctx.env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}
    out = run_pr_context(ctx, pr)
    finish_review(ctx, out, {"skeptic": [FINDING]}, aggregated or {"findings": [FINDING, FAR], "dropped": []})
    call(ctx, "ingest", "--run", out["run_id"])
    _code, posted = call(ctx, "post", "--run", out["run_id"])
    assert posted["posted"], posted
    ids = {r["line"]: r["id"] for r in ctx.shadow.read(out["run_id"]) if r.get("type") == "finding"}
    provider.threads[pr.ref] = threads(ids) if threads else []
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is handoff, res
    if not handoff:  # collect what the hand-off would have, so only the raw outputs are missing
        call(ctx, "shadow", "collect", "--pr", str(pr.ref))
    return ctx, pr, out, ids


def labelled(draft: dict, status="confirmed") -> dict:
    return {**draft, "label_status": status, "labels": [{**lab, "label": "expected"} for lab in draft["labels"]]}


def write(path, data):
    path.write_text(json.dumps(data))
    return path


def test_draft_requires_raw_outputs(tmp_path):
    ctx, _pr, out, _ids = review_run(tmp_path, handoff=False)
    with pytest.raises(CodeCheckError) as e:
        draft_case(ctx.shadow, out["run_id"])
    assert str(e.value) == (f"no raw outputs recorded for {out['run_id']}: "
                            "only runs made with shadow.handoff can be promoted")


def test_draft_requires_a_diff(tmp_path):
    ctx, _pr, out, _ids = review_run(tmp_path)
    (ctx.shadow.root / f"{out['run_id']}.diff").unlink()
    with pytest.raises(CodeCheckError, match="diff"):
        draft_case(ctx.shadow, out["run_id"])


def test_draft_labels_unlabelled_by_default(tmp_path):
    ctx, pr, out, ids = review_run(tmp_path)
    case = draft_case(ctx.shadow, out["run_id"])
    assert case["label_status"] == "auto" and case["version"] == 1
    assert case["repo"] == "o/r" and case["pr"] == pr.ref.number and case["head_sha"] == pr.head_sha
    assert case["case_id"] == f"o-r-{pr.ref.number}-{pr.head_sha[:8]}"
    assert case["repo_private"] is False and case["misses"] == []
    assert {lab["id"]: lab["label"] for lab in case["labels"]} == {ids[2]: "unlabelled", ids[500]: "unlabelled"}
    assert [(f["line"], f["severity"], f["inline"]) for f in case["expected_posted"]] == [(2, "warning", True), (500, "suggestion", False)]
    assert case["expected_decision"] == "COMMENT"
    assert case["reviewer_outputs"]["skeptic"][0]["line"] == 2 and "diff --git" in case["diff"]
    assert "signals" in case  # null here: no transcript was found for the run


def test_draft_labels_from_human_feedback(tmp_path):
    def threads(ids):
        return [own("T1", ids[2], resolved=True), own("T2", ids[500], replies=("this is wrong",)),
                human("T3", 120, "you missed the cache")]

    ctx, _pr, out, ids = review_run(tmp_path, threads=threads)
    case = draft_case(ctx.shadow, out["run_id"])
    assert {lab["id"]: lab["label"] for lab in case["labels"]} == {ids[2]: "expected", ids[500]: "not_expected"}
    assert case["misses"] == [{"path": "app.py", "line": 120, "author": "bob", "body": "you missed the cache"}]


def test_draft_counts_a_matching_human_comment_as_expected(tmp_path):
    ctx, _pr, out, ids = review_run(tmp_path, threads=lambda ids: [human("T1", 3, "y is never read")])
    case = draft_case(ctx.shadow, out["run_id"])
    assert {lab["id"]: lab["label"] for lab in case["labels"]} == {ids[2]: "expected", ids[500]: "unlabelled"}
    assert case["misses"] == []


def test_finalize_refuses_unconfirmed(tmp_path):
    ctx, _pr, out, _ids = review_run(tmp_path)
    draft = write(tmp_path / "draft.json", labelled(draft_case(ctx.shadow, out["run_id"]), status="auto"))
    with pytest.raises(CodeCheckError, match="confirmed"):
        finalize_case(draft, tmp_path / "golden", allow_private=False)
    assert not (tmp_path / "golden").exists()


def test_finalize_refuses_unlabelled(tmp_path):
    ctx, _pr, out, _ids = review_run(tmp_path)
    draft = write(tmp_path / "draft.json", {**draft_case(ctx.shadow, out["run_id"]), "label_status": "confirmed"})
    with pytest.raises(CodeCheckError, match="unlabelled"):
        finalize_case(draft, tmp_path / "golden", allow_private=False)


def test_finalize_refuses_private_or_unknown_repo(tmp_path):
    ctx, _pr, out, _ids = review_run(tmp_path)
    case = labelled(draft_case(ctx.shadow, out["run_id"]))
    for private in (True, None):
        draft = write(tmp_path / "draft.json", {**case, "repo_private": private})
        with pytest.raises(CodeCheckError, match="private"):
            finalize_case(draft, tmp_path / "golden", allow_private=False)
    assert finalize_case(draft, tmp_path / "golden", allow_private=True).exists()


def test_finalize_refuses_a_case_that_does_not_replay(tmp_path):
    ctx, _pr, out, _ids = review_run(tmp_path)
    draft = write(tmp_path / "draft.json", {**labelled(draft_case(ctx.shadow, out["run_id"])), "expected_decision": "APPROVE"})
    with pytest.raises(CodeCheckError, match="decision"):
        finalize_case(draft, tmp_path / "golden", allow_private=False)


def test_finalize_writes_case_that_replays(tmp_path):
    ctx, pr, out, ids = review_run(tmp_path)
    case = draft_case(ctx.shadow, out["run_id"])
    case["label_status"] = "confirmed"
    case["labels"] = [{**lab, "label": "expected" if lab["id"] == ids[2] else "not_expected"} for lab in case["labels"]]
    path = finalize_case(write(tmp_path / "draft.json", case), tmp_path / "golden", allow_private=False)
    assert path == tmp_path / "golden" / f"o-r-{pr.ref.number}-{pr.head_sha[:8]}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert check_case(load_case(path)) == []


def test_finalize_scrubs(tmp_path):
    ctx, _pr, out, _ids = review_run(tmp_path)
    case = labelled(draft_case(ctx.shadow, out["run_id"]))
    secret = "ghp_" + "Q7" * 20
    case["aggregator"]["findings"][1]["message"] = f"token {secret} leaked"
    case["misses"] = [{"path": "app.py", "line": 9, "author": "bob", "body": f"see {secret}"}]
    path = finalize_case(write(tmp_path / "draft.json", case), tmp_path / "golden", allow_private=False)
    assert secret not in path.read_text() and check_case(load_case(path)) == []


def test_finalize_never_writes_through_a_symlink(tmp_path):
    ctx, pr, out, _ids = review_run(tmp_path)
    draft = write(tmp_path / "draft.json", labelled(draft_case(ctx.shadow, out["run_id"])))
    dest = tmp_path / "golden"
    dest.mkdir()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("keep")
    (dest / f"o-r-{pr.ref.number}-{pr.head_sha[:8]}.json").symlink_to(elsewhere)
    with pytest.raises(CodeCheckError, match="symlink"):
        finalize_case(draft, dest, allow_private=False)
    assert elsewhere.read_text() == "keep"


def test_absolute_aggregator_paths_are_stored_relative(tmp_path):
    # A real aggregator may write paths under the run's worktree; `post` strips the root, and so must promotion.
    ctx, provider, pr = pr_setup(tmp_path)
    provider.private = False
    ctx.config = replace(ctx.config, shadow=replace(ctx.config.shadow, handoff=True))
    ctx.env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}
    out = run_pr_context(ctx, pr)
    root = out["root"]
    aggregated = {"findings": [{**FINDING, "file": f"{root}/app.py"}, {**FAR, "file": f"{root}/app.py"}], "dropped": []}
    finish_review(ctx, out, {"skeptic": [{**FINDING, "file": f"{root}/app.py"}]}, aggregated)
    call(ctx, "ingest", "--run", out["run_id"])
    assert call(ctx, "post", "--run", out["run_id"])[1]["posted"]
    call(ctx, "handoff", "--run", out["run_id"])
    case = draft_case(ctx.shadow, out["run_id"])
    files = ([f["file"] for f in case["aggregator"]["findings"]] + [f["file"] for f in case["expected_posted"]]
             + [lab["file"] for lab in case["labels"]] + [f["file"] for f in case["reviewer_outputs"]["skeptic"]])
    assert files and set(files) == {"app.py"}
    assert root not in json.dumps(case)
    path = finalize_case(write(tmp_path / "draft.json", labelled(case)), tmp_path / "golden", allow_private=False)
    assert check_case(load_case(path)) == []


def test_promote_is_hidden_from_shadow_help_but_parses():
    parser = build_parser()
    text = parser._subparsers._group_actions[0].choices["shadow"].format_help()
    assert "promote" not in text and "SUPPRESS" not in text and "export-golden" in text
    args = parser.parse_args(["shadow", "promote", "--run", "abcdef012345", "--out", "d.json"])
    assert (args.shadow_cmd, args.run, args.out) == ("promote", "abcdef012345", "d.json")
    args = parser.parse_args(["shadow", "promote", "--finalize", "d.json", "--allow-private", "--dest", "g"])
    assert (args.finalize, args.allow_private, args.dest) == ("d.json", True, "g")
    with pytest.raises(SystemExit):
        parser.parse_args(["shadow", "promote", "--run", "abcdef012345", "--finalize", "d.json"])


def test_promote_cli_drafts_then_finalizes(tmp_path):
    ctx, pr, out, _ids = review_run(tmp_path)
    code, res = call(ctx, "shadow", "promote", "--run", out["run_id"], "--out", "drafts/case.json")
    draft = ctx.cwd / "drafts" / "case.json"
    assert code == 0 and res["draft"] == str(draft) and res["label_status"] == "auto", res
    assert [lab["label"] for lab in res["labels"]] == ["unlabelled", "unlabelled"]
    assert stat.S_IMODE(draft.stat().st_mode) == 0o600
    code, res = call(ctx, "shadow", "promote", "--finalize", str(draft), "--dest", str(tmp_path / "golden"))
    assert code != 0 and "confirmed" in res["error"]
    write(draft, labelled(json.loads(draft.read_text())))
    code, res = call(ctx, "shadow", "promote", "--finalize", str(draft), "--dest", str(tmp_path / "golden"))
    assert code == 0 and res["case"] == str(tmp_path / "golden" / f"o-r-{pr.ref.number}-{pr.head_sha[:8]}.json"), res


def test_promote_cli_run_needs_out(tmp_path):
    ctx, _pr, out, _ids = review_run(tmp_path)
    code, res = call(ctx, "shadow", "promote", "--run", out["run_id"])
    assert code != 0 and "--out" in res["error"]
