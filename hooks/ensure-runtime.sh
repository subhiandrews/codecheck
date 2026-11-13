#!/bin/sh
# Ensure a venv containing the code-check package exists. Idempotent and silent when current.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${CODE_CHECK_HOME:-${CLAUDE_PLUGIN_DATA:-$HOME/.local/share/code-check}}"
VENV="$DATA/venv"
WANT="$(cat "$ROOT/VERSION" 2>/dev/null || echo dev)"
HAVE="$(cat "$DATA/installed-version" 2>/dev/null || echo none)"
if [ -x "$VENV/bin/python" ] && [ "$WANT" = "$HAVE" ]; then
  exit 0
fi
mkdir -p "$DATA"
if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.14 --allow-existing "$VENV" >&2
  uv pip install --python "$VENV/bin/python" --quiet "$ROOT" >&2
else
  PY=""
  for c in python3.14 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 14) else 1)'; then
      PY="$c"
      break
    fi
  done
  if [ -z "$PY" ]; then
    echo "code-check needs Python >= 3.14 (3.14.10 or later recommended); install it or install uv" >&2
    exit 1
  fi
  "$PY" -m venv "$VENV" >&2
  "$VENV/bin/python" -m pip install --quiet "$ROOT" >&2
fi
printf '%s' "$WANT" > "$DATA/installed-version"
