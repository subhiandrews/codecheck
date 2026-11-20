"""Check and create decision records (docs/decisions/NNNN-title.md).

A record is one decision in at most 100 words. The word count covers the whole file except the Status line, which is
fixed-format metadata, so superseding an old record never pushes it over the limit.

    .venv/bin/python scripts/decisions.py check          # validate working-tree records
    .venv/bin/python scripts/decisions.py check --staged # validate the Git index (pre-commit)
    .venv/bin/python scripts/decisions.py new --title T --decision D --why W --rejected R [--evidence PATH] [--supersedes NNNN] [--dry-run]

`new` refuses a record over the limit and writes nothing; `--dry-run` only reports the word count.
"""
import argparse
import fcntl
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

LIMIT = 100
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "docs" / "decisions"
FIELDS = ("Decision", "Why", "Rejected")
OPTIONAL = ("Evidence",)
TITLE_RE = re.compile(r"^# (\d{4}) (\S.*)$")
STATUS_RE = re.compile(r"^Status: (?:accepted (\d{4}-\d{2}-\d{2})|superseded by (\d{4}) \(accepted (\d{4}-\d{2}-\d{2})\))$")
NAME_RE = re.compile(r"^(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")


def words(text: str) -> int:
    """Whitespace-separated words in the record, not counting the Status line."""
    return sum(len(line.split()) for line in text.splitlines() if not line.startswith("Status:"))


def render(num: int, title: str, day: str, decision: str, why: str, rejected: str, evidence: str | None) -> str:
    lines = [f"# {num:04d} {title}", f"Status: accepted {day}", f"Decision: {decision}", f"Why: {why}",
             f"Rejected: {rejected}"]
    if evidence:
        lines.append(f"Evidence: {evidence}")
    return "\n".join(lines) + "\n"


def _records(d: Path) -> list[Path]:
    return sorted(p for p in d.glob("*.md") if p.name != "README.md")


def _check_record(name: str, text: str, evidence_exists: Callable[[str], bool]) -> tuple[list[str], int | None]:
    """Problems with one record and the record number it supersedes-by (None when it is accepted)."""
    out: list[str] = []
    lines = text.splitlines()
    if not NAME_RE.fullmatch(name):
        out.append(f"{name}: file name must be NNNN-lowercase-words.md")
    title = TITLE_RE.match(lines[0]) if lines else None
    if not title:
        out.append(f"{name}: first line must be '# NNNN Title'")
    elif title.group(1) != name[:4]:
        out.append(f"{name}: title number {title.group(1)} does not match the file name")
    status = STATUS_RE.match(lines[1]) if len(lines) > 1 else None
    if not status:
        out.append(f"{name}: second line must be 'Status: accepted YYYY-MM-DD' or "
                   "'Status: superseded by NNNN (accepted YYYY-MM-DD)'")
    seen = {}
    for line in lines[2:]:
        if not line.strip():
            continue
        key = line.split(":", 1)[0]
        if ":" not in line or key not in FIELDS + OPTIONAL:
            out.append(f"{name}: unexpected line {line[:40]!r}; use only {', '.join(FIELDS + OPTIONAL)}")
        elif key in seen:
            out.append(f"{name}: {key} appears twice")
        else:
            seen[key] = line.split(":", 1)[1].strip()
    out.extend(f"{name}: missing {f}:" for f in FIELDS if not seen.get(f))
    evidence = seen.get("Evidence")
    if evidence and not evidence_exists(evidence.split("#", 1)[0]):
        out.append(f"{name}: Evidence {evidence} does not exist")
    count = words(text)
    if count > LIMIT:
        out.append(f"{name}: {count} words, limit {LIMIT} ({count - LIMIT} over)")
    return out, (int(status.group(2)) if status and status.group(2) else None)


def problems(d: Path) -> list[str]:
    """Every rule violation in the decisions directory; empty when it is clean."""
    d = Path(d)
    root = d.parent.parent
    index = d / "README.md"
    records = {path.name: path.read_text(encoding="utf-8") for path in _records(d)}
    return _problems(records, index.read_text(encoding="utf-8") if index.is_file() else None,
                     lambda evidence: (root / evidence).is_file())


def _problems(records: dict[str, str], index: str | None, evidence_exists: Callable[[str], bool]) -> list[str]:
    out: list[str] = []
    nums = []
    superseded: dict[int, int] = {}
    for name, record in records.items():
        found, by = _check_record(name, record, evidence_exists)
        out.extend(found)
        if NAME_RE.fullmatch(name):
            nums.append(int(name[:4]))
            if by is not None:
                superseded[int(name[:4])] = by
    for n in sorted(set(nums)):
        if nums.count(n) > 1:
            out.append(f"number {n:04d} is used by more than one record")
    for n in range(1, max(nums, default=0) + 1):
        if n not in nums:
            out.append(f"numbers must be contiguous from 0001: {n:04d} is missing")
    for old, new in superseded.items():
        if new not in nums:
            out.append(f"{old:04d} is superseded by {new:04d}, which does not exist")
        elif new <= old:
            out.append(f"{old:04d} is superseded by {new:04d}, which is not a later record")
    for name in records:
        if index is None or name not in index:
            out.append(f"{name}: not listed in README.md")
    return out


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _new(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    # Lock the directory itself so concurrent writers share an inode and leave no lock file behind.
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _new_locked(args, d)
    finally:
        os.close(fd)


def _new_locked(args: argparse.Namespace, d: Path) -> int:
    records = [p for p in _records(d) if NAME_RE.fullmatch(p.name)]
    nums = [int(p.name[:4]) for p in records]
    num = max(nums, default=0) + 1
    slug = _slug(args.title)
    if not slug:
        print("decisions: the title needs letters or digits", file=sys.stderr)
        return 1
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        print("decisions: --date must be YYYY-MM-DD", file=sys.stderr)
        return 1
    for field in ("title", "decision", "why", "rejected", "evidence"):
        value = getattr(args, field)
        if value and value.splitlines() != [value]:
            print(f"decisions: --{field} must fit on one line", file=sys.stderr)
            return 1
    text = render(num, args.title, args.date, args.decision, args.why, args.rejected, args.evidence)
    target = d / f"{num:04d}-{slug}.md"
    errors, _ = _check_record(target.name, text, lambda evidence: (d.parent.parent / evidence).is_file())
    if errors:
        for error in errors:
            print(f"decisions: {error}", file=sys.stderr)
        return 1
    count = words(text)
    old_path = None
    if args.supersedes:
        old_path = next((p for p in records if p.name.startswith(args.supersedes + "-")), None)
        if old_path is None:
            print(f"decisions: no record {args.supersedes} to supersede", file=sys.stderr)
            return 1
        old_lines = old_path.read_text().splitlines()
        m = STATUS_RE.match(old_lines[1]) if len(old_lines) > 1 else None
        if not m or not m.group(1):
            print(f"decisions: {old_path.name} is not an accepted record", file=sys.stderr)
            return 1
    if args.dry_run:
        print(f"{count}/{LIMIT} words ({LIMIT - count} left)")
        return 0
    target.write_text(text, encoding="utf-8")
    if old_path is not None:
        old_lines[1] = f"Status: superseded by {num:04d} (accepted {m.group(1)})"
        old_path.write_text("\n".join(old_lines) + "\n")
    index = d / "README.md"
    if index.is_file():
        with index.open("a") as fh:
            fh.write(f"- [{target.name}]({target.name}): {args.title}\n")
    print(f"{target} ({count}/{LIMIT} words)")
    return 0


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(  # noqa: S603
        ["/usr/bin/env", "git", *args], cwd=root, check=True, stdout=subprocess.PIPE,
    ).stdout


def _staged_problems(root: Path) -> tuple[list[str], int]:
    # Intent-to-add entries appear in ls-files with empty blob IDs but are excluded from commits.
    additions = ("diff", "--cached", "--name-only", "--diff-filter=A", "--no-renames", "-z")
    intent_to_add = (
        set(_git(root, *additions, "--ita-visible-in-index").split(b"\0"))
        - set(_git(root, *additions, "--ita-invisible-in-index").split(b"\0"))
    )
    # Git selects the index, including the temporary index used for path-limited commits.
    entries = {}
    errors = []
    directory = Path("docs/decisions")
    for entry in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        if raw_path in intent_to_add:
            continue
        path = Path(os.fsdecode(raw_path))
        mode, blob, stage = metadata.split()
        if stage == b"0" and mode in {b"100644", b"100755"}:
            entries[path] = blob.decode("ascii")
        elif path.parent == directory and path.suffix == ".md":
            errors.append(f"{path}: expected a regular staged file")

    documents = {
        path.name: _git(root, "cat-file", "blob", blob).decode("utf-8")
        for path, blob in entries.items()
        if path.parent == directory and path.suffix == ".md"
    }
    index = documents.pop("README.md", None)

    def evidence_exists(evidence: str) -> bool:
        return Path(os.path.normpath(evidence)) in entries

    errors.extend(_problems(documents, index, evidence_exists))
    return errors, len(documents)


def _check(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    try:
        if args.staged:
            found, count = _staged_problems(DEFAULT_DIR.parent.parent)
        else:
            found, count = problems(d), len(_records(d))
    except (OSError, UnicodeError, subprocess.CalledProcessError) as error:
        print(f"decisions: {error}", file=sys.stderr)
        return 1
    for p in found:
        print(p, file=sys.stderr)
    if not found:
        print(f"decisions: {count} records ok")
    return 1 if found else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="decisions.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="validate every record")
    source = check.add_mutually_exclusive_group()
    source.add_argument("--dir", default=str(DEFAULT_DIR))
    source.add_argument("--staged", action="store_true", help="validate the repository’s Git index")
    check.set_defaults(fn=_check)
    new = sub.add_parser("new", help="create the next record (refuses more than 100 words)")
    new.add_argument("--dir", default=str(DEFAULT_DIR))
    new.add_argument("--title", required=True)
    new.add_argument("--date", default=date.today().isoformat())
    new.add_argument("--decision", required=True)
    new.add_argument("--why", required=True)
    new.add_argument("--rejected", required=True)
    new.add_argument("--evidence")
    new.add_argument("--supersedes", metavar="NNNN")
    new.add_argument("--dry-run", action="store_true")
    new.set_defaults(fn=_new)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
