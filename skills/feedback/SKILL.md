---
name: feedback
description: Report a problem with code-check, or ask for a feature, as a GitHub issue in the code-check repository, only after you have read the exact text and said yes. Use when code-check did something wrong or is missing something, or when another code-check skill hands over a pending report. Asks at most once a day on its own and never when run unattended.
argument-hint: "[what went wrong or what you want]"
---

# feedback

Files a GitHub issue about code-check on the user's behalf. Nothing is posted until the user has seen the whole text and chosen to post it.

`code-check` below means `"${CLAUDE_PLUGIN_ROOT}/bin/code-check"`. Every subcommand prints one JSON document. Drafts are plain files under the state directory; the CLI scrubs secrets and caps the size of whatever it files, edited or not.

Stop at once, saying nothing, if `CODE_CHECK_UNATTENDED` or `CODE_CHECK_HEADLESS` is set or nobody is there to answer: this skill never runs unattended.

## With an argument (the user asked to report something)

1. Run `code-check feedback signal --kind user-reported --source feedback --note -` and pass the user's words, exactly as they wrote them, on stdin with a quoted heredoc (`<<'NOTE'`), never inline in the command: the shell would run backticks and `$(...)` in them. If `recorded` is false, say why (`disabled` means feedback is turned off in the configuration) and stop. Note `fingerprint`.
2. Run `code-check feedback pending --interactive --user-initiated --only <fingerprint>` and show `drafts[0].title` and `drafts[0].body` in full, then say the file is at `drafts[0].path`.
3. Ask with AskUserQuestion: **Post as shown**, **Edit first**, **Cancel**.
   - Edit first: ask what to change, rewrite the file at `path` with the Write tool (first line `# <title>`, keep the `<!-- feedback-fingerprint: ... -->` line last), show it again in full and ask again.
   - Post as shown: run `code-check feedback answer --id <fingerprint> --choice post --user-initiated`.
   - Cancel: run `code-check feedback answer --id <fingerprint> --choice decline --user-initiated`.
4. Report `url`. If `outcome` is `created` it is the new issue; `duplicate` means an open issue already covers it and a short "seen again" note was added; `fallback` means nothing was filed (GitHub was not reachable through `gh`), so give the user the `url` to open and the draft's `path`.

## Without an argument (another skill handed over)

1. Run `code-check feedback pending --interactive`. If `ask` is false, say nothing and stop: it already asked today, the user snoozed it or said never, or nothing is waiting.
2. Show the first entry of `drafts` (`title` and `body`) in full and say how many others are waiting. Do not summarise or reword it.
3. Ask with AskUserQuestion: **Post as shown**, **Edit first**, **Not now**, **Snooze for a week**, **Never ask me again**.
   - Post as shown, then Edit first as above: `code-check feedback answer --id <fingerprint> --choice post`.
   - Not now: `--choice decline`. Snooze for a week: `--choice snooze`. Never ask me again: `--choice never`.
4. Report the result in one line, then stop. Ask about only one draft per day.

## Recording a problem you noticed

Another skill may send you here after something looked like a defect in code-check (a command failed unexpectedly, an agent went off track or looped, a finding was disputed). Record it first with `code-check feedback signal --kind agent-off-track --judged --source <skill> --id step=<label>`, using one of: `attempt-exhausted`, `rerun-refused`, `findings-missing`, `push-refused`, `loop-failed`, `thread-resolved-unfixed`, `finding-disputed`, `agent-off-track`. Ids are short labels, never free text, names or code. Then go on as in "Without an argument".

## Rules

- Never post without an explicit **Post as shown** (after any edit). Never paste code, secrets or other people's comments into a draft; keep what the CLI wrote and what the user adds.
- `code-check feedback answer` must stay out of any allowlist: the permission prompt is part of the consent.
- "Never ask me again" only stops the automatic questions; `/code-check:feedback <text>` always works.
- Do not retry a `fallback`. Never ask more than the questions above.
