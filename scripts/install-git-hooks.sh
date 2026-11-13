#!/usr/bin/env bash
# Point this repository's git at the checked-in hooks in .githooks.
# core.hooksPath is repository-level config: it applies to every worktree.
set -eu

if ! ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "install-git-hooks: not inside a git repository" >&2
  exit 1
fi
cd "$ROOT"
git config core.hooksPath .githooks
echo "set core.hooksPath=.githooks in $ROOT (applies to all of its worktrees)"
