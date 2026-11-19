"""Synthetic cases keep the lesson of a private or non-permissive PR and drop its code.

Brief: what is general about a labelled case (language, category, severity, expected or not), never what identifies it.
Check: a hand-written snippet shares nothing distinctive with the source and matches the brief.
"""

import json

import pytest
from clihelp import call, make_ctx

from codecheck.errors import CodeCheckError
from codecheck.shadow.synthesize import build_brief, check_synthetic, leak_terms

SOURCE = {
    "version": 1, "case_id": "acme-payments-412-1a2b3c4d", "repo": "acme/payments", "pr": 412, "head_sha": "1a2b3c4d" * 5,
    "repo_private": True, "repo_license": None, "label_status": "confirmed",
    "diff": (
        "diff --git a/internal/billing/ledger_reconciler.go b/internal/billing/ledger_reconciler.go\n"
        "+func reconcile_ledger_entries(AcmeInvoiceRouter *Router) error {\n"
        "+    entry := ledgerIndex[key]\n"
        "+    if len(parts) > 0 && parts[0] == \"\" {\n"
        "+    return postTo(\"ledger.acme-internal.example.com\", entry.Amount, strictRoundingMode(entry))\n"
    ),
    "reviewer_outputs": {"skeptic": [{"message": "entry may be nil when the key is absent; see `settlementWindow` and synchronization"}]},
    "labels": [
        {"id": "aaaaaaaaaaaa", "file": "internal/billing/ledger_reconciler.go", "line": 2, "severity": "warning",
         "category": "bug", "label": "expected", "message": "the vaultCheckpoint lookup can fail"},
        {"id": "bbbbbbbbbbbb", "file": "internal/billing/ledger_reconciler.go", "line": 3, "severity": "suggestion",
         "category": "style", "label": "not_expected"},
        {"id": "cccccccccccc", "file": "internal/billing/ledger_reconciler.go", "line": 9, "severity": "suggestion",
         "category": "docs", "label": "unlabelled"},
    ],
    "misses": [{"path": "x.go", "line": 1, "author": "bob", "body": "rename it to quarterlyRollup"}],
}

CLEAN = {
    "name": "missing-key-lookup",
    "base": {"inventory.go": "package inventory\n\ntype Row struct{ Price int }\n\nvar stock = map[string]*Row{}\n"},
    "change": {"inventory.go": ("package inventory\n\ntype Row struct{ Price int }\n\nvar stock = map[string]*Row{}\n\n"
                                "func PriceOf(item string) int {\n\trow := stock[item]\n\treturn row.Price\n}\n")},
    "expected": [{"file": "inventory.go", "line": 9, "category": ["bug", "null"], "severity": "warning"}],
    "must_not_report": [{"file": "inventory.go", "category": "style"}],
}
BRIEF = build_brief(SOURCE)


def clone(snippet=CLEAN):
    return json.loads(json.dumps(snippet))


# -- the brief ----------------------------------------------------------------------------------------------------


def test_the_brief_is_general_and_holds_nothing_that_identifies_the_source():
    brief = build_brief(SOURCE)
    text = json.dumps(brief).lower()
    for private in ("acme", "payments", "412", "1a2b3c4d", "ledger", "reconcil", "internal", "billing", "aaaaaaaaaaaa", "entry",
                    ".go", "label_status", "diff", "vault", "quarterly"):
        assert private not in text, private
    assert brief["language"] == "go"
    assert [(f["label"], f["category"], f["severity"]) for f in brief["findings"]] == [
        ("expected", "bug", "warning"), ("not_expected", "style", "suggestion")]  # the unlabelled one is left out
    assert all(f["pattern"] == "" for f in brief["findings"])  # a person fills this in
    assert brief["allow"] == []  # and lists the generic API names the defect needs


def test_the_language_is_the_panels_detection_for_the_most_common_labelled_file():
    labels = [{**lab, "file": f} for lab, f in zip(SOURCE["labels"], ["a.py", "b.py", "c.rs"], strict=True)]
    assert build_brief({**SOURCE, "labels": labels})["language"] == "python"
    assert build_brief({**SOURCE, "labels": [{**SOURCE["labels"][0], "file": "Makefile"}]})["language"] == "unknown"
    assert build_brief({**SOURCE, "labels": [{**SOURCE["labels"][0], "file": "build.gradle.kts"}]})["language"] == "kotlin"


def test_the_instructions_say_where_to_anchor_and_that_severity_is_advisory():
    text = build_brief(SOURCE)["instructions"].lower()
    assert "anchor" in text and "within 2 lines" in text and "stands alone" in text


