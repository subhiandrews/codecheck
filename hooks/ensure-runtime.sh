#!/bin/sh
# Ensure a venv containing the code-check package exists. Idempotent and silent when current.
# The venv is built in a temp directory and swapped in only on success, so a failed (re)build
# leaves the previous venv and marker untouched. Concurrent runs are serialized by a lock
# directory with an owner token and a heartbeat.
set -eu
. "$(dirname "$0")/common.sh"
WANT="$(cat "$ROOT/VERSION" 2>/dev/null || echo dev)"
STALE="${CODE_CHECK_LOCK_STALE_SECS:-1800}"
HEARTBEAT=$((STALE / 3))
[ "$HEARTBEAT" -ge 1 ] || HEARTBEAT=1
WAIT_MAX=$((STALE + 60))

current() {
  [ -x "$VENV/bin/python" ] && [ "$(cat "$DATA/installed-version" 2>/dev/null || echo none)" = "$WANT" ]
}

current && exit 0
mkdir -p "$DATA" || { echo "code-check: cannot create $DATA" >&2; exit 1; }

LOCK="$DATA/.lock"
TOKEN="$$-$(date +%s)"
owns_lock() { [ "$(cat "$LOCK/owner" 2>/dev/null || true)" = "$TOKEN" ]; }

waited=0
until mkdir "$LOCK" 2>/dev/null; do
  if [ ! -e "$LOCK" ]; then
    echo "code-check: cannot create lock $LOCK (is $DATA writable?)" >&2
    exit 1
  fi
  seen="$(cat "$LOCK/owner" 2>/dev/null || true)"
  beat="$(cat "$LOCK/beat" 2>/dev/null || true)"
  case "$beat" in
    ''|*[!0-9]*) ;;
    *)
      if [ $(( $(date +%s) - beat )) -gt "$STALE" ]; then
        # Breaking is serialized by a short-lived break lock; under it the lock is re-read and
        # removed only if it is still the exact stale one we saw, so a fresh lock is never removed.
        if mkdir "$LOCK.break" 2>/dev/null; then
          date +%s > "$LOCK.break/t"
          if [ "$(cat "$LOCK/owner" 2>/dev/null || true)" = "$seen" ] && [ "$(cat "$LOCK/beat" 2>/dev/null || true)" = "$beat" ]; then
            rm -rf "$LOCK"
          fi
          rm -rf "$LOCK.break"
        else
          bt="$(cat "$LOCK.break/t" 2>/dev/null || true)"
          case "$bt" in
            ''|*[!0-9]*) ;;
            *) [ $(( $(date +%s) - bt )) -le 60 ] || rm -rf "$LOCK.break" ;;
          esac
          sleep 1
        fi
        continue
      fi
      ;;
  esac
  current && exit 0
  waited=$((waited + 1))
  if [ "$waited" -gt "$WAIT_MAX" ]; then
    echo "code-check: timed out after ${WAIT_MAX}s waiting for another install; remove $LOCK if no install is running" >&2
    exit 1
  fi
  sleep 1
done
printf '%s' "$TOKEN" > "$LOCK/owner"
date +%s > "$LOCK/beat"

NEW="$VENV.new.$$"
OLD="$VENV.old.$$"
HBPID=""
cleanup() {
  [ -z "$HBPID" ] || kill "$HBPID" 2>/dev/null || true
  rm -rf "$NEW"
  if [ -e "$OLD" ]; then
    if [ -e "$VENV" ]; then rm -rf "$OLD"; else mv "$OLD" "$VENV" 2>/dev/null || true; fi
  fi
  if owns_lock; then rm -rf "$LOCK"; fi
}
trap cleanup EXIT
trap 'exit 1' INT TERM HUP

(
  while :; do
    sleep "$HEARTBEAT"
    owns_lock || exit 0
    date +%s > "$LOCK/beat"
  done
) >/dev/null 2>&1 &
HBPID=$!

# Another process may have finished while we waited.
current && exit 0

rm -rf "$VENV".new.* "$VENV".old.* 2>/dev/null || true
if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.14 "$NEW" >&2
  uv pip install --python "$NEW/bin/python" --quiet "$ROOT" >&2
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
  "$PY" -m venv "$NEW" >&2
  "$NEW/bin/python" -m pip install --quiet "$ROOT" >&2
fi

# Commit: only if we still hold the lock, so a broken lock never yields two swaps.
owns_lock || { echo "code-check: lost the install lock; aborting" >&2; exit 1; }
if [ -e "$VENV" ]; then mv "$VENV" "$OLD"; fi
mv "$NEW" "$VENV"
printf '%s' "$WANT" > "$DATA/installed-version.tmp.$$"
mv "$DATA/installed-version.tmp.$$" "$DATA/installed-version"
rm -rf "$OLD"
