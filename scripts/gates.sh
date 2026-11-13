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

if [ "${1:-}" = "--if-no-git-hook" ]; then
  if [ "$(git config core.hooksPath 2>/dev/null)" = ".githooks" ]; then
    exit 0
  fi
fi

if [ -n "${CODE_CHECK_BIN:-}" ]; then
  BIN="$CODE_CHECK_BIN"
else
  if [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo "$ROOT/.venv/bin/python not found (pip install -e '.[dev]'); skipping eval gates" >&2
    exit 0
  fi
  BIN="$ROOT/.venv/bin/python -m codecheck"
fi

# Print the failing output and exit 1 unless the JSON on stdin says passed: true.
check_passed() {
  local label="$1" out="$2"
  if printf '%s' "$out" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("passed") is True else 1)' 2>/dev/null; then
    return 0
  fi
  echo "$label failed:" >&2
  printf '%s\n' "$out" >&2
  return 1
}

# $BIN is deliberately unquoted: it may be a command plus arguments.
# shellcheck disable=SC2086
out="$($BIN eval --replay 2>&1)"
check_passed "eval --replay" "$out" || exit 1

gated=0
if [ -f "$ROOT/scripts/gate-paths.txt" ]; then
  while IFS= read -r pattern; do
    [ -n "$pattern" ] || continue
    if git diff --cached --name-only | grep -Eq -- "$pattern"; then
      gated=1
      break
    fi
  done < "$ROOT/scripts/gate-paths.txt"
fi

if [ "$gated" = 1 ]; then
  # shellcheck disable=SC2086
  out="$($BIN eval --live --if-reachable 2>&1)"
  check_passed "eval --live" "$out" || exit 1
fi
exit 0