def test_a_case_with_no_labelled_findings_has_no_brief():
    with pytest.raises(CodeCheckError, match="no labelled"):
        build_brief({**SOURCE, "labels": [SOURCE["labels"][2]]})


# -- the leak check -----------------------------------------------------------------------------------------------


def problems(snippet, **kw):
    return check_synthetic(snippet, SOURCE, brief=BRIEF, **kw)


def test_a_clean_snippet_passes():
    assert problems(CLEAN) == []


def test_distinctive_terms_are_collected_but_ordinary_words_are_not():
    terms = leak_terms(SOURCE)
    assert {"reconcile_ledger_entries", "acmeinvoicerouter", "ledger.acme-internal.example.com",
            "internal/billing/ledger_reconciler.go", "strictroundingmode", "acme", "payments", "acme/payments"} <= terms
    assert not {"entry", "error", "return", "func", "amount"} & terms


def test_prose_contributes_compound_names_and_quoted_code_but_not_plain_long_words():
    terms = leak_terms(SOURCE)
    assert "settlementwindow" in terms  # quoted in a reviewer message
    assert "vaultcheckpoint" in terms  # a label's message
    assert "quarterlyrollup" in terms  # a missed human comment's body
    assert "synchronization" not in terms  # a long English word in prose says nothing about the project


def test_attribute_chains_and_prose_slashes_are_not_hosts_or_paths():
    case = {"diff": "+x = os.path.join(a, b)\n+cfg.dev = 1\n+np.io.read()\n", "reviewer_outputs": {"r": ["read/write and try/except"]},
            "labels": [], "repo": "o/r"}
    assert leak_terms(case) == set()
    assert {"build.example.com"} <= leak_terms({**case, "diff": "+url = 'https://build.example.com/x'\n"})
    assert {"example.org"} <= leak_terms({**case, "diff": "+u = 'example.org'\n"})


@pytest.mark.parametrize("where,mutate", [
    ("an identifier", lambda s: s["change"].update({"inventory.go": "func reconcile_ledger_entries() {}\n"})),
    ("a camelCase identifier", lambda s: s["change"].update({"inventory.go": "type AcmeInvoiceRouter struct{}\n"})),
    ("a host name", lambda s: s["change"].update({"inventory.go": "const URL = \"ledger.acme-internal.example.com\"\n"})),
    ("a path", lambda s: s["change"].update({"internal/billing/ledger_reconciler.go": "package main\n"})),
    ("the name", lambda s: s.update(name="strictRoundingMode-case")),
    ("the project name", lambda s: s["change"].update({"inventory.go": "// the payments service\n"})),
    ("a name only a label message used", lambda s: s["change"].update({"inventory.go": "var vaultCheckpoint int\n"})),
    ("a case-style rename", lambda s: s["change"].update({"inventory.go": "func reconcileLedgerEntries() {}\n"})),
    ("a snake_case rename", lambda s: s["change"].update({"inventory.go": "func acme_invoice_router() {}\n"})),
    ("the project name inside an identifier", lambda s: s["change"].update({"inventory.go": "type PaymentsClient struct{}\n"})),
    ("a name only in the expectations", lambda s: s["expected"][0].update({"note": "see vaultCheckpoint"})),
    ("a name only in must_not_report", lambda s: s["must_not_report"][0].update({"why": "strictRoundingMode"})),
    ("a name in an extra key", lambda s: s.update(description="reconciles ledger.acme-internal.example.com")),
])
def test_the_snippet_may_not_share_what_is_distinctive_with_the_source(where, mutate):
    snippet = clone()
    mutate(snippet)
    found = problems(snippet)
    assert any("shares" in p for p in found), (where, found)


def test_a_line_copied_from_the_source_is_found_without_any_distinctive_term_in_it():
    snippet = clone()
    snippet["base"]["x.go"] = "func f() {\n        if len(parts)   > 0 && parts[0] == \"\" {\n}\n"
    found = problems(snippet)
    assert any("copies a line" in p for p in found) and not any("shares" in p for p in found), found


def test_a_term_matches_on_identifier_boundaries_not_inside_another_word():
    snippet = clone()
    snippet["change"]["inventory.go"] += "// the paymentsish score and a decode of the payments_per_page\n"
    assert [p for p in problems(snippet) if "shares" in p] == ["shares 'payments' with the source (use another name, or allow it if it is a generic API name)"]
    source = {**SOURCE, "repo": "acme/core"}  # `core` is a repository name, and `score` is an ordinary word
    ordinary = clone()
    ordinary["change"]["inventory.go"] += "var score = decode(barcode)\n"
    assert check_synthetic(ordinary, source, brief=BRIEF) == []
    ordinary["change"]["inventory.go"] += "type CoreClient struct{}\n"
    assert any("'core'" in p for p in check_synthetic(ordinary, source, brief=BRIEF))


