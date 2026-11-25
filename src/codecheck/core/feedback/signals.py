"""A feedback signal: what went wrong, where, and a stable fingerprint. Detail is ids and short facts, never free text."""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from codecheck.shadow.scrub import scrub_text

MAX_VALUE = 500
KEY = re.compile(r"[a-z][a-z0-9_]{0,29}")
SOURCE = re.compile(r"[a-z][a-z0-9-]{0,29}")


@dataclass(frozen=True, slots=True)
class Signal:
    kind: str
    source: str
    fingerprint: str
    detail: dict = field(default_factory=dict)
    judged: bool = False  # true: the model noticed it; false: a deterministic detector in code did


def make_signal(kind: str, source: str, ids: Mapping[str, str | int], *, judged: bool = False) -> Signal:
    if not SOURCE.fullmatch(source):
        raise ValueError(f"signal source must be a plain label, got {source!r}")
    detail = {}
    for key, value in ids.items():
        if not KEY.fullmatch(str(key)):
            raise ValueError(f"signal id keys must be plain labels, got {key!r}")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TypeError(f"signal detail {key!r} must be a string or an integer")
        detail[str(key)] = scrub_text(value)[:MAX_VALUE] if isinstance(value, str) else value
    # The fingerprint covers the kind and every id, so the same failure dedupes across runs while two
    # different reports the user typed (their notes) stay two drafts.
    raw = kind + "\0" + "\0".join(f"{k}={detail[k]}" for k in sorted(detail))
    return Signal(kind, source, hashlib.sha256(raw.encode()).hexdigest()[:12], detail, judged)
