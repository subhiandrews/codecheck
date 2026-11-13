"""Test doubles: FakeRunner (scripted commands) and, later, FakeProvider."""

from dataclasses import dataclass
from pathlib import Path

from codecheck.runner import CommandResult


@dataclass
class Call:
    argv: tuple[str, ...]
    input: str | None
    cwd: Path | None


class FakeRunner:
    def __init__(self) -> None:
        self._exact: dict[tuple[str, ...], list[CommandResult]] = {}
        self._prefix: list[tuple[tuple[str, ...], list[CommandResult]]] = []
        self.calls: list[Call] = []

    def on(self, *argv: str, stdout: str = "", stderr: str = "", returncode: int = 0) -> FakeRunner:
        self._exact.setdefault(tuple(argv), []).append(CommandResult(returncode, stdout, stderr))
        return self

    def on_prefix(self, *prefix: str, stdout: str = "", stderr: str = "", returncode: int = 0) -> FakeRunner:
        self._prefix.append((tuple(prefix), [CommandResult(returncode, stdout, stderr)]))
        return self

    def run(self, args, *, input=None, cwd=None, timeout=None) -> CommandResult:
        argv = tuple(args)
        self.calls.append(Call(argv, input, cwd))
        queue = self._exact.get(argv)
        if queue is None:
            for prefix, q in self._prefix:
                if argv[: len(prefix)] == prefix:
                    queue = q
                    break
        if queue is None:
            raise AssertionError(f"unexpected command: {argv}")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def argvs(self) -> list[tuple[str, ...]]:
        return [c.argv for c in self.calls]
