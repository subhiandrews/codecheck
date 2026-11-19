"""Structured output shared by the Codex schema and the capture validator."""

from codecheck.core.findings import validate_findings
from codecheck.core.paths import check_finding_paths
from codecheck.errors import CodeCheckError
from codecheck.models import Severity
from codecheck.shadow.scrub import scrub_text

TEXT = {"type": "string"}
REVIEWERS = {"type": "array", "items": TEXT}
LINE = {"type": "integer"}
DROPPED_REASONS = (
    "duplicate", "not-verified", "linter", "out-of-scope", "low-value", "already-raised", "unsupported-style",
)


def _object(**properties) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _array(items: dict) -> dict:
    return {"type": "array", "items": items}


def capture_schema(aggregator: bool) -> dict:
    location = {"file": TEXT, "line": LINE, "message": TEXT}
    finding = {
        **location, "category": TEXT, "severity": {**TEXT, "enum": [s.value for s in Severity]},
        "suggestion": {"type": ["string", "null"]},
    }
    if aggregator:
        finding["reviewers"] = REVIEWERS
        return _object(findings=_array(_object(**finding)), dropped=_array(_object(
            **location, reviewers=REVIEWERS, reason={**TEXT, "enum": list(DROPPED_REASONS)},
        )))
    return _object(findings=_array(_object(**finding)))


def _matches(value, schema: dict) -> bool:
    kind = schema["type"]
    if kind == "object":
        props = schema["properties"]
        return isinstance(value, dict) and value.keys() == props.keys() and all(
            _matches(value[key], shape) for key, shape in props.items()
        )
    if kind == "array":
        return isinstance(value, list) and all(_matches(item, schema["items"]) for item in value)
    if kind == "integer":
        return type(value) is int and value > 0
    if kind == ["string", "null"]:
        return value is None or isinstance(value, str)
    return isinstance(value, str) and bool(value.strip()) and ("enum" not in schema or value in schema["enum"])


def validate_capture(raw, schema: dict, member: str, root: str, reviewers: set[str]):
    if not _matches(raw, schema):
        raise CodeCheckError("output does not match the findings schema")
    kept, errors = validate_findings(raw["findings"], reviewer=member, root=root)
    kept, errors = check_finding_paths(kept, errors, root, member)
    if errors:
        raise CodeCheckError("output contains invalid findings")
    findings = [finding.to_dict() for finding in kept]
    if member != "aggregator":
        return findings

    dropped = raw["dropped"]
    all_attributions = [item["reviewers"] for item in raw["findings"] + dropped]
    if any(not names or not set(names) <= reviewers for names in all_attributions):
        raise CodeCheckError("output refers to an unknown or missing reviewer")
    return {"findings": findings, "dropped": [{**item, "message": scrub_text(item["message"])} for item in dropped]}
