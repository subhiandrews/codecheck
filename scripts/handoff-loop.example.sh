#!/usr/bin/env bash
# Example loop for the handoff-round skill. Copy it, adapt it, run it yourself: nothing in this repository calls it.
#
# Each pass refreshes the installed plugin (so the next pass runs the code the last one merged), then runs one
# headless round. The round exits at once when no hand-off is pending.
#
# The plugin refresh commands and the allow-list below are a starting point and unverified on a real machine: check
# `claude plugin --help` and tighten the allow-list to what you are comfortable with. The skill itself refuses to
# run the queue or the merge guard from this checkout, so the installed plugin must be current.
set -u

REPO="${CODE_CHECK_REPO:-$HOME/opensource/codecheck}"   # a dedicated, clean clone of the code-check repository
SLEEP_SECONDS="${HANDOFF_LOOP_SLEEP:-300}"
ROUND_TIMEOUT="${HANDOFF_ROUND_TIMEOUT:-10800}"          # a round can include a 15+ minute live eval and CI

TOOLS=(
  Read Write Edit Grep Glob Skill Agent
  "Bash(git fetch:*)" "Bash(git switch:*)" "Bash(git pull --ff-only)" "Bash(git status:*)" "Bash(git add:*)"
  "Bash(git commit:*)" "Bash(git diff:*)" "Bash(git log:*)" "Bash(git push -u origin handoff-round/*)"
  "Bash(gh pr create:*)" "Bash(gh pr checks:*)" "Bash(gh pr view:*)" "Bash(gh pr merge:*)" "Bash(gh pr comment:*)"
  "Bash(code-check:*)" "Bash(command -v:*)"
  "Bash(.venv/bin/pytest:*)" "Bash(.venv/bin/ruff:*)" "Bash(.venv/bin/python -m codecheck:*)"
)

run_with_timeout() {
  if command -v timeout >/dev/null; then timeout "$ROUND_TIMEOUT" "$@"
  elif command -v gtimeout >/dev/null; then gtimeout "$ROUND_TIMEOUT" "$@"
  else "$@"; fi
}

while true; do
  claude plugin marketplace update code-check || echo "marketplace update failed; continuing" >&2
  claude plugin update code-check@code-check || echo "plugin update failed; continuing" >&2

  if cd "$REPO"; then
    # Stdin is closed: claude -p would otherwise wait for it. There is no turn limit; the timeout is the bound.
    run_with_timeout claude -p "Run the handoff-round skill." \
      --permission-mode dontAsk --permission-prompts none --output-format json \
      --allowedTools "${TOOLS[@]}" </dev/null || echo "round ended with status $?" >&2
  else
    echo "cannot enter $REPO" >&2
  fi

  sleep "$SLEEP_SECONDS"
done
