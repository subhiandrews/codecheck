"""Containment checks for untrusted file paths (from PRs, config and reviewer output)."""

from pathlib import Path


def contained(root: Path | str, rel: str) -> Path | None:
    """Resolve `rel` under `root`; None if it is absolute or escapes (via `..` or a symlink)."""
    if not rel or Path(rel).is_absolute() or "\0" in rel:
        return None
    base = Path(root).resolve()
    target = (base / rel).resolve()
    return target if target.is_relative_to(base) else None


def check_finding_paths(findings, errors, root, who):
    """Drop findings whose (untrusted) file path escapes the repo root; report each as an error."""
    kept = []
    errors = list(errors)
    for f in findings:
        if contained(root, f.file) is None:
            errors.append(f"{who}: finding path {f.file!r} escapes the repository")
        else:
            kept.append(f)
    return kept, errors


def finding_items(raw):
    return raw["findings"] if isinstance(raw, dict) and "findings" in raw else raw
