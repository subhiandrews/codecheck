"""Redact secrets before anything is written to the shadow store."""

import re

REDACTED = "[REDACTED]"
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----[\s\S]{0,4000}?-----END [A-Z ]{0,20}PRIVATE KEY-----"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"(?i)\bauthorization\b\s*[:=]\s*(?:bearer\s+|basic\s+|token\s+)?\S+"), f"Authorization: {REDACTED}"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"), f"Bearer {REDACTED}"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{1,2000}\.[A-Za-z0-9_-]{1,2000}\.[A-Za-z0-9_-]{1,2000}"), REDACTED),
    (re.compile(
        r"""(?i)((?:api[_-]?key|secret|token|password|passwd)\w{0,40})(["']?)(\s*[:=]\s*)"""
        r"""(?:"[^"\n]{0,500}"?|'[^'\n]{0,500}'?|[^\s"',;]+)"""), rf"\1\2\3{REDACTED}"),
]


def scrub_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub(obj):
    if isinstance(obj, str):
        return scrub_text(obj)
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj
