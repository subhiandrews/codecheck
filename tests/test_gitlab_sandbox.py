"""scripts/gitlab-sandbox/sandbox.sh, run against a fake `glab` and a local bare repository."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = ROOT / "scripts" / "gitlab-sandbox"
SCRIPT = SANDBOX / "sandbox.sh"
PROJECT = "alice/code-check-sandbox"
BRANCHES = ["feature/discount", "feature/clean-docs", "feature/report-export", "fix/failing-ci"]

FAKE_GLAB = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Answers the `glab api` calls sandbox.sh makes, and records every call."""
    import json, os, sys

    args = sys.argv[1:]
    assert args[0] == "api", args
    endpoint = args[1]
    method = args[args.index("--method") + 1] if "--method" in args else "GET"
    body = sys.stdin.read() if "--input" in args else ""
    state = os.environ["FAKE_GLAB_STATE"]
    with open(os.path.join(state, "calls.jsonl"), "a") as fh:
        fh.write(json.dumps({"method": method, "endpoint": endpoint, "body": body}) + "\\n")
    created = os.path.join(state, "created")
    pid = "alice%2Fcode-check-sandbox"
    out = {}
    if method == "GET":
        if endpoint == "user":
            out = {"username": os.environ.get("FAKE_GLAB_USER", "alice")}
        elif endpoint.startswith("namespaces/"):
            out = {"id": 11}
        elif endpoint == "projects/" + pid:
            if not os.path.exists(created):
                sys.exit(1)
            out = {"visibility": "public", "http_url_to_repo": "unused", "ssh_url_to_repo": "unused"}
        elif endpoint.startswith("users?username="):
            out = [{"id": 42}]
        elif "source_branch=" in endpoint:
            out = [{"iid": 5}]
        elif endpoint.endswith("/versions"):
            out = [{"base_commit_sha": "base1", "start_commit_sha": "start1", "head_commit_sha": "head1"}]
        elif endpoint.endswith("/pipelines"):
            out = [{"id": 9}]
        elif endpoint.endswith("merge_requests?state=opened"):
            out = [{"iid": 5}]
        elif "merge_requests?state=all" in endpoint:
            out = [{"iid": 5, "state": "opened", "author": {"username": "alice"}, "title": "t"}]
        elif "--paginate" in args:
            out = []
    elif method == "POST":
        if endpoint == "projects":
            open(created, "w").close()
        elif endpoint.endswith("/merge_requests"):
            n = len(open(os.path.join(state, "calls.jsonl")).read().splitlines())
            out = {"iid": n, "web_url": "https://gitlab.example/mr/%d" % n}
    print(json.dumps(out))
