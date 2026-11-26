"""The scratch draft: a scrubbed, size-capped markdown file the user reads (and may edit) before anything is filed."""

import os
from collections.abc import Mapping
from pathlib import Path

from codecheck.core.feedback.manifest import Manifest
from codecheck.core.state import ensure_private_dir
from codecheck.errors import CodeCheckError
from codecheck.shadow.scrub import scrub_text

MARKER = "<!-- feedback-fingerprint: {} -->"
MAX_TITLE = 120


def render(m: Manifest, kind: str, source: str, fingerprint: str, detail: Mapping, *, judged: bool) -> tuple[str, str]:
    title = scrub_text(f"[{m.product}] {m.title_for(kind)}")[:MAX_TITLE]
    marker = MARKER.format(fingerprint)
    lines = [
        f"**{m.product} {m.version}** reported this automatically after you agreed to file it.", "",
        f"- Kind: `{kind}`", f"- Source: `{source}`",
    ]
    if judged:
        lines.append("- Noticed by the model, not by a detector")
    for key in sorted(detail):
        lines.append(f"- {key}: {detail[key]}")
    lines += ["", "What I expected, and what happened instead (edit this before filing):", ""]
    head = scrub_text("\n".join(lines))
    room = max(m.max_body_chars - len(marker) - 1, 0)
    return title, head[:room].rstrip() + "\n" + marker


def write_draft(directory: Path, fingerprint: str, title: str, body: str) -> Path:
    ensure_private_dir(Path(directory))
    path = Path(directory) / f"{fingerprint}.md"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)  # never through a planted symlink
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"# {title}\n\n{body}\n")
    os.chmod(path, 0o600)
    return path


def read_draft(path: Path) -> tuple[str, str]:
    """(title, body) as the file stands now, so an edit the user made is what gets filed."""
    if Path(path).is_symlink():
        raise CodeCheckError(f"{path} is a symlink; refusing to read it")
    text = Path(path).read_text(encoding="utf-8", errors="replace")  # a stray byte in an edited draft must not break `pending`
    first, _, rest = text.partition("\n")
    return first.removeprefix("# ").strip(), rest.strip("\n")


def clear_symlink(path: Path) -> None:
    """A symlink in the private drafts folder is not ours: remove the link (never its target) so a fresh draft can be written."""
    if Path(path).is_symlink():
        Path(path).unlink()
