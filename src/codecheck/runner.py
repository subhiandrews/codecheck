"""Thin subprocess abstraction; every external command goes through a CommandRunner."""

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codecheck.errors import CodeCheckError


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandError(CodeCheckError):
    def __init__(self, argv: Sequence[str], result: CommandResult) -> None:
        self.argv = list(argv)
        self.result = result
        super().__init__(f"{' '.join(argv[:4])} failed ({result.returncode}): {result.stderr.strip()[:500]}")


class CommandRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        input: str | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> CommandResult: ...


class SubprocessRunner:
    def run(self, args, *, input=None, cwd=None, timeout=None) -> CommandResult:
        try:
            p = subprocess.run(
                list(args), input=input, cwd=cwd, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
        except FileNotFoundError as e:
            return CommandResult(127, "", str(e))
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", f"timed out after {timeout}s")
        return CommandResult(p.returncode, p.stdout, p.stderr)


def run_checked(runner: CommandRunner, args: Sequence[str], **kwargs) -> CommandResult:
    result = runner.run(args, **kwargs)
    if result.returncode != 0:
        raise CommandError(args, result)
    return result
