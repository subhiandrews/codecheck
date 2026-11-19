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
        "+    return postTo(\"ledger.acme-internal.example.com\", entry.Amount, strictRoundingMode(entry))\n"
    ),
    "reviewer_outputs": {"skeptic": [{"message": "entry may be nil when the key is absent in ledgerIndex"}]},
    "labels": [
        {"id": "aaaaaaaaaaaa", "file": "internal/billing/ledger_reconciler.go", "line": 2, "severity": "warning",
         "category": "bug", "label": "expected"},
        {"id": "bbbbbbbbbbbb", "file": "internal/billing/ledger_reconciler.go", "line": 3, "severity": "suggestion",
         "category": "style", "label": "not_expected"},
        {"id": "cccccccccccc", "file": "internal/billing/ledger_reconciler.go", "line": 9, "severity": "suggestion",
         "category": "docs", "label": "unlabelled"},
    ],
}

CLEAN = {
    "name": "missing-key-lookup",
    "base": {"inventory.py": "STOCK = {}\n"},
    "change": {"inventory.py": "STOCK = {}\n\n\ndef price_of(item):\n    row = STOCK.get(item)\n    return row['price']\n"},
    "expected": [{"file": "inventory.py", "line": 6, "category": ["bug", "null"], "severity": "warning"}],
    "must_not_report": [{"file": "inventory.py", "category": "style"}],
}
BRIEF = build_brief(SOURCE)


# -- the brief ----------------------------------------------------------------------------------------------------


def test_the_brief_is_general_and_holds_nothing_that_identifies_the_source():
    brief = build_brief(SOURCE)
    text = json.dumps(brief).lower()
    for private in ("acme", "payments", "412", "1a2b3c4d", "ledger", "reconcil", "internal", "billing", "aaaaaaaaaaaa", "entry",
                    ".go", "label_status", "diff"):
        assert private not in text, private
    assert brief["language"] == "go"
    assert [(f["label"], f["category"], f["severity"]) for f in brief["findings"]] == [
        ("expected", "bug", "warning"), ("not_expected", "style", "suggestion")]  # the unlabelled one is left out
    assert all(f["pattern"] == "" for f in brief["findings"])  # a person fills this in


def test_a_brief_of_mixed_languages_names_the_most_common_one_and_unknown_when_unsure():
    labels = [{**lab, "file": f} for lab, f in zip(SOURCE["labels"], ["a.py", "b.py", "c.rs"], strict=True)]
    assert build_brief({**SOURCE, "labels": labels})["language"] == "python"
    assert build_brief({**SOURCE, "labels": [{**SOURCE["labels"][0], "file": "Makefile"}]})["language"] == "unknown"


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
            "internal/billing/ledger_reconciler.go", "strictroundingmode"} <= terms
    assert not {"entry", "error", "return", "func", "amount"} & terms


@pytest.mark.parametrize("where,mutate", [
    ("an identifier", lambda s: s["change"].update({"inventory.py": "def reconcile_ledger_entries():\n    return 1\n"})),
    ("a camelCase identifier", lambda s: s["change"].update({"inventory.py": "class AcmeInvoiceRouter:\n    pass\n"})),
    ("a host name", lambda s: s["change"].update({"inventory.py": "URL = 'ledger.acme-internal.example.com'\n"})),
    ("a path", lambda s: s["change"].update({"internal/billing/ledger_reconciler.go": "package main\n"})),
    ("the name", lambda s: s.update(name="strictRoundingMode-case")),
    ("a copied line", lambda s: s["base"].update({"x.py": "    return postTo(\"ledger.acme-internal.example.com\", entry.Amount)\n"})),
])
def test_the_snippet_may_not_share_what_is_distinctive_with_the_source(where, mutate):
    snippet = json.loads(json.dumps(CLEAN))
    mutate(snippet)
    found = problems(snippet)
    assert any("shares" in p or "copies" in p for p in found), (where, found)


def test_a_copied_long_line_is_found_even_when_indentation_differs():
    snippet = json.loads(json.dumps(CLEAN))
    snippet["change"]["inventory.py"] += "          return    postTo(\"ledger.acme-internal.example.com\", entry.Amount, strictRoundingMode(entry))\n"
    assert any("copies" in p for p in problems(snippet))


def test_allow_clears_a_term_a_person_decided_is_generic():
    snippet = json.loads(json.dumps(CLEAN))
    snippet["change"]["inventory.py"] += "x = strictRoundingMode\n"
    assert any("strictroundingmode" in p.lower() for p in problems(snippet))
    assert problems(snippet, allow=["strictRoundingMode"]) == []


# -- fidelity to the brief and the schema ---------------------------------------------------------------------------


def test_the_snippet_must_expect_what_the_brief_expects():
    assert any("expected" in p for p in problems({**CLEAN, "expected": []}))
    wrong = {**CLEAN, "expected": [{**CLEAN["expected"][0], "severity": "critical"}]}
    assert any("severit" in p for p in problems(wrong))
    assert any("must_not_report" in p for p in problems({**CLEAN, "must_not_report": []}))


def test_a_new_snippet_is_uncalibrated_until_a_person_has_run_it_live():
    assert any("calibrated" in p for p in problems({**CLEAN, "calibrated": True}))
    assert problems({**CLEAN, "calibrated": False}) == []


def test_an_invalid_snippet_is_refused_with_the_live_eval_schema_message():
    assert any("change must not be empty" in p for p in problems({**CLEAN, "change": {}}))


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


def test_the_cli_needs_its_companion_arguments(tmp_path):
    code, out = run_cli(tmp_path, "--brief", str(tmp_path / "nope.json"))
    assert code == 1 and "--out" in out["error"]
    code, out = run_cli(tmp_path, "--check-synthetic", str(tmp_path))
    assert code == 1 and "--source" in out["error"]
