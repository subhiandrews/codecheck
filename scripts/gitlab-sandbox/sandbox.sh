#!/usr/bin/env bash
# Create and drive a public GitLab project for testing code-check against GitLab merge requests.
# See docs/gitlab-testing.md for the whole test plan. Needs: bash, git, python3 and a logged-in `glab`.
#
#   create                       project, seed code, two issues and four branches (as the account that runs code-check)
#   add-member USER [LEVEL]      add a second account to the project (LEVEL 30 = Developer, the default)
#   open-mrs owner|reviewer      owner: the two MRs for address-comments and babysit-pr
#                                reviewer: the two MRs that code-check reviews (run as the second account)
#   comment-as-reviewer          leave review threads on the owner's MRs (run as the second account)
#   capture-fixtures DIR [IID]   save the raw API responses the GitLab provider reads, to turn into test fixtures
#   status                       list the project's merge requests and their pipelines
#   teardown                     delete the project (asks you to type its path)
#                                SANDBOX_CONFIRM_DELETE=group/name skips the prompt; the value must still equal the path
#
# SANDBOX_PROJECT=group/name picks the project (default: <your username>/code-check-sandbox). The second account
# must set it to the owner's project and run with its own GITLAB_TOKEN. GITLAB_TOKEN is also used, through
# GIT_ASKPASS, for `git push`, so it never appears in a URL, a config file or a process list.
# SANDBOX_USE_SSH=1 pushes over ssh. SANDBOX_REMOTE_URL overrides the push URL (used by the tests).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEED="$HERE/seed"
BRANCH_DIRS="feature__discount feature__clean-docs feature__report-export fix__failing-ci"
TMP=""
PROJECT_MADE=""
cleanup() {
  local rc=$?
  [ -z "$TMP" ] || rm -rf "$TMP"
  if [ "$rc" -ne 0 ] && [ -n "$PROJECT_MADE" ]; then
    echo "sandbox: create failed after the project was made; run 'teardown', then 'create' again" >&2
  fi
}
trap cleanup EXIT

die() { echo "sandbox: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required but was not found on PATH"; }

enc() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }
# jget a.b.0.c reads a value out of the JSON on stdin (list indexes are numbers).
jget() {
  python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in sys.argv[1].split("."):
    if k:
        d = d[int(k)] if isinstance(d, list) else d[k]
print(d)' "$1"
}
# jb key=value ... builds a JSON object of strings.
jb() { python3 -c 'import json, sys; print(json.dumps(dict(a.split("=", 1) for a in sys.argv[1:])))' "$@"; }

api_get() { glab api "$@"; }
api_send() { printf '%s' "$3" | glab api "$2" --method "$1" --header 'Content-Type: application/json' --input -; }

setup_project() {
  need glab; need python3
  if [ -n "${SANDBOX_PROJECT:-}" ]; then
    PROJECT="$SANDBOX_PROJECT"
  else
    PROJECT="$(api_get user | jget username)/code-check-sandbox"
  fi
  case "$PROJECT" in */*) ;; *) die "SANDBOX_PROJECT must look like group/name, got '$PROJECT'" ;; esac
  PID="$(enc "$PROJECT")"
  NS="${PROJECT%/*}"
  NAME="${PROJECT##*/}"
}

# gcommit MESSAGE: commit in the work tree with a fixed identity, so a machine without user.name and user.email still works.
gcommit() {
  git -C "$WORK" -c user.name="code-check sandbox" -c user.email="sandbox@example.invalid" commit -q -m "$1"
}

gpush() {
  if [ -n "${GITLAB_TOKEN:-}" ] && [ -z "${SANDBOX_REMOTE_URL:-}" ] && [ -z "${SANDBOX_USE_SSH:-}" ]; then
    local askpass="$TMP/askpass.sh"
    printf '#!/bin/sh\ncase "$1" in Username*) echo oauth2 ;; *) echo "$GITLAB_TOKEN" ;; esac\n' > "$askpass"
    chmod 700 "$askpass"
    GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0 git -C "$WORK" push "$@"
  else
    git -C "$WORK" push "$@"
  fi
}

# copy_seed SRC DEST: copy a seed directory, dropping the .tmpl suffix (kept so lint and pytest ignore the flawed code)
# and leaving out the per-branch metadata files.
copy_seed() {
  local src="$1" dest="$2" rel
  while IFS= read -r -d '' f; do
    rel="${f#./}"
    rel="${rel%.tmpl}"
    mkdir -p "$dest/$(dirname "$rel")"
    cp "$src/${f#./}" "$dest/$rel"
  done < <(cd "$src" && find . -type f ! -name MR.md ! -name comments.json -print0)
}

mr_title() { head -n 1 "$SEED/branches/$1/MR.md"; }
mr_description() { tail -n +3 "$SEED/branches/$1/MR.md"; }
branch_name() { echo "${1//__//}"; }

cmd_create() {
  need git
  setup_project
  if api_get "projects/$PID" >/dev/null 2>&1; then die "project $PROJECT already exists; run 'teardown' first or pick another SANDBOX_PROJECT"; fi
  local nsid
  nsid="$(api_get "namespaces/$(enc "$NS")" | jget id)"
  echo "creating public project $PROJECT"
  api_send POST projects "$(jb name="$NAME" path="$NAME" namespace_id="$nsid" visibility=public default_branch=main \
    initialize_with_readme=false description='Public sandbox for testing the code-check plugin on GitLab merge requests')" >/dev/null
  PROJECT_MADE=1

  local remote
  if [ -n "${SANDBOX_REMOTE_URL:-}" ]; then remote="$SANDBOX_REMOTE_URL"
  elif [ -n "${SANDBOX_USE_SSH:-}" ]; then remote="$(api_get "projects/$PID" | jget ssh_url_to_repo)"
  else remote="$(api_get "projects/$PID" | jget http_url_to_repo)"; fi

  TMP="$(mktemp -d)"
  WORK="$TMP/work"
  mkdir -p "$WORK"
  git -C "$WORK" init -q -b main
  git -C "$WORK" remote add origin "$remote"
  copy_seed "$SEED/main" "$WORK"
  git -C "$WORK" add -A
  gcommit "Initial import"
  gpush -q -u origin main

  # Issues first, so the numbers in the merge request descriptions ("Closes #2") are right.
  api_send POST "projects/$PID/issues" "$(jb title='total_between leaves out the end index' description='The docstring says both ends are included, but `total_between([1, 2, 3], 0, 2)` returns 3 instead of 6.')" >/dev/null
  api_send POST "projects/$PID/issues" "$(jb title='Add CSV export of totals' description='Write one row per name with the total of its values.')" >/dev/null

  local d b
  for d in $BRANCH_DIRS; do
    b="$(branch_name "$d")"
    git -C "$WORK" checkout -q -b "$b" main
    copy_seed "$SEED/branches/$d" "$WORK"
    git -C "$WORK" add -A
    gcommit "$(mr_title "$d")"
    gpush -q -u origin "$b"
    git -C "$WORK" checkout -q main
  done
  echo "done. Next: add-member <second account>, then open-mrs owner (this account) and open-mrs reviewer (second account)."
  echo "project: https://gitlab.com/$PROJECT"
}

cmd_add_member() {
  [ $# -ge 1 ] || die "usage: add-member USERNAME [ACCESS_LEVEL]"
  setup_project
  local uid
  uid="$(api_get "users?username=$(enc "$1")" | jget 0.id)"
  api_send POST "projects/$PID/members" "$(jb user_id="$uid" access_level="${2:-30}")" >/dev/null
  echo "added $1 to $PROJECT (access level ${2:-30})"
}

open_mr() {
  local out
  out="$(api_send POST "projects/$PID/merge_requests" "$(jb source_branch="$(branch_name "$1")" target_branch=main \
    title="$(mr_title "$1")" description="$(mr_description "$1")" remove_source_branch=true)")"
  echo "opened !$(printf '%s' "$out" | jget iid): $(printf '%s' "$out" | jget web_url)"
}

cmd_open_mrs() {
  setup_project
  case "${1:-}" in
    owner) open_mr feature__report-export; open_mr fix__failing-ci ;;
    reviewer) open_mr feature__discount; open_mr feature__clean-docs ;;
    *) die "usage: open-mrs owner|reviewer" ;;
  esac
}

