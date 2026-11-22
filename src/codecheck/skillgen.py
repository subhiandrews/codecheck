"""Render Codex skills from the Claude skill sources."""

import re
from pathlib import Path

from codecheck.errors import CodeCheckError

INSTALL_BIN = "@CODE_CHECK_BIN@"

PANEL = (
    "3. Run `code-check panel run --run <run_id>` and wait for its JSON result. "
    "If `failed_members` includes `aggregator` or the command errors, report the failure and go straight to cleanup. "
    "Otherwise continue and report any failed reviewers."
)
QUEUE = "## Without an argument\n\nAsk for a PR reference and stop until one is provided.\n\n"
QUEUE_RULE = (r"^## Without an argument \(repository mode\)\n\n[^\n]+\n\n", QUEUE)
PUSH_WAIT = "Allow up to 10 minutes for the push because it runs the configured tests first. Wait for the command to finish."
REVIEW_RULES = (
    (r"^3\. Dispatch the panel in parallel:.*$", PANEL),
    (r"^4\. Run `code-check ingest .*\n^5\. Dispatch one Agent call .*\n", ""),
    (r"^([6-9])\. ", lambda match: f"{int(match[1]) - 2}. "),
    (r" If any subagent's final .* relaying those reasons\.", ""),
    (r"\. Note `run_id`, `run_dir`, (?:`panel`, `chunks`|`panel\.members`, `panel\.languages` and `chunks`)\.",
     ". Note `run_id`."),
)
SUBSTITUTIONS = {
    "review-local": (
        *REVIEW_RULES,
        (r" Safe under /loop because it costs nothing while the work is unchanged\.", ""),
        (r" Under `/loop` it spends no tokens while your work is unchanged\.", ""),
    ),
    "review-pr": (
        *REVIEW_RULES,
        (r" With no argument it reviews the open PRs .*? Use for", " Use for"),
        (r', or under /loop to keep reviewing teammates\' PRs\.', "."),
        (r"\(step 8\)", "(step 6)"),
        (r" A keyword without a PR means repository mode, where it has no effect because configuration controls it\.", ""),
        (r", and `?\(ignored guidance: <reasons>\)`? when any subagent.*\.", "."),
        (r"Ask no questions, except the replies-only question in step 1, and only when run interactively\.",
         "Ask for a missing PR reference or ask the replies-only question in step 1 when run interactively."),
        QUEUE_RULE,
    ),
    "address-comments": (
        (r"Run it with the Bash tool's maximum timeout .*? wait for it instead\.", PUSH_WAIT),
    ),
    "babysit-pr": (
        (r" With no argument it works your open PRs .*? Use under /loop to keep your PRs moving\.", ""),
        (r"`code-check:address-comments`", "`$address-comments`"),
        (r"with the Bash tool's maximum timeout of 600000 ms \(tests gate the push; handle a slow test run and a refused push as in address-comments\)",
         "and allow up to 10 minutes for the configured tests to finish, as in address-comments"),
        QUEUE_RULE,
    ),
}
CLAUDE_ONLY = re.compile(
    r"CLAUDE_PLUGIN_ROOT|subagent_type|\bsubagents?\b|code-check:|AskUserQuestion|Bash tool|run_in_background|/loop"
)


def render_codex_skills(source: Path) -> dict[str, str]:
    rendered = {}
    for name, rules in SUBSTITUTIONS.items():
        relative = f"{name}/SKILL.md"
        text = (source / relative).read_text(encoding="utf-8")
        text = text.replace("${CLAUDE_PLUGIN_ROOT}/bin/code-check", INSTALL_BIN)
        for pattern, replacement in rules:
            text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
        if CLAUDE_ONLY.search(text) or INSTALL_BIN not in text:
            raise CodeCheckError(f"untranslated runtime instruction in {relative}")
        rendered[relative] = text
    return rendered
