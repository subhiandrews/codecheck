#!/usr/bin/env bash
# Run the handoff-round skill in a loop, one hand-off per round.
#
# Each round: `claude -p` runs the skill, which analyses one hand-off, opens a PR, waits for CI, runs the merge guard and
# merges with `gh pr merge --admin` (the guard stays inside the skill, so this script never merges anything itself).
# Then this script puts the checkout back on a fresh main, deletes the merged handoff-round branches, refreshes the
# code-check marketplace and plugin so the next round runs the code the last one merged, and starts again.
#
#   scripts/handoff-loop.sh            # run until stopped
#   touch ~/.local/state/handoff-loop/STOP   # stop after the current round
#
# Settings (environment): CODE_CHECK_REPO (a dedicated, clean clone), HANDOFF_LOOP_SLEEP (seconds between rounds),
# HANDOFF_LOOP_IDLE_SLEEP (seconds when nothing was pending), HANDOFF_LOOP_NOACTION_SLEEP (seconds after a round
# that analysed a hand-off and found nothing to act on), HANDOFF_ROUND_TIMEOUT (seconds per round),
# HANDOFF_LOOP_MAX_FAILURES (consecutive failed rounds before the loop stops).
#
# The allow-list below is a starting point; absolute-path `Bash(...)` rules are unverified (see CLAUDE.md), so if the
# review-local step is denied, tighten or widen the `code-check` entries to match your install.
set -u

REPO="${CODE_CHECK_REPO:-$HOME/opensource/codecheck}"
SLEEP_SECONDS="${HANDOFF_LOOP_SLEEP:-60}"
IDLE_SLEEP_SECONDS="${HANDOFF_LOOP_IDLE_SLEEP:-1800}"
NOACTION_SLEEP_SECONDS="${HANDOFF_LOOP_NOACTION_SLEEP:-600}"
ROUND_TIMEOUT="${HANDOFF_ROUND_TIMEOUT:-10800}"
MAX_FAILURES="${HANDOFF_LOOP_MAX_FAILURES:-3}"
STATE="${HANDOFF_LOOP_STATE:-$HOME/.local/state/handoff-loop}"
PLUGIN="code-check@code-check"
MARKETPLACE="code-check"

TOOLS=(
  Read Write Edit Grep Glob Skill Agent
  "Bash(git fetch:*)" "Bash(git switch:*)" "Bash(git branch -d handoff-round/*)" "Bash(git pull --ff-only)" "Bash(git status:*)" "Bash(git add:*)"
  "Bash(git commit:*)" "Bash(git diff:*)" "Bash(git log:*)" "Bash(git push -u origin handoff-round/*)"
  "Bash(gh pr create:*)" "Bash(gh pr checks:*)" "Bash(gh pr view:*)" "Bash(gh pr merge:*)" "Bash(gh pr comment:*)"
  "Bash(code-check:*)" "Bash(*/bin/code-check:*)" "Bash(command -v:*)"
  "Bash(.venv/bin/pytest:*)" "Bash(.venv/bin/ruff:*)" "Bash(.venv/bin/python -m codecheck:*)"
)

