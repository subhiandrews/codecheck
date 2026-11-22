"""Keep the code-check version pinned in CI (`code-check-ref:` in .github/workflows/code-check.yml) current.

    git tag --merged origin/main | python scripts/update_ci_pin.py --tags-from-stdin [--workflow PATH] [--write]

Prints one JSON line {pin, newest, action, reason}; action is none, bump or skip. A pin that is not a plain
vMAJOR.MINOR.PATCH tag (a commit SHA, a branch, anything unparseable) is reported with action skip and never rewritten.
Only a strictly newer release tag ever replaces the pin, so it cannot downgrade. Exit status is 0 except for usage errors.
"""
import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "code-check.yml"
TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
PIN_RE = re.compile(r"^(?P<head>\s*code-check-ref:[ \t]*)(?P<value>[^\s#]+)(?P<tail>.*)$", re.MULTILINE)


def parse_tag(tag: str) -> tuple[int, int, int] | None:
    m = TAG_RE.match(tag.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def current_pin(text: str) -> str | None:
    m = PIN_RE.search(text)
    return m["value"].strip("'\"") if m else None


def newest_tag(tags: list[str]) -> str | None:
    valid = [(parse_tag(t), t.strip()) for t in tags if parse_tag(t)]
    return max(valid)[1] if valid else None


def needs_bump(pin: str | None, newest: str | None) -> bool:
    old, new = parse_tag(pin or ""), parse_tag(newest or "")
    return old is not None and new is not None and new > old


def bump(text: str, tag: str) -> str:
    if not TAG_RE.match(tag):
        raise ValueError(f"not a release tag: {tag!r}")
    return PIN_RE.sub(lambda m: f"{m['head']}{tag}{m['tail']}", text, count=1)


def decide(text: str, tags: list[str]) -> dict:
    pin, newest = current_pin(text), newest_tag(tags)
    if pin is None:
        return {"pin": pin, "newest": newest, "action": "skip", "reason": "no code-check-ref pin found"}
    if parse_tag(pin) is None:
        return {"pin": pin, "newest": newest, "action": "skip", "reason": "pin is not a vX.Y.Z tag; not rewriting it"}
    if newest is None:
        return {"pin": pin, "newest": newest, "action": "skip", "reason": "no release tags given"}
    if needs_bump(pin, newest):
        return {"pin": pin, "newest": newest, "action": "bump", "reason": f"{newest} is newer than {pin}"}
    return {"pin": pin, "newest": newest, "action": "none", "reason": "pin is current"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workflow", type=Path, default=DEFAULT_WORKFLOW)
    ap.add_argument("--tags-from-stdin", action="store_true", required=True, help="read tags, one per line, from stdin")
    ap.add_argument("--write", action="store_true", help="rewrite the pin in place when the action is bump")
    args = ap.parse_args(argv)
    text = args.workflow.read_text(encoding="utf-8")
    result = decide(text, sys.stdin.read().split())
    if result["action"] == "bump" and args.write:
        args.workflow.write_text(bump(text, result["newest"]), encoding="utf-8")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
