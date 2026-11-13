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
WAIT_MAX=$((STALE + ${CODE_CHECK_WAIT_MARGIN_SECS:-60}))

current() {
  [ -x "$VENV/bin/python" ] && [ "$(cat "$DATA/installed-version" 2>/dev/null || echo none)" = "$WANT" ]
}

current && exit 0
mkdir -p "$DATA" || { echo "code-check: cannot create $DATA" >&2; exit 1; }

LOCK="$DATA/.lock"
TOKEN="$$-$(date +%s)"
owns_lock() { [ "$(cat "$LOCK/owner" 2>/dev/null || true)" = "$TOKEN" ]; }
# Take lock directory $1 with token $2. mkdir alone is not a reliable test-and-set: a mkdir that
# checks for existence and then creates, treating "already exists" as success, lets several racers
# all "create" the same directory. The lock belongs to whoever creates the owner file inside it;
# the shell's noclobber redirection opens it with O_EXCL, so exactly one racer wins.
claim() { mkdir "$1" 2>/dev/null && ( set -C; printf '%s' "$2" > "$1/owner" ) 2>/dev/null; }

BREAK_STALE="${CODE_CHECK_BREAK_STALE_SECS:-60}"
waited=0
nobeat=0
brk=0
gone=0
until claim "$LOCK" "$TOKEN"; do
  if [ ! -e "$LOCK" ]; then
    # Our mkdir lost to a lock that has since vanished: its holder finished, or a stale-breaker removed it,
    # between the mkdir and this test. That is a race to retry, not a failure. Only a data directory we cannot
    # write to (or a mkdir that keeps failing without any lock in the way) is an error.
    gone=$((gone + 1))
    if [ ! -d "$DATA" ] || [ ! -w "$DATA" ] || [ "$gone" -gt 5 ]; then
      echo "code-check: cannot create lock $LOCK (is $DATA writable?)" >&2
      exit 1
    fi
    continue
  fi
  gone=0
  waited=$((waited + 1))
  if [ "$waited" -gt "$WAIT_MAX" ]; then
    echo "code-check: timed out after ${WAIT_MAX}s waiting for another install; remove $LOCK if no install is running" >&2
    exit 1
  fi
  current && exit 0
  # The break mutex is held only for milliseconds; one that persists is from a dead breaker.
  if [ -d "$LOCK.break" ]; then
    brk=$((brk + 1))
    if [ "$brk" -gt "$BREAK_STALE" ]; then
      if mv "$LOCK.break" "$LOCK.break.dead.$$" 2>/dev/null; then rm -rf "$LOCK.break.dead.$$"; fi
      brk=0
    fi
  else
    brk=0
  fi
  seen="$(cat "$LOCK/owner" 2>/dev/null || true)"
  beat="$(cat "$LOCK/beat" 2>/dev/null || true)"
  stale=0
  case "$beat" in
    ''|*[!0-9]*)
      # No readable heartbeat: treat as dead once it has stayed that way longer than the threshold.
      nobeat=$((nobeat + 1))
      [ "$nobeat" -le "$STALE" ] || stale=1
      ;;
    *)
      nobeat=0
      [ $(( $(date +%s) - beat )) -le "$STALE" ] || stale=1
      ;;
  esac
  if [ "$stale" = 1 ] && claim "$LOCK.break" "$TOKEN"; then
    # Under the break mutex, remove the lock only if it is still the exact stale one we saw,
    # so a fresh lock is never removed.
    if [ "$(cat "$LOCK/owner" 2>/dev/null || true)" = "$seen" ] && [ "$(cat "$LOCK/beat" 2>/dev/null || true)" = "$beat" ]; then
      rm -rf "$LOCK"
    fi
    rm -rf "$LOCK.break"
    nobeat=0
    continue
  fi
  sleep 1
done
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

P=$$
(
  while kill -0 "$P" 2>/dev/null; do
    sleep "$HEARTBEAT"
    kill -0 "$P" 2>/dev/null || exit 0
    owns_lock || exit 0
    date +%s > "$LOCK/beat"
  done
) >/dev/null 2>&1 &
HBPID=$!

# We hold the lock: leftovers belong to dead runs. Restore an orphaned previous venv if the
# live one is missing, drop everything else.
for o in "$VENV".old.*; do
  [ -e "$o" ] || continue
  if [ ! -e "$VENV" ]; then mv "$o" "$VENV" 2>/dev/null || true; else rm -rf "$o"; fi
done
rm -rf "$VENV".new.* 2>/dev/null || true

# Another process may have finished while we waited.
current && exit 0

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