log() { printf '%s handoff-loop: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }

# The ECC GateGuard hook asks for facts before the first Bash or Write call, which no unattended round can answer, so it
# denies tool calls (including the scratch analysis Write). Rounds run with it off.
export ECC_GATEGUARD=off

mkdir -p "$STATE" || exit 1
if ! mkdir "$STATE/lock" 2>/dev/null; then
  log "another loop holds $STATE/lock (remove it if that loop is gone)"
  exit 1
fi
# A round has no turn limit, so the timeout is its only bound: refuse to start without one. Stock macOS has neither
# command; `brew install coreutils` provides gtimeout.
if command -v timeout >/dev/null; then TIMEOUT_CMD=(timeout)
elif command -v gtimeout >/dev/null; then TIMEOUT_CMD=(gtimeout)
else
  log "neither timeout nor gtimeout is on PATH; refusing to run unbounded rounds (brew install coreutils)"
  rmdir "$STATE/lock" 2>/dev/null
  exit 1
fi

# Rounds and sleeps run in the background and are waited on, because bash delays a trapped signal until a foreground
# child exits. CHILD is the one being waited on; the EXIT trap stops it so a TERM or HUP does not leave it running.
CHILD=0
cleanup() {
  if [ "$CHILD" -gt 0 ]; then kill "$CHILD" 2>/dev/null; fi
  rmdir "$STATE/lock" 2>/dev/null
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

nap() {
  sleep "$1" &
  CHILD=$!
  wait "$CHILD"
  CHILD=0
}

# Refresh the installed plugin from its marketplace. A failure is logged, never fatal: the round then runs the
# version that is installed, and the skill refuses to start when the installed guard is too old.
refresh_plugin() {
  claude plugin marketplace update "$MARKETPLACE" </dev/null || log "marketplace update failed; continuing"
  claude plugin update "$PLUGIN" </dev/null || log "plugin update failed; continuing"
}

# Back to a fresh main, then delete the local handoff-round branches whose PR is merged. A branch whose PR is not
# merged is kept and reported. Returns 1 when the checkout could not be put back on main.
reset_to_main() {
  git fetch -q origin || { log "git fetch failed"; return 1; }
  git switch -q main || { log "cannot switch to main (uncommitted work?)"; return 1; }
  git pull -q --ff-only || { log "git pull --ff-only failed"; return 1; }
  # -D is needed because squash merges leave the branch unmerged as far as git can tell, so the check is that the
  # local tip is exactly the head the merged PR had: commits made after the merge are never deleted.
  local branch tip head_oid
  for branch in $(git for-each-ref --format='%(refname:short)' 'refs/heads/handoff-round/*'); do
    tip="$(git rev-parse "$branch")"
    head_oid="$(gh pr list --head "$branch" --state merged --json headRefOid --jq '.[0].headRefOid // empty')" || head_oid=""
    if [ -n "$head_oid" ] && [ "$tip" = "$head_oid" ]; then
      git branch -D "$branch" >/dev/null && log "deleted merged branch $branch"
    else
      log "kept $branch (no merged PR with this tip)"
    fi
  done
}

failures=0
refresh_plugin
while [ ! -e "$STATE/STOP" ]; do
  if ! cd "$REPO"; then
    log "cannot enter $REPO"
    exit 1
  fi
  reset_to_main || { log "stopping: fix the checkout in $REPO and start the loop again"; exit 1; }

  out="$STATE/round-$(date '+%Y%m%d-%H%M%S').json"
  log "round starting (output: $out)"
  # Stdin is closed: claude -p would otherwise wait for it. There is no turn limit; the timeout is the bound.
  "${TIMEOUT_CMD[@]}" "$ROUND_TIMEOUT" claude -p "Run the handoff-round skill." \
    --permission-mode dontAsk --permission-prompts none --output-format json \
    --allowedTools "${TOOLS[@]}" </dev/null >"$out" 2>"$out.err" &
  CHILD=$!
  wait "$CHILD"
  status=$?
  CHILD=0

  # A round that ends in an error result (every tool call denied, for example) can still exit 0.
  if [ "$status" -eq 0 ] && grep -Eq '"is_error" *: *true' "$out"; then
    status=1
  fi

  idle=0
  noaction=0
  if [ "$status" -eq 0 ] && grep -q 'no pending hand-offs' "$out"; then
    idle=1
  elif [ "$status" -eq 0 ] && grep -q 'no action' "$out"; then
    noaction=1
  fi

  reset_to_main || { log "stopping: fix the checkout in $REPO and start the loop again"; exit 1; }
  refresh_plugin

  if [ "$status" -ne 0 ]; then
    failures=$((failures + 1))
    log "round failed with status $status ($failures of $MAX_FAILURES); see $out.err"
    if [ "$failures" -ge "$MAX_FAILURES" ]; then
      log "stopping after $failures failed rounds in a row"
      exit 1
    fi
  else
    failures=0
  fi

  if [ "$idle" = 1 ]; then
    log "nothing pending; sleeping ${IDLE_SLEEP_SECONDS}s"
    nap "$IDLE_SLEEP_SECONDS"
  elif [ "$noaction" = 1 ]; then
    log "hand-off analysed, nothing to act on; sleeping ${NOACTION_SLEEP_SECONDS}s"
    nap "$NOACTION_SLEEP_SECONDS"
  else
    nap "$SLEEP_SECONDS"
  fi
done
log "STOP file found; exiting"
