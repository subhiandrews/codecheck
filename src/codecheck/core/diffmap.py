"""Parse unified diffs and anchor findings to lines a review comment can attach to."""

import re
from dataclasses import dataclass

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_QUOTED = re.compile(r'^("(?:[^"\\]|\\.)*"|\S+) ("(?:[^"\\]|\\.)*"|\S+)$')


@dataclass(frozen=True, slots=True)
class DiffLine:
    kind: str  # "add" | "del" | "ctx"
    old: int | None
    new: int | None
    text: str


@dataclass(frozen=True, slots=True)
class FileDiff:
    path: str
    old_path: str
    status: str  # added | deleted | modified | renamed
    binary: bool
    lines: tuple[DiffLine, ...]
    patch: str

    def right_lines(self) -> dict[int, str]:
        return {ln.new: ln.text for ln in self.lines if ln.new is not None}


@dataclass(frozen=True, slots=True)
class Anchor:
    path: str
    line: int
    side: str = "RIGHT"
    relocated: bool = False


def _unquote(p: str) -> str:
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        body, raw, i = p[1:-1], bytearray(), 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                if nxt in "01234567":
                    j, digits = i + 1, ""
                    while j < len(body) and len(digits) < 3 and body[j] in "01234567":
                        digits += body[j]
                        j += 1
                    raw.append(int(digits, 8))
                    i = j
                    continue
                raw.extend({"n": b"\n", "t": b"\t", '"': b'"', "\\": b"\\"}.get(nxt, nxt.encode()))
                i += 2
                continue
            raw.extend(ch.encode())
            i += 1
        return raw.decode("utf-8", "replace")
    return p


def _strip_prefix(p: str) -> str:
    p = _unquote(p.split("\t")[0])
    return p[2:] if p[:2] in ("a/", "b/") else p


def _header_paths(rest: str) -> tuple[str, str]:
    if '"' in rest:
        m = _QUOTED.match(rest)
        if m:
            return _strip_prefix(m.group(1)), _strip_prefix(m.group(2))
    n = (len(rest) - 5) // 2  # "a/P b/P"
    if n > 0 and rest[:2] == "a/" and rest[2 + n : 5 + n] == " b/" and rest[2 : 2 + n] == rest[5 + n :]:
        return rest[2 : 2 + n], rest[5 + n :]
    left, _, right = rest.partition(" b/")
    return left.removeprefix("a/"), right


def parse_diff(text: str) -> dict[str, FileDiff]:
    files: dict[str, FileDiff] = {}
    cur: dict | None = None
    rem_old = rem_new = old_n = new_n = 0

    def flush() -> None:
        if cur is None:
            return
        path = cur["old"] if cur["status"] == "deleted" else cur["new"]
        files[path] = FileDiff(
            path, cur["old"], cur["status"], cur["binary"], tuple(cur["lines"]), "\n".join(cur["raw"]) + "\n"
        )

    for raw in text.split("\n"):
        if cur is not None and (rem_old > 0 or rem_new > 0):
            cur["raw"].append(raw)
            c = raw[:1]
            if c == "+":
                cur["lines"].append(DiffLine("add", None, new_n, raw[1:]))
                new_n += 1
                rem_new -= 1
            elif c == "-":
                cur["lines"].append(DiffLine("del", old_n, None, raw[1:]))
                old_n += 1
                rem_old -= 1
            elif c == "\\":
                pass
            else:  # " " context, or a context line whose single space was stripped
                cur["lines"].append(DiffLine("ctx", old_n, new_n, raw[1:]))
                old_n += 1
                new_n += 1
                rem_old -= 1
                rem_new -= 1
            continue
        if raw.startswith("diff --git "):
            flush()
            old, new = _header_paths(raw[len("diff --git ") :])
            cur = {"old": old, "new": new, "status": "modified", "binary": False, "lines": [], "raw": [raw]}
            continue
        if cur is None:
            continue
        cur["raw"].append(raw)
        if raw.startswith("new file mode"):
            cur["status"] = "added"
        elif raw.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif raw.startswith("rename from "):
            cur["status"], cur["old"] = "renamed", _unquote(raw[len("rename from ") :])
        elif raw.startswith("rename to "):
            cur["new"] = _unquote(raw[len("rename to ") :])
        elif raw.startswith(("Binary files", "GIT binary patch")):
            cur["binary"] = True
        elif raw.startswith("--- "):
            p = _strip_prefix(raw[4:])
            if p != "/dev/null":
                cur["old"] = p
        elif raw.startswith("+++ "):
            p = _strip_prefix(raw[4:])
            if p != "/dev/null":
                cur["new"] = p
        elif m := _HUNK.match(raw):
            old_n, rem_old = int(m[1]), int(m[2]) if m[2] is not None else 1
            new_n, rem_new = int(m[3]), int(m[4]) if m[4] is not None else 1
    flush()
    return files


def anchor_finding(files: dict[str, FileDiff], path: str, line: int, *, max_distance: int = 3) -> Anchor | None:
    fd = files.get(path)
    if fd is None or fd.binary:
        return None
    right = fd.right_lines()
    if line in right:
        return Anchor(path, line)
    if not right:
        return None
    nearest = min(right, key=lambda n: (abs(n - line), n))
    if abs(nearest - line) <= max_distance:
        return Anchor(path, nearest, relocated=True)
    return None
