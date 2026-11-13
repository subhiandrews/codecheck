#!/usr/bin/env bash
# Commit gates: deterministic golden replay always, live snippet eval only when
# the staged files touch review behaviour (scripts/gate-paths.txt).
#   scripts/gates.sh [--if-no-git-hook]
# --if-no-git-hook: do nothing when the git pre-commit hook is installed
# (core.hooksPath = .githooks), because that hook runs these gates itself.
# CODE_CHECK_BIN overrides the CLI command (default: .venv/bin/python -m codecheck).
# It is a command line, word-split on whitespace, not a path: a path containing
# spaces cannot be used there.
# On success one summary line goes to stderr; stdout stays empty.
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

# The JSON parser: the CLI's own interpreter by default, else python3 when there
# is one. A missing or broken parser (no python3, the macOS command line tools
# stub) must never block a commit over valid JSON, so passes_json falls back to grep.
PARSER=()
if [ -z "${CODE_CHECK_BIN:-}" ]; then
  PARSER=("$ROOT/.venv/bin/python")
elif command -v python3 >/dev/null 2>&1; then
  PARSER=(python3)
fi
PARSE='import json, sys
try:
    ok = json.load(sys.stdin).get("passed") is True
except Exception:
    ok = False
sys.exit(0 if ok else 3)'

# passes_json TEXT: 0 when the document's top-level "passed" is true. The
# parser exits 0 or 3; any other status means the parser itself did not run.
passes_json() {
  local rc=99
  if [ "${#PARSER[@]}" -gt 0 ]; then
    printf '%s' "$1" | "${PARSER[@]}" -c "$PARSE" >/dev/null 2>&1
    rc=$?
  fi
  case "$rc" in
    0) return 0 ;;
    3) return 1 ;;
  esac
  # Fallback: the first "passed" key in the output is the top-level one.
  printf '%s' "$1" | grep -Eo '"passed"[[:space:]]*:[[:space:]]*(true|false)' | head -n 1 | grep -Eq 'true$'
}

ERRFILE="$(mktemp "${TMPDIR:-/tmp}/code-check-gates.XXXXXX")" || exit 1
trap 'rm -f "$ERRFILE"' EXIT
trap 'exit 130' INT TERM HUP

# run_gate LABEL ARGS...: stdout (the JSON) is parsed, stderr is kept apart so a
# warning on a passing run cannot fail it; both are shown on failure.
LAST_OUT=""
run_gate() {
  local label="$1" out
  shift
  out="$("${BIN[@]}" "$@" 2>"$ERRFILE")"
  LAST_OUT="$out"
  if passes_json "$out"; then
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

live="skipped (no gated path)"
if [ "$gated" = 1 ]; then
  run_gate "eval --live" eval --live --if-reachable || exit 1
  if grep -Eq '"skipped"[[:space:]]*:[[:space:]]*"' <<< "$LAST_OUT"; then
    live="skipped (no model reachable)"
  else
    live="passed"
  fi
fi
echo "gates: replay passed; live $live" >&2
exit 0
