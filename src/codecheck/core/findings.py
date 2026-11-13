"""Finding schema validation, stable IDs and deterministic duplicate merging."""

import hashlib
from dataclasses import replace

from codecheck.core.diffmap import FileDiff
from codecheck.models import Finding, Severity
from codecheck.shadow.scrub import scrub_text


def normalize_text(text: str) -> str:
    return " ".join(text.lower().split())


def finding_id(f: Finding) -> str:
    key = f"{f.file}\0{normalize_text(f.anchor)}\0{normalize_text(f.message)}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _clean_path(path: str, root: str | None) -> str:
    path = path.strip()
    if root:
        path = path.removeprefix(root.rstrip("/") + "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def validate_findings(raw: object, *, reviewer: str, root: str | None = None) -> tuple[list[Finding], list[str]]:
    if not isinstance(raw, list):
        return [], [f"{reviewer}: expected a JSON array of findings"]
    findings: list[Finding] = []
    errors: list[str] = []
    for i, item in enumerate(raw):
        where = f"{reviewer}[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: not an object")
            continue
        problems: list[str] = []
        file, line, sev = item.get("file"), item.get("line"), item.get("severity")
        message, category, suggestion = item.get("message"), item.get("category", "general"), item.get("suggestion")
        if not isinstance(file, str) or not file.strip():
            problems.append("file must be a non-empty string")
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            problems.append("line must be a positive integer")
        if not isinstance(sev, str) or sev.lower() not in {s.value for s in Severity}:
            problems.append("severity must be critical, warning or suggestion")
        if not isinstance(message, str) or not message.strip():
            problems.append("message must be a non-empty string")
        if not isinstance(category, str) or not category.strip():
            category = "general"
        if suggestion is not None and not isinstance(suggestion, str):
            problems.append("suggestion must be a string")
        if problems:
            errors.extend(f"{where}: {p}" for p in problems)
            continue
        message, category = message.strip(), category.strip()
        if category.casefold() == "security":
            # Backstop: comments are public, so never let an echoed secret through.
            message = scrub_text(message)
            suggestion = scrub_text(suggestion) if suggestion is not None else None
        supplied = item.get("reviewers")
        who = tuple(supplied) if isinstance(supplied, list) and supplied and all(isinstance(r, str) for r in supplied) else (reviewer,)
        findings.append(
            Finding(
                file=_clean_path(file, root), line=line, severity=Severity(sev.lower()),
                category=category, message=message, suggestion=suggestion,
                reviewers=who,
            )
        )
    return findings, errors


def merge_exact_duplicates(findings: list[Finding]) -> list[Finding]:
    merged: dict[tuple, Finding] = {}
    for f in findings:
        key = (f.file, f.line, f.category, normalize_text(f.message))
        if key not in merged:
            merged[key] = f
            continue
        prev = merged[key]
        reviewers = tuple(dict.fromkeys((*prev.reviewers, *f.reviewers)))
        severity = max(prev.severity, f.severity, key=lambda s: s.rank)
        merged[key] = replace(prev, severity=severity, reviewers=reviewers, suggestion=prev.suggestion or f.suggestion)
    return list(merged.values())


def attach_anchors(findings: list[Finding], files: dict[str, FileDiff]) -> list[Finding]:
    out = []
    for f in findings:
        fd = files.get(f.file)
        out.append(replace(f, anchor=fd.right_lines().get(f.line, "") if fd else ""))
    return out
