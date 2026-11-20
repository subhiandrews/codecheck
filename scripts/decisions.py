"""Check and create decision records (docs/decisions/NNNN-title.md).

A record is one decision in at most 100 words. The word count covers the whole file except the Status line, which is
fixed-format metadata, so superseding an old record never pushes it over the limit.

    .venv/bin/python scripts/decisions.py check     # validate every record (also run by pytest and pre-commit)
    .venv/bin/python scripts/decisions.py new --title T --decision D --why W --rejected R [--evidence PATH] [--supersedes NNNN] [--dry-run]

`new` refuses a record over the limit and writes nothing; `--dry-run` only reports the word count.
"""
import argparse
import re
import sys
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
    return sorted(p for p in d.glob("[0-9][0-9][0-9][0-9]-*.md"))


def _check_record(path: Path, root: Path) -> tuple[list[str], int | None]:
    """Problems with one record and the record number it supersedes-by (None when it is accepted)."""
    out: list[str] = []
    name = path.name
    text = path.read_text()
    lines = text.splitlines()
    if not NAME_RE.match(name):
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
    if evidence and not (root / evidence.split("#", 1)[0]).is_file():
        out.append(f"{name}: Evidence {evidence} does not exist")
    count = words(text)
    if count > LIMIT:
        out.append(f"{name}: {count} words, limit {LIMIT} ({count - LIMIT} over)")
    return out, (int(status.group(2)) if status and status.group(2) else None)


def problems(d: Path) -> list[str]:
    """Every rule violation in the decisions directory; empty when it is clean."""
    d = Path(d)
    root = d.parent.parent
    out: list[str] = []
    files = _records(d)
    nums = []
    superseded: dict[int, int] = {}
    for path in files:
        found, by = _check_record(path, root)
        out.extend(found)
        if NAME_RE.match(path.name):
            nums.append(int(path.name[:4]))
            if by is not None:
                superseded[int(path.name[:4])] = by
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
    index = (d / "README.md").read_text() if (d / "README.md").is_file() else None
    for path in files:
        if index is None or path.name not in index:
            out.append(f"{path.name}: not listed in README.md")
    return out


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _new(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    nums = [int(p.name[:4]) for p in _records(d)]
    num = max(nums, default=0) + 1
    slug = _slug(args.title)
    if not slug:
        print("decisions: the title needs letters or digits", file=sys.stderr)
        return 1
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        print("decisions: --date must be YYYY-MM-DD", file=sys.stderr)
        return 1
    text = render(num, args.title, args.date, args.decision, args.why, args.rejected, args.evidence)
    count = words(text)
    if count > LIMIT:
        print(f"decisions: this record is {count} words, {count - LIMIT} over the limit of {LIMIT}. "
              "Cut words from the title, Decision, Why or Rejected; nothing was written.", file=sys.stderr)
        return 1
    old_path = None
    if args.supersedes:
        old_path = next((p for p in _records(d) if p.name.startswith(args.supersedes + "-")), None)
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
    target = d / f"{num:04d}-{slug}.md"
    target.write_text(text)
    if old_path is not None:
        old_lines[1] = f"Status: superseded by {num:04d} (accepted {m.group(1)})"
        old_path.write_text("\n".join(old_lines) + "\n")
    index = d / "README.md"
    if index.is_file():
        with index.open("a") as fh:
            fh.write(f"- [{target.name}]({target.name}): {args.title}\n")
    print(f"{target} ({count}/{LIMIT} words)")
    return 0


def _check(args: argparse.Namespace) -> int:
    found = problems(Path(args.dir))
    for p in found:
        print(p, file=sys.stderr)
    if not found:
        print(f"decisions: {len(_records(Path(args.dir)))} records ok")
    return 1 if found else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="decisions.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="validate every record")
    check.add_argument("--dir", default=str(DEFAULT_DIR))
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
