# Review Attribution and Label Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a one-line credit to the top-level review comment and a `code-check` label on reviewed PRs.

**Architecture:** `summary_body` gains an opt-in footer line; `Provider.add_label` (GitHub via `gh api`) is called best-effort from `commands/post.py` after a successful post. Two user-only config keys, `review.attribution` and `review.label`, control both.

**Tech Stack:** Python 3.14, pytest, ruff, `gh` CLI.

**Spec:** `docs/superpowers/specs/2026-11-13-review-attribution-design.md`

## Global Constraints

- Footer text, verbatim: `<sub>Reviewed with [code-check](https://github.com/subhiandrews/codecheck) · Apache-2.0</sub>`
- Footer goes in the summary body only, directly before the hidden `RUN_MARKER` line. `comment_body`, `render_local` and address-flow replies are unchanged.
- Config defaults: `review.attribution = true`, `review.label = "code-check"`. Empty `label` disables labeling; `attribution = false` removes the footer.
- Both keys are added to `REPO_FORBIDDEN` (user config and env only).
- Label names must match `[A-Za-z0-9._ :-]+`, max 50 characters, must not start with `-`; enforced in config loading and again in the provider (argv guard invariant in CLAUDE.md).
- Labeling failure never fails or changes the post; `post` output gains `labeled: bool`.
- `.venv/bin/ruff check .` and `.venv/bin/pytest -q` must pass. README and skills stay in sync with config keys and CLI output (`tests/test_readme.py`, `tests/test_plugin_static.py`).

## Review Focus

- Label already on the PR: the GitHub add-labels call is idempotent; the provider adds no pre-check (test: single `gh` call issued).
- Reviewer without triage rights: `add_label` raises `ProviderError`, review still reported `posted: true`, `labeled: false`.
- Hostile label value (`--foo`, newline, 51 chars) from env or user config: rejected at load, and by the provider if called directly.
- Re-review at a new head: footer present again, label call repeated harmlessly.
- Draft, stale head, own PR, local mode: no label call.

---

### Task 1: Footer in the summary body

**Files:**
- Modify: `src/codecheck/core/render.py` (`summary_body`)
- Test: `tests/test_render.py`

**Interfaces:**
- Produces: `summary_body(..., attribution: bool = False)`; constant `ATTRIBUTION` in `core/render.py` holding the verbatim footer text from Global Constraints.

- [ ] **Step 1: Write failing tests** in `tests/test_render.py`: `test_summary_footer_present_when_attribution_on` (body contains `ATTRIBUTION`, and `body.index(ATTRIBUTION) < body.index("<!-- code-check:run:")`); `test_summary_footer_absent_by_default`; `test_inline_comment_has_no_attribution` (`ATTRIBUTION not in comment_body(...)`, using an existing finding fixture from that file).
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_render.py -q`. Expected: FAIL (`ATTRIBUTION` import error).
- [ ] **Step 3: Implement** `ATTRIBUTION` and the keyword-only `attribution` parameter; when true, append `["", ATTRIBUTION]` just before the run-marker lines.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat: attribution footer in the review summary`.

### Task 2: Config keys

**Files:**
- Modify: `src/codecheck/config.py` (`ReviewConfig`, `REPO_FORBIDDEN`, validation where model names are validated)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `cfg.review.attribution: bool = True`, `cfg.review.label: str = "code-check"`; `LABEL_NAME = re.compile(r"[A-Za-z0-9._ :-]{1,50}")` in `config.py`, applied with `fullmatch` and a leading-`-` rejection, empty string allowed.

