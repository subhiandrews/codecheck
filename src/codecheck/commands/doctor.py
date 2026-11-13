import sys

NAME = "doctor"
HELP = "Check prerequisites (python, git, gh, gh auth, claude)"
MIN_PY = (3, 14, 10)


def python_check(version_info=sys.version_info) -> dict:
    ok = tuple(version_info[:3]) >= MIN_PY
    have = ".".join(str(p) for p in version_info[:3])
    detail = f"python {have}" + ("" if ok else f" is older than {'.'.join(map(str, MIN_PY))}; upgrade for security fixes")
    return {"name": "python", "ok": ok, "required": False, "detail": detail}


def add_arguments(p) -> None:
    pass


def run(args, ctx) -> dict:
    checks = [python_check()]
    for name, argv, required in (
        ("git", ["git", "--version"], True),
        ("gh", ["gh", "--version"], True),
        ("gh auth", ["gh", "auth", "status"], True),
        ("claude", ["claude", "--version"], False),
    ):
        res = ctx.runner.run(argv, cwd=ctx.cwd)
        text = (res.stdout or res.stderr).strip()
        checks.append({"name": name, "ok": res.returncode == 0, "required": required,
                       "detail": text.splitlines()[0] if text else ""})
    return {"ok": all(c["ok"] or not c["required"] for c in checks), "checks": checks}