''')


def _env(tmp_path: Path, bare: Path | None = None, **extra) -> dict:
    state = tmp_path / "state"
    bin_dir = tmp_path / "bin"
    state.mkdir(exist_ok=True)
    bin_dir.mkdir(exist_ok=True)
    glab = bin_dir / "glab"
    glab.write_text(FAKE_GLAB)
    glab.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GLAB_STATE": str(state),
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    }
    if bare is not None:
        env["SANDBOX_REMOTE_URL"] = str(bare)
    env.update(extra)
    return env


def run(env: dict, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False)
    if check:
        assert res.returncode == 0, res.stderr
    return res


def calls(env: dict) -> list[dict]:
    path = Path(env["FAKE_GLAB_STATE"]) / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture(scope="module")
def created(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sandbox")
    bare = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    env = _env(tmp, bare)
    run(env, "create")
    clone = tmp / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True, env=env)
    return env, bare, clone


def checkout(clone: Path, ref: str) -> None:
    git(clone, "checkout", "-q", "--detach", ref)


def test_create_makes_a_public_project_with_issues_in_order(created):
    env, _, _ = created
    made = [c for c in calls(env) if c["method"] == "POST"]
    project = json.loads(made[0]["body"])
    assert made[0]["endpoint"] == "projects"
    assert (project["name"], project["visibility"], project["default_branch"], project["namespace_id"]) == (
        "code-check-sandbox", "public", "main", "11")
    issues = [json.loads(c["body"])["title"] for c in made if c["endpoint"].endswith("/issues")]
    assert issues == ["total_between leaves out the end index", "Add CSV export of totals"]


def test_create_pushes_main_and_the_four_branches_without_template_suffixes(created):
    _, bare, clone = created
    refs = git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    assert sorted(refs) == sorted(["main", *BRANCHES])
    checkout(clone, "origin/main")
    files = {str(p.relative_to(clone)) for p in clone.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert {"README.md", "LICENSE", ".gitlab-ci.yml", "pyproject.toml", "sandbox/__init__.py", "sandbox/stats.py",
            "tests/test_stats.py"} <= files
    assert not [f for f in files if f.endswith((".tmpl", "MR.md", "comments.json"))]


def test_each_branch_commit_message_is_its_merge_request_title(created):
    _, _, clone = created
    for branch in BRANCHES:
        directory = branch.replace("/", "__")
        title = (SANDBOX / "seed" / "branches" / directory / "MR.md").read_text().splitlines()[0]
        assert git(clone, "log", "-1", "--format=%s", f"origin/{branch}").strip() == title


def test_a_second_create_refuses_because_the_project_exists(created):
    env, _, _ = created
    res = run(env, "create", check=False)
    assert res.returncode != 0 and "already exists" in res.stderr


@pytest.mark.parametrize("ref,passes", [
    ("origin/main", True), ("origin/feature/report-export", True), ("origin/feature/clean-docs", True),
    ("origin/feature/discount", True), ("origin/fix/failing-ci", False),
])
def test_the_seeded_project_is_green_or_red_as_the_scenarios_need(created, ref, passes):
    _, _, clone = created
    checkout(clone, ref)
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=clone, capture_output=True, text=True,
                         check=False)
    assert (res.returncode == 0) is passes, res.stdout[-600:]


def test_the_planted_bug_is_in_main(created):
    _, _, clone = created
    checkout(clone, "origin/main")
    import importlib.util

    spec = importlib.util.spec_from_file_location("sandbox_stats", clone / "sandbox" / "stats.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.total_between([1, 2, 3], 0, 2) == 3  # issue #1: the docstring promises 6


def test_comment_line_numbers_point_at_the_lines_they_describe():
    report = (SANDBOX / "seed/branches/feature__report-export/sandbox/report.py.tmpl").read_text().splitlines()
    comments = json.loads((SANDBOX / "seed/branches/feature__report-export/comments.json").read_text())
    wanted = {7: 'open(path, "w")', 8: "csv.writer", 10: "sum(values)"}
    assert {c["line"] for c in comments} == set(wanted)
    for line, text in wanted.items():
        assert text in report[line - 1]
    stats = (SANDBOX / "seed/branches/fix__failing-ci/sandbox/stats.py.tmpl").read_text().splitlines()
    [comment] = json.loads((SANDBOX / "seed/branches/fix__failing-ci/comments.json").read_text())
    assert "//" in stats[comment["line"] - 1]


def test_open_mrs_uses_the_titles_and_branches(tmp_path):
    env = _env(tmp_path)
    run(env, "open-mrs", "owner")
    run(env, "open-mrs", "reviewer")
    bodies = [json.loads(c["body"]) for c in calls(env) if c["endpoint"].endswith("/merge_requests") and c["method"] == "POST"]
    assert [b["source_branch"] for b in bodies] == ["feature/report-export", "fix/failing-ci", "feature/discount", "feature/clean-docs"]
    assert all(b["target_branch"] == "main" and b["title"] for b in bodies)
    assert "Closes #2" in bodies[0]["description"]
    assert run(env, "open-mrs", "nobody", check=False).returncode != 0


def test_comment_as_reviewer_posts_positioned_discussions_on_the_owners_merge_requests(tmp_path):
    env = _env(tmp_path)
    res = run(env, "comment-as-reviewer")
    posted = [c for c in calls(env) if c["method"] == "POST" and c["endpoint"].endswith("/discussions")]
    assert len(posted) == 4 and "!5" in res.stdout
    first = json.loads(posted[0]["body"])
    assert first["position"] == {"position_type": "text", "base_sha": "base1", "start_sha": "start1", "head_sha": "head1",
                                 "new_path": "sandbox/report.py", "old_path": "sandbox/report.py", "new_line": 7}
    assert [json.loads(c["body"])["position"]["new_line"] for c in posted] == [7, 8, 10, 13]


def test_add_member_resolves_the_user_first(tmp_path):
    env = _env(tmp_path)
    run(env, "add-member", "bob")
    member = next(c for c in calls(env) if c["endpoint"].endswith("/members"))
    assert json.loads(member["body"]) == {"user_id": "42", "access_level": "30"}


def test_a_second_account_targets_the_owners_project(tmp_path):
    env = _env(tmp_path, SANDBOX_PROJECT=PROJECT, FAKE_GLAB_USER="bob")
    run(env, "open-mrs", "reviewer")
    assert all("alice%2Fcode-check-sandbox" in c["endpoint"] for c in calls(env))


def test_teardown_needs_the_exact_project_path(tmp_path):
    env = _env(tmp_path)
    refused = run({**env, "SANDBOX_CONFIRM_DELETE": "alice/other"}, "teardown", check=False)
    assert refused.returncode != 0 and not [c for c in calls(env) if c["method"] == "DELETE"]
    run({**env, "SANDBOX_CONFIRM_DELETE": PROJECT}, "teardown")
    assert [c["endpoint"] for c in calls(env) if c["method"] == "DELETE"] == ["projects/alice%2Fcode-check-sandbox"]


def test_capture_fixtures_saves_every_response_the_provider_reads(tmp_path):
    env = _env(tmp_path)
    (Path(env["FAKE_GLAB_STATE"]) / "created").touch()  # the project already exists
    out = tmp_path / "fixtures"
    run(env, "capture-fixtures", str(out), "5")
    assert {p.name for p in out.iterdir()} == {"user.json", "project.json", "mr.json", "diffs.json", "discussions.json",
                                                "notes.json", "versions.json", "pipelines.json", "jobs.json"}


def test_help_and_unknown_commands():
    assert "create" in run({**os.environ}, "help").stdout
    assert run({**os.environ}, "nonsense", check=False).returncode != 0
