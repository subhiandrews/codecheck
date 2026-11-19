"""Golden cases hold the reviewed PR's diff, which is someone's code. A case may be committed to the public set only
when its repository's license allows it, and every source repository is credited in THIRD_PARTY.md.
"""

import json
import re
from pathlib import Path

import pytest
from fakes import FakeRunner
from test_promote import call, labelled, review_run, write

from codecheck.cli import build_parser
from codecheck.config import DEFAULT_LICENSES, GoldenConfig, load_config
from codecheck.errors import CodeCheckError, ConfigError
from codecheck.evals.golden import DEFAULT_CASES, load_cases
from codecheck.providers.github import GitHubProvider
from codecheck.providers.gitlab import GitLabProvider
from codecheck.shadow.promote import draft_case, finalize_case

pytestmark = pytest.mark.usefixtures("git_env")

ROOT = Path(__file__).resolve().parents[1]


# -- where the license comes from ------------------------------------------------------------------------------


def test_github_license_is_the_lowercased_spdx_id_or_none():
    argv = ("gh", "repo", "view", "o/r", "--json", "licenseInfo", "--jq", ".licenseInfo.key")
    assert GitHubProvider(FakeRunner().on(*argv, stdout="apache-2.0\n")).repo_license("o/r") == "apache-2.0"
    for unknown in ("other\n", "NOASSERTION\n", "null\n", "\n", "not a license!\n"):
        assert GitHubProvider(FakeRunner().on(*argv, stdout=unknown)).repo_license("o/r") is None
    assert GitHubProvider(FakeRunner().on(*argv, returncode=1, stderr="boom")).repo_license("o/r") is None
    assert GitHubProvider(FakeRunner()).repo_license("--bad/x") is None


def test_gitlab_license_is_the_project_license_key_or_none():
    argv = ("glab", "api", "projects/g%2Fp?license=true")
    assert GitLabProvider(FakeRunner().on(*argv, stdout='{"license": {"key": "MIT"}}')).repo_license("g/p") == "mit"
    assert GitLabProvider(FakeRunner().on(*argv, stdout='{"license": null}')).repo_license("g/p") is None
    assert GitLabProvider(FakeRunner().on(*argv, returncode=1, stderr="404")).repo_license("g/p") is None


# -- recording it ------------------------------------------------------------------------------------------------


def test_a_handoff_run_records_the_license_and_the_draft_carries_it(tmp_path):
    ctx, _pr, out, _ids = review_run(tmp_path)
    run = next(r for r in ctx.shadow.read(out["run_id"]) if r.get("type") == "run")
    assert run["repo_license"] == "mit"
    assert draft_case(ctx.shadow, out["run_id"])["repo_license"] == "mit"


# -- the gate -------------------------------------------------------------------------------------------------


def draft_with(tmp_path, license_value):
    ctx, _pr, out, _ids = review_run(tmp_path)
    case = labelled(draft_case(ctx.shadow, out["run_id"]))
    return write(tmp_path / "draft.json", {**case, "repo_license": license_value})


@pytest.mark.parametrize("value", ["gpl-3.0", "agpl-3.0", "mpl-2.0", "proprietary", None, ""])
def test_finalize_refuses_a_license_that_is_not_allowed(tmp_path, value):
    draft = draft_with(tmp_path, value)
    with pytest.raises(CodeCheckError, match="license") as e:
        finalize_case(draft, tmp_path / "golden", allow_private=False)
    assert not (tmp_path / "golden").exists() or not list((tmp_path / "golden").iterdir())
    assert "--allow-license" in str(e.value)


@pytest.mark.parametrize("value", ["mit", "MIT", "Apache-2.0", "bsd-3-clause"])
def test_finalize_accepts_a_permissive_license_in_any_case(tmp_path, value):
    assert finalize_case(draft_with(tmp_path, value), tmp_path / "golden", allow_private=False).exists()


def test_finalize_uses_the_configured_allow_list_and_the_explicit_override(tmp_path):
    draft = draft_with(tmp_path, "mpl-2.0")
    assert finalize_case(draft, tmp_path / "g1", allow_private=False, allowed_licenses=("mpl-2.0",)).exists()
    assert finalize_case(draft, tmp_path / "g2", allow_private=False, allow_license=True).exists()


def test_a_missing_license_key_is_refused_like_an_unknown_one(tmp_path):
    ctx, _pr, out, _ids = review_run(tmp_path)
    case = labelled(draft_case(ctx.shadow, out["run_id"]))
    case.pop("repo_license")
    with pytest.raises(CodeCheckError, match="license"):
        finalize_case(write(tmp_path / "draft.json", case), tmp_path / "golden", allow_private=False)


def test_the_cli_has_allow_license():
    args = build_parser().parse_args(["shadow", "promote", "--finalize", "d.json", "--allow-license"])
    assert args.allow_license is True


# -- config ---------------------------------------------------------------------------------------------------


def test_golden_licenses_default_to_permissive_ones_and_a_repo_cannot_widen_them(tmp_path):
    cfg = load_config(tmp_path / "repo", tmp_path / "home", {})
    assert cfg.golden == GoldenConfig() and set(cfg.golden.licenses) == set(DEFAULT_LICENSES)
    assert {"mit", "apache-2.0", "bsd-3-clause"} <= set(DEFAULT_LICENSES) and "gpl-3.0" not in DEFAULT_LICENSES
    (tmp_path / "r2").mkdir()
    (tmp_path / "r2/.code-check.toml").write_text('[golden]\nlicenses = ["gpl-3.0"]\n')
    with pytest.raises(ConfigError, match="golden"):
        load_config(tmp_path / "r2", tmp_path / "home", {})
    home = tmp_path / "home2"
    (home / ".config/code-check").mkdir(parents=True)
    (home / ".config/code-check/config.toml").write_text('[golden]\nlicenses = ["MIT", "mpl-2.0"]\n')
    assert load_config(tmp_path / "repo", home, {}).golden.licenses == ("mit", "mpl-2.0")