- [ ] **Step 1: Write failing tests** in `tests/test_config.py`: defaults (`attribution is True`, `label == "code-check"`); a repo `.code-check.toml` setting `[review] attribution = false` and another setting `label = "x"` each raise `ConfigError` naming the key (copy the pattern of the existing `review.auto_approve` parametrized case); user file `label = ""` loads; `CODE_CHECK_REVIEW__LABEL=--foo`, a label with a newline, and a 51-character label each raise `ConfigError`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_config.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the fields, the two `REPO_FORBIDDEN` entries, and label validation next to the existing model-name validation.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat: review.attribution and review.label config keys`.

### Task 3: Provider `add_label`

**Files:**
- Modify: `src/codecheck/providers/base.py` (Protocol), `src/codecheck/providers/github.py`, `tests/fakes.py`
- Test: `tests/providers/test_github_write.py`, `tests/providers/contract.py`

**Interfaces:**
- Produces: `Provider.add_label(self, ref: PRRef, label: str) -> None`. GitHub: `_label(label: str) -> str` validator (same rule as `LABEL_NAME`, raises `ProviderError`), then `self._gh("api", f"repos/{_repo(ref.repo)}/issues/{_num(ref.number)}/labels", "--method", "POST", "-f", f"labels[]={_label(label)}")`. `FakeProvider.labeled: list[tuple[PRRef, str]]` records calls; `FakeProvider.label_error: bool = False` makes it raise `ProviderError`.

- [ ] **Step 1: Write failing tests**: in `test_github_write.py`, `test_add_label_issues_one_gh_call` (assert argv exactly as above, following `test_reply_resolve_request_review_commands`) and `test_add_label_rejects_option_like_and_overlong_names` (`--foo`, `a\nb`, 51 chars raise `ProviderError`, and no command ran). In `contract.py`, call `provider.add_label(ref, "code-check")` next to the `request_review` call.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/providers -q`. Expected: FAIL.
- [ ] **Step 3: Implement** Protocol method, GitHub method and validator, fake.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat: Provider.add_label`.

### Task 4: Wire into `post`

**Files:**
- Modify: `src/codecheck/commands/post.py`
- Test: `tests/test_cli_review.py`

**Interfaces:**
- Consumes: `summary_body(..., attribution=)` (Task 1), `cfg.review.attribution` / `cfg.review.label` (Task 2), `provider.add_label` and `FakeProvider.labeled` / `label_error` (Task 3).
- Produces: `post` JSON gains `"labeled": bool` on the posted path.

- [ ] **Step 1: Write failing tests** in `tests/test_cli_review.py`, using `pr_setup` and the flow of `test_pr_flow_posts_one_inline_review_and_marks_head`: `test_posted_review_has_footer_and_label` (`ATTRIBUTION in provider.posted[0]["body"]`, every inline comment body lacks it, `provider.labeled == [(pr.ref, "code-check")]`, `out["labeled"] is True`); `test_attribution_and_label_can_be_disabled` (`ReviewConfig(attribution=False, label="")` gives no footer, no label call, `labeled` false); `test_label_failure_does_not_fail_post` (`provider.label_error = True` gives `posted` true, `labeled` false); `test_no_label_for_draft_or_stale` (extend/reuse the draft and stale setups, assert `provider.labeled == []`).
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_cli_review.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** in `run`: pass `attribution=cfg.review.attribution` to `summary_body`; after `post_review` succeeds, if `cfg.review.label`, call `ctx.provider.add_label(ref, cfg.review.label)` inside `try/except ProviderError` setting `labeled`. Place it before the shadow-recording block. Early returns (draft, stale, own PR) are untouched.
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: all PASS.
- [ ] **Step 5: Commit** `feat: label reviewed PRs and credit code-check in the summary`.

### Task 5: Docs and final checks

**Files:**
- Modify: `README.md` (config block under `[review]`; forbidden-keys sentence; a short "Attribution and label" note), `CLAUDE.md` (add the two keys to the user-only list in Invariants), `skills/review-pr/SKILL.md` step 8 (mention `labeled` only if the skill echoes post fields; otherwise leave unchanged).
- Test: `tests/test_readme.py`, `tests/test_plugin_static.py`

- [ ] **Step 1: Update README**: `attribution = true  # footer line in the review summary` and `label = "code-check"  # label added to reviewed PRs; "" disables` under `[review]`; add `review.attribution` and `review.label` to the forbidden-keys sentence (line ~99).
- [ ] **Step 2: Run** `.venv/bin/ruff check . && .venv/bin/pytest -q`. Expected: no ruff findings, all tests PASS (readme and static-plugin tests included).
- [ ] **Step 3: Commit** `docs: document attribution footer and label`.