def test_a_change_that_rewrites_a_base_file_cannot_hide_what_the_base_held():
    snippet = clone()
    snippet["base"]["inventory.go"] += "// see reconcile_ledger_entries for the original\n"
    assert any("reconcile_ledger_entries" in p for p in problems(snippet))  # the change file no longer has it


def test_a_copied_line_in_a_base_file_is_found_even_when_the_change_drops_it():
    snippet = clone()
    snippet["base"]["inventory.go"] += "          return    postTo(\"ledger.acme-internal.example.com\", entry.Amount, strictRoundingMode(entry))\n"
    assert any("copies" in p for p in problems(snippet))


def test_the_brief_can_vouch_for_the_generic_api_names_the_defect_needs():
    snippet = clone()
    snippet["change"]["inventory.go"] += "var _ = strictRoundingMode\nvar _ = postTo\n"
    assert {"strictroundingmode", "postto"} <= {p.split("'")[1] for p in problems(snippet) if p.startswith("shares")}
    vouched = {**BRIEF, "allow": ["strictRoundingMode", "postTo"]}
    assert check_synthetic(snippet, SOURCE, brief=vouched) == []
    assert check_synthetic(snippet, SOURCE, brief=vouched, allow=["other"]) == []  # the two lists add up


def test_a_vouched_name_does_not_excuse_a_project_name():
    snippet = clone()
    snippet["change"]["inventory.go"] += "var _ = reconcile_ledger_entries\n"
    vouched = {**BRIEF, "allow": ["strictRoundingMode"]}
    assert any("reconcile_ledger_entries" in p for p in check_synthetic(snippet, SOURCE, brief=vouched))


def test_allow_clears_a_term_a_person_decided_is_generic():
    snippet = clone()
    snippet["change"]["inventory.go"] += "var x = strictRoundingMode\n"
    assert any("strictroundingmode" in p.lower() for p in problems(snippet))
    assert problems(snippet, allow=["strictRoundingMode"]) == []


# -- fidelity to the brief and the schema ---------------------------------------------------------------------------


def test_the_snippet_must_expect_what_the_brief_expects():
    assert "the brief expects 1 expected finding(s); the snippet has 0" in problems({**CLEAN, "expected": []})
    # severity is advisory: a standalone snippet can deserve a different one than the original's context gave
    assert problems({**CLEAN, "expected": [{**CLEAN["expected"][0], "severity": "critical"}]}) == []
    assert any("must_not_report" in p for p in problems({**CLEAN, "must_not_report": []}))


def test_the_snippet_must_have_the_defect_kind_and_language_the_brief_names():
    other_kind = {**CLEAN, "expected": [{**CLEAN["expected"][0], "category": ["style"]}]}
    assert any("category 'bug'" in p for p in problems(other_kind))
    in_python = clone()
    in_python["base"] = {"inventory.py": "STOCK = {}\n"}
    in_python["change"] = {"inventory.py": "STOCK = {}\n\n\ndef price_of(item):\n    row = STOCK.get(item)\n    return row['price']\n"}
    in_python["expected"] = [{"file": "inventory.py", "line": 6, "category": ["bug"], "severity": "warning"}]
    in_python["must_not_report"] = [{"file": "inventory.py", "category": "style"}]
    assert any("brief is for go" in p for p in problems(in_python))


def test_a_new_snippet_is_uncalibrated_until_a_person_has_run_it_live():
    assert any("calibrated" in p for p in problems({**CLEAN, "calibrated": True}))
    assert problems({**CLEAN, "calibrated": False}) == []


def test_an_invalid_snippet_is_refused_with_the_live_eval_schema_message():
    assert any("change must not be empty" in p for p in problems({**CLEAN, "change": {}}))


def test_the_module_can_be_imported_on_its_own():
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-c", "import codecheck.shadow.synthesize"], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr


# -- the commands -----------------------------------------------------------------------------------------------------


def run_cli(tmp_path, *argv):
    ctx = make_ctx(tmp_path, tmp_path)
    return call(ctx, "shadow", "promote", *argv)