# -- what is committed ------------------------------------------------------------------------------------------


SYNTHETIC = {"seed-pipeline"}  # written for this project, not taken from any repository


def test_every_committed_real_case_has_an_allowed_license_and_is_credited():
    table = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8")
    credited = {m.lower() for m in re.findall(r"^\| \[([^\]]+)\]\(", table, re.MULTILINE)}
    for case in load_cases(DEFAULT_CASES):
        if case["case_id"] in SYNTHETIC:
            continue
        assert case.get("repo_license") in DEFAULT_LICENSES, f"{case['case_id']}: license {case.get('repo_license')!r}"
        assert case["repo"].lower() in credited, f"add {case['repo']} to the THIRD_PARTY.md table"


def test_the_license_texts_the_credits_depend_on_are_shipped():
    mit = (ROOT / "licenses" / "MIT-tokio-rs-tokio.txt").read_text(encoding="utf-8")
    assert "Copyright" in mit and "Permission is hereby granted" in mit
    assert "Apache License" in (ROOT / "licenses" / "Apache-2.0-google-guava.txt").read_text(encoding="utf-8")


def test_a_draft_cannot_widen_the_license_the_run_recorded(tmp_path):
    draft = draft_with(tmp_path, "mit")  # the draft says mit; pretend the run recorded gpl-3.0
    with pytest.raises(CodeCheckError, match="differs from the license recorded"):
        finalize_case(draft, tmp_path / "golden", allow_private=False, recorded_license="gpl-3.0")
    assert finalize_case(draft, tmp_path / "golden", allow_private=False, recorded_license="MIT").exists()


def test_a_synthetic_origin_in_a_draft_does_not_skip_the_gate(tmp_path):
    ctx, _pr, out, _ids = review_run(tmp_path)
    case = labelled(draft_case(ctx.shadow, out["run_id"]))
    draft = write(tmp_path / "draft.json", {**case, "repo_license": "gpl-3.0", "origin": "synthetic"})
    with pytest.raises(CodeCheckError, match="license"):
        finalize_case(draft, tmp_path / "golden", allow_private=False)


def test_a_bare_string_for_licenses_is_a_config_error(tmp_path):
    (tmp_path / "r").mkdir()
    home = tmp_path / "home"
    (home / ".config/code-check").mkdir(parents=True)
    (home / ".config/code-check/config.toml").write_text('[golden]\nlicenses = "mit"\n')
    with pytest.raises(ConfigError, match="golden.licenses"):
        load_config(tmp_path / "r", home, {})


def test_committed_case_files_are_valid_json_objects():
    for path in sorted(DEFAULT_CASES.glob("*.json")):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict), path.name


# -- the CLI reads the recorded license back -------------------------------------------------------------------


def cli_finalize(ctx, tmp_path, draft, *extra):
    return call(ctx, "shadow", "promote", "--finalize", str(draft), "--dest", str(tmp_path / "golden"), *extra)


def test_cli_refuses_a_draft_whose_license_was_edited_away_from_the_runs(tmp_path):
    ctx, _pr, out, _ids = review_run(tmp_path)
    case = labelled(draft_case(ctx.shadow, out["run_id"]))
    draft = write(tmp_path / "draft.json", {**case, "repo_license": "isc"})  # allowed, but not what the run recorded
    code, res = cli_finalize(ctx, tmp_path, draft)
    assert code != 0 and "differs from the license recorded" in res["error"], res
    assert cli_finalize(ctx, tmp_path, write(tmp_path / "ok.json", case))[0] == 0


def test_cli_treats_a_recorded_unknown_license_as_authoritative(tmp_path):
    ctx, _pr, out, _ids = review_run(tmp_path, license=None)
    case = labelled(draft_case(ctx.shadow, out["run_id"]))
    draft = write(tmp_path / "draft.json", {**case, "repo_license": "mit"})
    code, res = cli_finalize(ctx, tmp_path, draft)
    assert code != 0 and "differs from the license recorded" in res["error"], res


@pytest.mark.parametrize("edit", [{"run_id": None}, {"run_id": "not-a-run"}, {"run_id": "0123456789ab"}])
def test_cli_refuses_a_draft_whose_run_cannot_be_read_back_unless_allowed(tmp_path, edit):
    ctx, _pr, out, _ids = review_run(tmp_path)
    case = labelled(draft_case(ctx.shadow, out["run_id"]))
    draft = write(tmp_path / "draft.json", {**case, **edit})
    code, res = cli_finalize(ctx, tmp_path, draft)
    assert code != 0 and "--allow-license" in res["error"], res
    assert cli_finalize(ctx, tmp_path, draft, "--allow-license")[0] == 0


def test_a_finalized_case_does_not_carry_the_local_run_id(tmp_path):
    draft = draft_with(tmp_path, "mit")
    assert "run_id" in json.loads(draft.read_text())
    assert "run_id" not in json.loads(finalize_case(draft, tmp_path / "golden", allow_private=False).read_text())


def test_an_empty_entry_in_the_allow_list_does_not_allow_an_unknown_license(tmp_path):
    draft = draft_with(tmp_path, "")
    with pytest.raises(CodeCheckError, match="license"):
        finalize_case(draft, tmp_path / "golden", allow_private=False, allowed_licenses=("", "mit"))
