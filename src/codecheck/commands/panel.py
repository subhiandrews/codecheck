"""Run the context plan through independent read-only Codex processes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from codecheck.agentgen import render_codex_prompt
from codecheck.commands import ingest
from codecheck.commands._common import contained, load_meta, read_json, write_json
from codecheck.config import model_for, reasoning_for, validate_codex
from codecheck.core.panel_output import capture_schema, validate_capture
from codecheck.core.state import FileLock
from codecheck.errors import CodeCheckError

NAME = "panel"
HELP = "Run the Codex review panel for an existing context"


def add_arguments(p) -> None:
    run_parser = p.add_subparsers(dest="panel_action", required=True).add_parser("run")
    run_parser.add_argument("--run", required=True)


@dataclass(frozen=True, slots=True)
class Job:
    member: str
    capture: Path
    argv: list[str]


def _job(cfg, member, index, files, root, run_dir, scratch) -> Job:
    try:
        prompt, role, language = render_codex_prompt(member, run_dir, files)
    except ValueError as e:
        raise CodeCheckError(str(e)) from e
    model = model_for(cfg, role, language, runtime="codex")
    effort = reasoning_for(cfg, role, language)
    label = "aggregator" if member == "aggregator" else "reviewer"
    name = member if member == "aggregator" else f"{member}-{index}"
    capture = scratch / f"{name}.json"
    # Keep the measured policy literal. There are no user- or repo-configurable CLI flags.
    argv = [
        "codex", "exec", "-s", "read-only", "-C", str(root), "--ephemeral",
        "--ignore-user-config", "--disable", "hooks", f"--model={model}", "-c",
        'approval_policy="never"', "-c", f'model_reasoning_effort="{effort}"',
        "--output-schema", str(scratch / f"{label}-schema.json"),
        "--output-last-message", str(capture), "--json", "--", prompt,
    ]
    return Job(member, capture, argv)


def _execute(job, ctx, root, usable_reviewers):
    try:
        result = ctx.runner.run(job.argv, input="", timeout=ctx.config.codex.timeout_seconds)
        if result.returncode == 124:
            return None, "Codex timed out"
        if result.returncode:
            return None, f"Codex exit status {result.returncode}"
        raw = read_json(job.capture)
        data = validate_capture(raw, capture_schema(job.member == "aggregator"), job.member, str(root), usable_reviewers)
        return data, None
    except (RecursionError, ValueError):
        return None, "capture was not valid JSON"
    except OSError:
        return None, "could not start Codex or read its capture"
    except CodeCheckError as e:
        return None, str(e)


def _finding_path(run_dir, name):
    rel = f"findings/{name}.json"
    path = contained(run_dir, rel)
    if path != run_dir / rel:
        raise CodeCheckError("findings path is outside the run or uses a symlink")
    return path


def run(args, ctx) -> dict:
    validate_codex(ctx.config)
    run_dir = ctx.run_dir(args.run).resolve()
    meta = load_meta(ctx, args.run)
    root = Path(meta["root"])
    if not root.is_absolute() or not root.is_dir():
        raise CodeCheckError("context checkout must be an existing absolute directory")
    root = root.resolve()
    if run_dir.is_relative_to(root):
        raise CodeCheckError("the run directory must be outside the checkout")
    lock = FileLock(run_dir / "codex-panel.lock")
    if not lock.acquire():
        raise CodeCheckError("a Codex panel is already running for this review")
    try:
        with lock.keep_alive(), TemporaryDirectory(prefix="codex-", dir=run_dir) as temp:
            return _run_panel(args, ctx, meta, root, run_dir, Path(temp))
    finally:
        lock.release()


def _run_panel(args, ctx, meta, root, run_dir, scratch) -> dict:
    panel = meta["panel"]
    members = panel["members"]
    if not members or len(set(members)) != len(members) or "aggregator" in members:
        raise CodeCheckError("invalid context panel members")
    files = [file["path"] for file in meta["files"]]
    jobs = []
    for index, chunk in enumerate(meta.get("chunks") or [files], 1):
        for member in members:
            owned = panel["languages"].get(member.removeprefix("lang-"), []) if member.startswith("lang-") else chunk
            scope = [path for path in chunk if path in owned]
            if scope:
                jobs.append(_job(ctx.config, member, index, scope, root, run_dir, scratch))
    aggregator = _job(ctx.config, "aggregator", 1, files, root, run_dir, scratch)
    outputs = {name: _finding_path(run_dir, name) for name in [*members, "aggregator"]}
    for name, is_aggregator in (("reviewer", False), ("aggregator", True)):
        write_json(scratch / f"{name}-schema.json", capture_schema(is_aggregator))

    # Invalidate the previous attempt before dispatch so a failed retry cannot reuse findings.
    for path in reversed(outputs.values()):
        path.unlink(missing_ok=True)
    (run_dir / "ingest.json").unlink(missing_ok=True)

    combined = {name: [] for name in members}
    scheduled = {job.member for job in jobs}
    failed = {name: "no owned files in the context plan" for name in members if name not in scheduled}
    with ThreadPoolExecutor(max_workers=ctx.config.codex.max_parallel) as pool:
        results = pool.map(lambda job: _execute(job, ctx, root, set()), jobs)
        for job, (data, error) in zip(jobs, results, strict=True):
            if error:
                failed.setdefault(job.member, error)
            else:
                combined[job.member].extend(data)

    written = []
    for name, findings in combined.items():
        if name not in failed:
            _publish(scratch, outputs[name], findings, written)
    summary = ingest.run(args, ctx)
    result, error = _execute(aggregator, ctx, root, set(summary["valid"]))
    if error:
        failed["aggregator"] = error
    else:
        _publish(scratch, outputs["aggregator"], result, written)
    return {"members": [*members, "aggregator"], "failed_members": failed, "files_written": written}


def _publish(scratch, target, data, written):
    stage = scratch / "publish.json"
    write_json(stage, data)
    stage.replace(target)
    written.append(f"findings/{target.name}")