def test_the_cli_writes_a_brief_and_checks_a_snippet(tmp_path):
    src = tmp_path / "source.json"
    src.write_text(json.dumps(SOURCE))
    brief = tmp_path / "brief.json"
    code, out = run_cli(tmp_path, "--brief", str(src), "--out", str(brief))
    assert code == 0 and json.loads(brief.read_text())["language"] == "go" and out["brief"] == str(brief)
    assert (brief.stat().st_mode & 0o077) == 0  # the brief is private until a person has read it
    snip = tmp_path / "snip" / "missing-key-lookup"
    snip.mkdir(parents=True)
    (snip / "case.json").write_text(json.dumps(CLEAN))
    code, out = run_cli(tmp_path, "--check-synthetic", str(snip), "--source", str(src), "--against", str(brief))
    assert code == 0 and out["ok"] is True and out["problems"] == []
    (snip / "case.json").write_text(json.dumps({**CLEAN, "name": "reconcile_ledger_entries"}))
    code, out = run_cli(tmp_path, "--check-synthetic", str(snip), "--source", str(src), "--against", str(brief))
    assert code == 0 and out["ok"] is False and out["problems"]
    assert "allow_hint" not in out  # a ready-to-paste --allow would make the easy step the one that defeats the check
    assert "do not pass this output to the model" in out["next"]
    code, out = run_cli(tmp_path, "--check-synthetic", str(snip), "--source", str(src), "--against", str(brief), "--allow", "reconcile_ledger_entries")
    assert out["ok"] is True and out["next"].startswith("read it")


def test_the_cli_needs_its_companion_arguments(tmp_path):
    code, out = run_cli(tmp_path, "--brief", str(tmp_path / "nope.json"))
    assert code == 1 and "--out" in out["error"]
    code, out = run_cli(tmp_path, "--check-synthetic", str(tmp_path))
    assert code == 1 and "--source" in out["error"]


def test_a_flag_for_another_mode_is_an_error_not_silently_ignored(tmp_path):
    src = tmp_path / "source.json"
    src.write_text(json.dumps(SOURCE))
    code, out = run_cli(tmp_path, "--brief", str(src), "--out", str(tmp_path / "b.json"), "--source", str(src))
    assert code == 1 and "--source" in out["error"] and "does not apply" in out["error"]
    code, out = run_cli(tmp_path, "--check-synthetic", str(tmp_path), "--source", str(src), "--out", str(tmp_path / "b.json"))
    assert code == 1 and "--out" in out["error"]


def test_a_malformed_snippet_gets_its_schema_problems_not_a_crash(tmp_path):
    src = tmp_path / "source.json"
    src.write_text(json.dumps(SOURCE))
    snip = tmp_path / "snip"
    snip.mkdir()
    for bad in ({**CLEAN, "base": [1]}, {**CLEAN, "change": None}, {**CLEAN, "base": {"inventory.go": 7}}):
        (snip / "case.json").write_text(json.dumps(bad))
        code, out = run_cli(tmp_path, "--check-synthetic", str(snip), "--source", str(src))
        assert code == 0 and out["ok"] is False and out["problems"], bad
        assert not any(p.startswith("shares") for p in out["problems"])


def test_a_brief_will_not_overwrite_the_case_it_is_built_from(tmp_path):
    src = tmp_path / "source.json"
    src.write_text(json.dumps(SOURCE))
    (tmp_path / "sub").mkdir()
    for out_path in (str(src), str(tmp_path / "sub" / ".." / "source.json")):  # the second spelling needs resolve()
        code, out = run_cli(tmp_path, "--brief", str(src), "--out", out_path)
        assert code == 1 and "overwrite" in out["error"], out
    assert json.loads(src.read_text()) == SOURCE


def test_a_category_that_names_the_source_is_refused_in_the_brief():
    labels = [{**SOURCE["labels"][0], "category": "ledger_reconciler-bug"}, *SOURCE["labels"][1:]]
    with pytest.raises(CodeCheckError, match="category"):
        build_brief({**SOURCE, "labels": labels})


def test_a_source_with_no_diff_is_refused_not_passed(tmp_path):
    for wrong in ({}, BRIEF, CLEAN, {**SOURCE, "diff": "  \n"}):
        assert any("no diff" in p for p in check_synthetic(CLEAN, wrong, brief=BRIEF)), wrong
    src = tmp_path / "brief.json"
    src.write_text(json.dumps(BRIEF))  # the usual slip: the brief given as the source
    snip = tmp_path / "snip"
    snip.mkdir()
    (snip / "case.json").write_text(json.dumps(CLEAN))
    code, out = run_cli(tmp_path, "--check-synthetic", str(snip), "--source", str(src))
    assert code == 0 and out["ok"] is False and "no diff" in out["problems"][0]


def test_a_long_plain_word_is_found_inside_a_longer_lowercase_token():
    snippet = clone()
    snippet["change"]["inventory.go"] += "var mockstrictroundingmodex int\n"
    assert any("'strictroundingmode'" in p for p in problems(snippet))
    short = clone()
    short["change"]["inventory.go"] += "var scoreboard, decoded int\n"  # a short name does not match inside other words
    assert check_synthetic(short, {**SOURCE, "repo": "acme/core"}, brief=BRIEF) == []
