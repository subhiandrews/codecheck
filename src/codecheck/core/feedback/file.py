"""File the issue with `gh`: dedupe by fingerprint first, never raise on a gh problem, fall back to a prefilled URL."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from codecheck.core.feedback.draft import MAX_TITLE
from codecheck.core.feedback.manifest import Manifest
from codecheck.errors import CodeCheckError
from codecheck.runner import CommandRunner
from codecheck.shadow.scrub import scrub_text

_FP = re.compile(r"<!-- feedback-fingerprint: ([0-9a-f]{12}) -->")
GH_TIMEOUT = 60  # seconds: a stalled gh must not hang the session
URL_MAX = 7000  # GitHub refuses very long URLs; the full text stays in the draft file


@dataclass(frozen=True, slots=True)
class FileResult:
    outcome: str  # "created" | "duplicate" | "fallback"
    url: str


class Filer:
    def __init__(self, runner: CommandRunner, manifest: Manifest, cwd: Path | None = None) -> None:
        self.runner, self.m, self.cwd = runner, manifest, cwd

    def _check_title(self, title: str) -> None:
        if not title or title.startswith("-") or "\n" in title or "\r" in title or len(title) > MAX_TITLE:
            raise CodeCheckError(f"the issue title is not valid (empty, option-like, multi-line or over {MAX_TITLE} characters)")

    def _fallback(self, title: str, body: str) -> FileResult:
        head = f"https://github.com/{self.m.target_repo}/issues/new?title={quote(title, safe='')}&body="
        text = body
        while len(head) + len(quote(text, safe="")) > URL_MAX and text:  # percent-encoding can multiply the length several times
            text = text[: int(len(text) * 0.8)]
        return FileResult("fallback", head + quote(text, safe=""))

    def _duplicate(self, fingerprint: str) -> list[dict] | None:
        res = self.runner.run(
            ["gh", "issue", "list", "--repo", self.m.target_repo, "--state", "open", "--search", f"{fingerprint} in:body",
             "--json", "number,url", "--limit", "5"], cwd=self.cwd, timeout=GH_TIMEOUT)
        if res.returncode != 0:
            return None
        try:
            found = json.loads(res.stdout or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(found, list) or not all(isinstance(i, dict) and type(i.get("number")) is int and isinstance(i.get("url"), str) for i in found):
            return None
        return found

    def _prepare(self, title: str, body: str) -> tuple[str, str]:
        """Everything that leaves the machine is scrubbed and capped here, whatever the user typed into the draft."""
        title, body = scrub_text(title), scrub_text(body)
        limit = self.m.max_body_chars
        if len(body) > limit:
            m = _FP.search(body)
            marker = m.group(0) if m else ""
            head = body.replace(marker, "").rstrip()[: max(limit - len(marker) - 1, 0)] if marker else body[:limit]
            body = head + ("\n" + marker if marker else "")
        return title, body

    def file(self, title: str, body: str) -> FileResult:
        self._check_title(title)
        title, body = self._prepare(title, body)
        match = _FP.search(body)
        if match:
            found = self._duplicate(match.group(1))
            if found is None:  # gh missing, unauthenticated, or the search failed
                return self._fallback(title, body)
            if found:
                number, url = found[0]["number"], found[0]["url"]
                note = f"Seen again with {self.m.product} {self.m.version}; the reporter agreed to file it."
                res = self.runner.run(["gh", "issue", "comment", str(number), "--repo", self.m.target_repo, "--body-file", "-"],
                                      input=note, cwd=self.cwd, timeout=GH_TIMEOUT)
                return FileResult("duplicate", url) if res.returncode == 0 else self._fallback(title, body)
        res = self.runner.run(["gh", "issue", "create", "--repo", self.m.target_repo, f"--title={title}", "--body-file", "-"],
                              input=body, cwd=self.cwd, timeout=GH_TIMEOUT)
        if res.returncode != 0:
            return self._fallback(title, body)
        return FileResult("created", res.stdout.strip().splitlines()[-1] if res.stdout.strip() else "")
