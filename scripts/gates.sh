#!/usr/bin/env bash
# Commit gates: deterministic golden replay always, live snippet eval only when
# the staged files touch review behaviour (scripts/gate-paths.txt).
#   scripts/gates.sh [--if-no-git-hook]
# --if-no-git-hook: do nothing when the git pre-commit hook is installed
# (core.hooksPath = .githooks), because that hook runs these gates itself.
# CODE_CHECK_BIN overrides the CLI command (default: .venv/bin/python -m codecheck).
set -u

# Work from the repository root regardless of cwd; git may export GIT_DIR and
# GIT_INDEX_FILE (inside a hook), which keep the git calls below on the right repo.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

CLAUDE_HOOK=0
if [ "${1:-}" = "--if-no-git-hook" ]; then
  if [ "$(git config core.hooksPath 2>/dev/null)" = ".githooks" ]; then
    exit 0
  fi
  CLAUDE_HOOK=1
fi

# BIN is an array so a repository path containing spaces survives; only the
# user-supplied CODE_CHECK_BIN override is word-split (it may carry arguments).
if [ -n "${CODE_CHECK_BIN:-}" ]; then
  read -r -a BIN <<< "$CODE_CHECK_BIN"
else
  if [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo "$ROOT/.venv/bin/python not found (pip install -e '.[dev]'); skipping eval gates" >&2
    exit 0
  fi
  BIN=("$ROOT/.venv/bin/python" -m codecheck)
fi

ERRFILE="$(mktemp "${TMPDIR:-/tmp}/code-check-gates.XXXXXX")" || exit 1
trap 'rm -f "$ERRFILE"' EXIT
trap 'exit 130' INT TERM HUP

# run_gate LABEL ARGS...: stdout (the JSON) is parsed, stderr is kept apart so a
# warning on a passing run cannot fail it; both are shown on failure.
run_gate() {
  local label="$1" out
  shift
  out="$("${BIN[@]}" "$@" 2>"$ERRFILE")"
  if printf '%s' "$out" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("passed") is True else 1)' 2>/dev/null; then
    return 0
  fi
  echo "$label failed:" >&2
  printf '%s\n' "$out" >&2
  cat "$ERRFILE" >&2
  return 1
}

run_gate "eval --replay" eval --replay || exit 1

# Changed paths, listed once. The git hook runs after staging, so the index is
# authoritative. The Claude hook runs before the commit command (which may still
# `git add` or use -a), so it also counts unstaged and untracked changes; that is
# deliberately conservative: it can run the live gate extra, never skip it.
changed="$(git diff --cached --name-only --no-renames 2>/dev/null)"
if [ "$CLAUDE_HOOK" = 1 ]; then
  changed="$changed
$(git diff --name-only --no-renames HEAD 2>/dev/null || true)
$(git ls-files --others --exclude-standard 2>/dev/null || true)"
fi

gated=0
if [ -f "$ROOT/scripts/gate-paths.txt" ]; then
  while IFS= read -r pattern; do
    [ -n "$pattern" ] || continue
    if grep -Eq -- "$pattern" <<< "$changed"; then
      gated=1
      break
    fi
  done < "$ROOT/scripts/gate-paths.txt"
fi

if [ "$gated" = 1 ]; then
  run_gate "eval --live" eval --live --if-reachable || exit 1
fi
exit 0
