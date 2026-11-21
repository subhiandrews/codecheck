"""Deterministic DeepEval metrics for the review panel: no judge, no tokens.

Each metric reads one run from `LLMTestCase.metadata` (built by scorecard.py): the diff, every reviewer's raw
findings, what the aggregator kept and dropped, and which reviewers wrote a findings file. A score is a share
(numerator over denominator), so scorecard.py can also pool them across runs. `threshold` is the target: None
means no target is agreed yet, so the metric only reports and `success` stays None.

Each one follows a finding from this session's evals:
  AnchoringShare             F6, how many reviewer findings sit on a changed line
  NoProcessVoice             issue #65, the aggregator's own reasoning inside a posted message
  MessageLength              issue #65, long messages (the target length is not decided)
  NoCommentCapFindings       issue #66, the built-in comment cap showing up as a finding
  FindingsFileWritten        issue #64, reviewers that finish without writing their file
  AggregatorAccounting       whether the reviewers' findings are named by a kept or dropped item
"""

from __future__ import annotations

import re
import statistics
from typing import ClassVar

import _env  # noqa: F401  (telemetry opt-out, must run before deepeval is imported)
from anchoring import CATEGORIES, check_findings
from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase, SingleTurnParams

PROCESS_VOICE = re.compile(
    r"\b(downgraded|lowered from|I could not|I couldn't|I did not|not verified|could not confirm|reviewers said)\b", re.IGNORECASE
)
COMMENT_CAP = re.compile(
    r"comments? (are|exceed|cap)|over the cap|comment (lines|density)|cap of 3%|3% cap|added comments", re.IGNORECASE
)


def _messages(meta: dict) -> list[str]:
    return [f.get("message", "") for f in meta.get("kept") or [] if isinstance(f, dict)]


def _counts(findings) -> int:
    return len(findings) if isinstance(findings, list) else 0


class DeterministicMetric(BaseMetric):
    """Base for metrics computed from a run's metadata. A subclass sets `name` and implements `applies` and `compute`."""

    _required_params: ClassVar[list[SingleTurnParams]] = [SingleTurnParams.METADATA]
    name = "deterministic"

    def __init__(self, threshold: float | None = None) -> None:
        self.threshold = threshold
        self.score = None
        self.success = None
        self.reason = None
        self.numerator = 0
        self.denominator = 0
        self.async_mode = False
        self.include_reason = True

    @classmethod
    def applies(cls, meta: dict) -> bool:
        raise NotImplementedError

    def compute(self, meta: dict) -> tuple[int, int, str]:
        """Return (numerator, denominator, reason); only called when `applies` is true."""
        raise NotImplementedError

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        self.numerator, self.denominator, self.reason = self.compute(test_case.metadata or {})
        self.score = self.numerator / self.denominator if self.denominator else 1.0
        self.success = None if self.threshold is None else self.score >= self.threshold
        return self.score

    async def a_measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return bool(self.success)

    @property
    def __name__(self) -> str:
        return self.name


class AnchoringShare(DeterministicMetric):
    name = "Reviewer findings on an added line"

    @classmethod
    def applies(cls, meta: dict) -> bool:
        return bool(meta.get("diff")) and any(_counts(f) for f in (meta.get("reviewers") or {}).values())

    def compute(self, meta: dict) -> tuple[int, int, str]:
        rows = check_findings(meta["diff"], meta["reviewers"], meta.get("root"))
        counts = {c: sum(r["category"] == c for r in rows) for c in (*CATEGORIES, "malformed")}
        return counts["added"], len(rows), ", ".join(f"{c} {n}" for c, n in counts.items())


class NoProcessVoice(DeterministicMetric):
    name = "Kept messages without aggregator process notes"

    @classmethod
    def applies(cls, meta: dict) -> bool:
        return bool(_messages(meta))

    def compute(self, meta: dict) -> tuple[int, int, str]:
        messages = _messages(meta)
        hits = [m for m in messages if PROCESS_VOICE.search(m)]
        phrases = sorted({p.group(0).lower() for m in hits for p in PROCESS_VOICE.finditer(m)})
        return len(messages) - len(hits), len(messages), f"{len(hits)} with process voice" + (f": {', '.join(phrases)}" if phrases else "")


class MessageLength(DeterministicMetric):
    """Share of kept messages within `max_chars`. The target length is undecided (issue #65), so it only reports."""

    def __init__(self, threshold: float | None = None, max_chars: int = 600) -> None:
        super().__init__(threshold)
        self.max_chars = max_chars

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"Kept messages within {self.max_chars} characters"

    @classmethod
    def applies(cls, meta: dict) -> bool:
        return bool(_messages(meta))

    def compute(self, meta: dict) -> tuple[int, int, str]:
        lengths = [len(m) for m in _messages(meta)]
        return sum(n <= self.max_chars for n in lengths), len(lengths), f"median {statistics.median(lengths):.0f}, longest {max(lengths)}"


class NoCommentCapFindings(DeterministicMetric):
    name = "Kept findings that are not comment-cap findings"

    @classmethod
    def applies(cls, meta: dict) -> bool:
        return bool(_messages(meta))

    def compute(self, meta: dict) -> tuple[int, int, str]:
        messages = _messages(meta)
        hits = sum(bool(COMMENT_CAP.search(m)) for m in messages)
        return len(messages) - hits, len(messages), f"{hits} comment-cap findings"


class FindingsFileWritten(DeterministicMetric):
    name = "Reviewers that wrote a findings file"

    @classmethod
    def applies(cls, meta: dict) -> bool:
        return bool(meta.get("reviewer_output"))

    def compute(self, meta: dict) -> tuple[int, int, str]:
        status = meta["reviewer_output"]
        missing = sorted(name for name, wrote in status.items() if not wrote)
        return len(status) - len(missing), len(status), "no file from " + ", ".join(missing) if missing else "all wrote a file"


class AggregatorAccounting(DeterministicMetric):
    """Reviewer findings named by at least one kept or dropped item. Mirrors `_accounting` in commands/handoff.py.

    A reviewer with more findings than items naming it may have had several merged into one, so a shortfall is a
    prompt to look, not proof that something was lost; that is why this has no target."""

    name = "Reviewer findings named by a kept or dropped item"

    @classmethod
    def applies(cls, meta: dict) -> bool:
        has_findings = any(_counts(f) for f in (meta.get("reviewers") or {}).values())
        return has_findings and bool(meta.get("kept") or meta.get("dropped"))

    def compute(self, meta: dict) -> tuple[int, int, str]:
        raw = {name: _counts(findings) for name, findings in meta["reviewers"].items()}
        named: dict[str, int] = {}
        for item in [*(meta.get("kept") or []), *(meta.get("dropped") or [])]:
            for name in {n for n in (item.get("reviewers") or []) if isinstance(n, str)} if isinstance(item, dict) else ():
                named[name] = named.get(name, 0) + 1
        short = {m: raw[m] - named.get(m, 0) for m in raw if raw[m] > named.get(m, 0)}
        total = sum(raw.values())
        return total - sum(short.values()), total, f"possibly unaccounted {short}" if short else "all named"


# metric class -> its target (None: report only). A target of 1.0 means every run must be perfect.
DEFAULT_TARGETS: dict[type[DeterministicMetric], float | None] = {
    AnchoringShare: None,
    NoProcessVoice: 1.0,
    MessageLength: None,
    NoCommentCapFindings: 1.0,
    FindingsFileWritten: 1.0,
    AggregatorAccounting: None,
}


def default_metrics() -> list[DeterministicMetric]:
    return [cls(threshold) for cls, threshold in DEFAULT_TARGETS.items()]