cmd_comment_as_reviewer() {
  setup_project
  [ "$(api_get user | jget username)" != "$NS" ] || die "comment-as-reviewer must run as the second account, not the project owner ($NS)"
  local d b iid versions base start head lines line
  for d in feature__report-export fix__failing-ci; do
    b="$(branch_name "$d")"
    iid="$(api_get "projects/$PID/merge_requests?source_branch=$(enc "$b")&state=opened" | jget 0.iid)"
    versions="$(api_get "projects/$PID/merge_requests/$iid/versions")"
    base="$(printf '%s' "$versions" | jget 0.base_commit_sha)"
    start="$(printf '%s' "$versions" | jget 0.start_commit_sha)"
    head="$(printf '%s' "$versions" | jget 0.head_commit_sha)"
    lines="$(python3 - "$SEED/branches/$d/comments.json" "$base" "$start" "$head" <<'PY'
import json, sys
path, base, start, head = sys.argv[1:]
for c in json.load(open(path)):
    print(json.dumps({"body": c["body"], "position": {
        "position_type": "text", "base_sha": base, "start_sha": start, "head_sha": head,
        "new_path": c["path"], "old_path": c["path"], "new_line": c["line"]}}))
PY
)"
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      api_send POST "projects/$PID/merge_requests/$iid/discussions" "$line" >/dev/null
    done <<< "$lines"
    echo "commented on !$iid ($b)"
  done
}

cmd_capture_fixtures() {
  [ $# -ge 1 ] || die "usage: capture-fixtures DIR [MR_IID]"
  setup_project
  local out="$1" iid="${2:-}" mr pipeline
  mkdir -p "$out"
  [ -n "$iid" ] || iid="$(api_get "projects/$PID/merge_requests?state=opened" | jget 0.iid)"
  mr="projects/$PID/merge_requests/$iid"
  # /user is the one response that carries the account's private email addresses, so drop every email field.
  api_get user | python3 -c 'import json, sys
d = json.load(sys.stdin)
print(json.dumps({k: v for k, v in d.items() if "email" not in k.lower()}, indent=2))' > "$out/user.json"
  api_get "projects/$PID" > "$out/project.json"
  api_get "$mr" > "$out/mr.json"
  api_get "$mr/diffs?per_page=100" --paginate > "$out/diffs.json"
  api_get "$mr/discussions?per_page=100" --paginate > "$out/discussions.json"
  api_get "$mr/notes?sort=asc&order_by=created_at&per_page=100" --paginate > "$out/notes.json"
  api_get "$mr/versions" > "$out/versions.json"
  api_get "$mr/pipelines" > "$out/pipelines.json"
  pipeline="$(jget 0.id < "$out/pipelines.json" 2>/dev/null || true)"
  if [ -n "$pipeline" ]; then api_get "projects/$PID/pipelines/$pipeline/jobs?per_page=100" --paginate > "$out/jobs.json"; fi
  echo "saved the responses for !$iid in $out (email fields are removed from user.json; look through the rest before you commit them)"
}

cmd_status() {
  setup_project
  api_get "projects/$PID/merge_requests?state=all&per_page=50" | python3 -c '
import json, sys
for m in json.load(sys.stdin):
    print("!%s  %-7s %-14s %s" % (m["iid"], m["state"], m["author"]["username"], m["title"]))'
}

cmd_teardown() {
  setup_project
  local answer="${SANDBOX_CONFIRM_DELETE:-}"
  if [ -z "$answer" ]; then
    printf 'This permanently deletes %s. Type its path to confirm: ' "$PROJECT" >&2
    read -r answer
  fi
  [ "$answer" = "$PROJECT" ] || die "confirmation did not match $PROJECT; nothing was deleted"
  api_send DELETE "projects/$PID" '{}' >/dev/null
  echo "deleted $PROJECT"
}

case "${1:-help}" in
  create) shift; cmd_create "$@" ;;
  add-member) shift; cmd_add_member "$@" ;;
  open-mrs) shift; cmd_open_mrs "$@" ;;
  comment-as-reviewer) shift; cmd_comment_as_reviewer "$@" ;;
  capture-fixtures) shift; cmd_capture_fixtures "$@" ;;
  status) shift; cmd_status "$@" ;;
  teardown) shift; cmd_teardown "$@" ;;
  help | -h | --help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command '$1' (try: help)" ;;
esac
