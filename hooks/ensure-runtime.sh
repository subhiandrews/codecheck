#!/bin/sh
# Ensure a venv containing the code-check package exists. Idempotent and silent when current.
# A half-built venv is never left behind, and concurrent runs are serialized by a lock.
set -eu
. "$(dirname "$0")/common.sh"
WANT="$(cat "$ROOT/VERSION" 2>/dev/null || echo dev)"

current() {
  [ -x "$VENV/bin/python" ] && [ "$(cat "$DATA/installed-version" 2>/dev/null || echo none)" = "$WANT" ]
}

current && exit 0
mkdir -p "$DATA"

LOCK="$DATA/.lock"
waited=0
until mkdir "$LOCK" 2>/dev/null; do
  # Break a lock left behind by a killed process (older than 10 minutes).
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
    rm -rf "$LOCK"
    continue
  fi
  current && exit 0
  waited=$((waited + 1))
  if [ "$waited" -gt 900 ]; then
    echo "code-check: timed out waiting for another install ($LOCK)" >&2
    exit 1
  fi
  sleep 1
done

OK=0
cleanup() {
  [ "$OK" = 1 ] || rm -rf "$VENV" "$DATA/installed-version"
  rm -rf "$LOCK"
}
trap cleanup EXIT
trap 'exit 1' INT TERM HUP

# Another process may have finished while we waited.
if current; then
  OK=1
  exit 0
fi

rm -rf "$VENV" "$DATA/installed-version"
if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.14 "$VENV" >&2
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
OK=1
