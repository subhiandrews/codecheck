"""Promote a hand-off run into a golden case: draft it with auto labels, then finalize it once a human confirmed them.

Drafting reads only the shadow store (offline). Finalizing refuses anything a person has not confirmed, anything
that does not replay exactly, and private (or unknown) repositories unless explicitly allowed.
"""

import json
import os
import re
from pathlib import Path

from codecheck.core.findings import _clean_path
from codecheck.errors import CodeCheckError
from codecheck.evals.golden import SCHEMA_VERSION, check_case, load_case, posted_view
from codecheck.shadow.export import finding_label, run_feedback
from codecheck.shadow.scrub import scrub
from codecheck.shadow.store import ShadowStore

# export-golden's labels as a golden case names them; anything without a clear signal is left for the human.
_LABELS = {"confirmed": "expected", "disputed": "not_expected"}
LABELS = ("expected", "not_expected")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _relative(items: object, root: str | None) -> object:
    """Finding items with `file` made repository-relative the way `post` does it, so the case replays without a root."""
    if not isinstance(items, list):
        return items
    return [{**f, "file": _clean_path(f["file"], root)} if isinstance(f, dict) and isinstance(f.get("file"), str) else f
            for f in items]


def _aggregator(raw: object, root: str | None) -> object:
    if isinstance(raw, dict) and "findings" in raw:
        return {**raw, "findings": _relative(raw["findings"], root)}
    return _relative(raw, root)


def _signals(shadow: ShadowStore, run_id: str):
    path = shadow.root / f"{run_id}.handoff" / "handoff.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("signals")
    except (OSError, ValueError, AttributeError):
        return None


def _pr_number(pr) -> int | None:
    number = str(pr or "").rpartition("#")[2]
    return int(number) if number.isdigit() else None


def draft_case(shadow: ShadowStore, run_id: str) -> dict:
    """A golden case for a hand-off run with `label_status: "auto"`, for a human to check and confirm."""
    raw = shadow.read_raw(run_id)
    if raw is None:
        raise CodeCheckError(f"no raw outputs recorded for {run_id}: only runs made with shadow.handoff can be promoted")
    diff = shadow.read_diff(run_id)
    if not diff:
        raise CodeCheckError(f"no diff recorded for {run_id}: there is nothing to replay the findings against")
    if "aggregator" not in raw:
        raise CodeCheckError(f"no aggregator output recorded for {run_id}: there is nothing to replay")
    records = shadow.read(run_id)
    run = next((r for r in records if r.get("type") == "run"), {})
    root = raw.get("root") if isinstance(raw.get("root"), str) else None
    view = posted_view(raw["aggregator"], diff, root=root)  # as `post` saw it; raises on invalid output
    outcomes, humans = run_feedback(shadow).get(run_id, ({}, {}))
    labels = [
        {"id": r["id"], "file": _clean_path(str(r.get("file")), root), "line": r.get("line"), "severity": r.get("severity"),
         "category": r.get("category"), "message": r.get("message"),
         "label": _LABELS.get(finding_label(r["id"], outcomes, humans), "unlabelled")}
        for r in records if r.get("type") == "finding" and isinstance(r.get("id"), str)
    ]
    repo, head_sha, number = run.get("repo"), run.get("head_sha"), _pr_number(run.get("pr"))
    reviewers = raw.get("reviewers") if isinstance(raw.get("reviewers"), dict) else {}
    return scrub({
        "version": SCHEMA_VERSION,
        "case_id": f"{str(repo).replace('/', '-')}-{number if number is not None else 'local'}-{str(head_sha)[:8]}",
        "repo": repo, "pr": number, "head_sha": head_sha, "diff": diff,
        "reviewer_outputs": {m: _relative(v, root) for m, v in reviewers.items()},
        "aggregator": _aggregator(raw["aggregator"], root),
        "expected_posted": view["findings"], "expected_decision": view["decision"],
        "labels": labels,
        "misses": [{k: h.get(k) for k in ("path", "line", "author", "body")} for h in humans.values() if not h.get("matches_finding")],
        "signals": _signals(shadow, run_id),
        "label_status": "auto",
        "repo_private": run.get("repo_private"),
    })


def _file_name(case: dict) -> str:
    repo, pr, sha = case.get("repo"), case.get("pr"), case.get("head_sha")
    if not isinstance(repo, str) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{8,64}", sha):
        raise CodeCheckError("golden case needs a repo and a hex head_sha to be named")
    name = f"{repo.replace('/', '-')}-{pr if isinstance(pr, int) else 'local'}-{sha[:8]}"
    if not _NAME.fullmatch(name):
        raise CodeCheckError(f"golden case name {name!r} is not a safe file name")
    return f"{name}.json"


def finalize_case(path: Path, dest_dir: Path, *, allow_private: bool) -> Path:
    """Check a confirmed draft and write it, scrubbed, as `dest_dir/<repo>-<pr>-<sha8>.json`."""
    path, dest_dir = Path(path), Path(dest_dir)
    try:
        status = json.loads(path.read_text(encoding="utf-8")).get("label_status")
    except (OSError, ValueError, AttributeError) as e:
        raise CodeCheckError(f"draft {path.name}: not a readable JSON object: {e}") from e
    if status != "confirmed":
        raise CodeCheckError(f"draft {path.name}: label_status is {status!r}; check every label, then set "
                             "label_status to \"confirmed\"")
    case = scrub(load_case(path))
    bad = [lab for lab in case["labels"] if not isinstance(lab, dict) or lab.get("label") not in LABELS]
    if bad:
        raise CodeCheckError(f"draft {path.name}: {len(bad)} label(s) are unlabelled or not one of {', '.join(LABELS)}")
    if case["repo_private"] is not False and not allow_private:
        reason = "private" if case["repo_private"] else "possibly private (visibility unknown)"
        raise CodeCheckError(f"draft {path.name}: the repository is {reason}; pass --allow-private to promote it anyway")
    problems = check_case(case)
    if problems:
        raise CodeCheckError(f"draft {path.name} does not replay: " + "; ".join(problems[:5]))
    if dest_dir.is_symlink():
        raise CodeCheckError(f"refusing to write through symlink {dest_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / _file_name(case)
    if target.is_symlink():
        raise CodeCheckError(f"refusing to write through symlink {target}")
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o644)  # a golden case is committed to the repository; the umask must not matter
            fh.write((json.dumps(case, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target
