"""Step 2: blind review sheets for the owner to label. No model calls, no tokens.

Two sheets, drawn at random (seeded) from the whole shadow store:
  drops  findings the aggregator dropped (not `duplicate`, which is a merge): "right drop or wrong drop?"
  kept   findings the aggregator kept, as they would be posted: "clear and worth posting, or sloppy?"

Each item shows the finding and the diff around the cited line. The aggregator's reason, the reviewers and the
message length stay behind a "Reveal" until the owner has chosen. At most `--per-pr` items come from one PR so
one review cannot dominate. Sheets and an answer key are written outside the repo (finding text is in them).

Usage:
  python -B evals/deepeval/review_sheets.py [--drops 25] [--kept 20] [--seed N] [--out-dir DIR]
"""

from __future__ import annotations

import argparse
import html
import json
import random
from pathlib import Path

from anchoring import STORE

from codecheck.core.diffmap import parse_diff

OUT = Path("~/.local/share/code-check-evals").expanduser()
CHOICES = {
    "drop": [("right", "right drop"), ("wrong", "wrong drop (should have been kept)"), ("skip", "not sure")],
    "kept": [("worth", "clear and worth posting"), ("unclear", "worth raising, but unclear or too long"),
             ("notworth", "not worth posting"), ("skip", "not sure")],
}
QUESTION = {
    "drop": "Was dropping this finding the right call? Judge the finding on its merits, as a maintainer would.",
    "kept": "Would you post this comment on a teammate's PR as written?",
}


def _relative(path: str, root: str | None) -> str:
    return path[len(root.rstrip("/")) + 1:] if root and path.startswith(root.rstrip("/") + "/") else path


GITLAB_REPOS = {"gitlab-org/cli"}  # the store records no host; every other repo in it is on GitHub


def pr_url(repo: str | None, pr: str) -> str | None:
    """The web URL of a PR or merge request, or None for a local run (its `pr` is a run id with no `#number`)."""
    number = pr.rpartition("#")[2]
    if not repo or "#" not in pr or not number.isdigit():
        return None
    if repo in GITLAB_REPOS:
        return f"https://gitlab.com/{repo}/-/merge_requests/{number}"
    return f"https://github.com/{repo}/pull/{number}"


def _run_meta(path: Path) -> dict:
    for line in path.read_text().splitlines() if path.is_file() else []:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("type") == "run":
            return row
    return {}


def full_text_for_drop(drop: dict, reviewers: dict, root: str | None) -> str:
    """The reviewer's own text for a dropped finding (the aggregator keeps only a short summary of it)."""
    path = _relative(drop.get("file") or "", root)
    best, best_gap = None, None
    for name in drop.get("reviewers") or []:
        for f in reviewers.get(name) or []:
            if not isinstance(f, dict) or _relative(f.get("file") or "", root) != path or not f.get("message"):
                continue
            gap = abs((f.get("line") or 0) - (drop.get("line") or 0))
            if best_gap is None or gap < best_gap:
                best, best_gap = f["message"], gap
    return best or drop.get("message") or ""


def load_items(store: Path = STORE) -> list[dict]:
    """Every kept finding and every non-duplicate drop in the store, one dict each."""
    items = []
    for raw_path in sorted(store.glob("*.raw.json")):
        run_id = raw_path.name.removesuffix(".raw.json")
        diff_path = store / f"{run_id}.diff"
        raw = json.loads(raw_path.read_text())
        aggregator = raw.get("aggregator") or {}
        if not diff_path.is_file() or not isinstance(raw.get("reviewers"), dict):
            continue
        meta = _run_meta(store / f"{run_id}.jsonl")
        base = {"run_id": run_id, "pr": meta.get("pr") or run_id, "repo": meta.get("repo"),
                "head_sha": meta.get("head_sha"), "tool_version": meta.get("tool_version"), "diff": diff_path.read_text()}
        root = raw.get("root")
        for i, f in enumerate(aggregator.get("findings") or []):
            items.append({**base, "id": f"{run_id}:k{i}", "kind": "kept", "file": _relative(f.get("file") or "", root),
                          "line": f.get("line"), "message": f.get("message") or "", "severity": f.get("severity"),
                          "category": f.get("category"), "suggestion": f.get("suggestion"),
                          "reviewers": f.get("reviewers") or [], "reason": None})
        for i, d in enumerate(aggregator.get("dropped") or []):
            if d.get("reason") == "duplicate":
                continue
            items.append({**base, "id": f"{run_id}:d{i}", "kind": "drop", "file": _relative(d.get("file") or "", root),
                          "line": d.get("line"), "message": full_text_for_drop(d, raw["reviewers"], root),
                          "severity": None, "category": None, "suggestion": None,
                          "reviewers": d.get("reviewers") or [], "reason": d.get("reason")})
    return items


def sample(items: list[dict], kind: str, n: int, seed: int, per_pr: int = 2, *, reason: str | None = None,
           exclude: frozenset[str] | set[str] = frozenset()) -> list[dict]:
    """A seeded random draw of `n` items of one kind, at most `per_pr` per PR and no repeated text within a PR.

    `reason` keeps only drops with that aggregator reason; `exclude` skips ids the owner has already labelled."""
    pool = sorted((i for i in items if i["kind"] == kind and (reason is None or i["reason"] == reason)
                   and i["id"] not in exclude), key=lambda i: i["id"])
    random.Random(seed).shuffle(pool)  # noqa: S311  (a reproducible sample, not security)
    chosen, per, seen = [], {}, set()
    for item in pool:
        if len(chosen) >= n:  # checked before adding, so a count of 0 or less gives an empty sheet
            break
        key = (item["pr"], item["file"], item["message"][:80])
        if per.get(item["pr"], 0) >= per_pr or key in seen:
            continue
        per[item["pr"]] = per.get(item["pr"], 0) + 1
        seen.add(key)
        chosen.append(item)
    return chosen


def source_summary(chosen: list[dict]) -> str:
    """How many distinct PRs or local runs a sheet draws from, and how many of those are local runs (no `#number`)."""
    sources = {i["pr"] for i in chosen}
    return f"from {len(sources)} PRs or runs, {sum('#' not in str(s) for s in sources)} of them local runs"


def diff_window(diff_text: str, path: str, line: int, radius: int = 12) -> list[dict]:
    """The diff lines around a cited right-side line (deletions included); [] when the line is not in the diff."""
    entry = parse_diff(diff_text).get(path)
    if entry is None or entry.binary or not isinstance(line, int):
        return []
    lines = entry.lines
    at = next((i for i, ln in enumerate(lines) if ln.new == line), None)
    if at is None:
        return []
    return [{"kind": ln.kind, "new": ln.new, "old": ln.old, "text": ln.text, "cited": i == at}
            for i, ln in enumerate(lines) if abs(i - at) <= radius]


_CSS = """:root{--bg:#fff;--fg:#1f2328;--mut:#656d76;--card:#f6f8fa;--bd:#d0d7de;--h:#0969da;--ok:#1a7f37;--add:#dafbe1;--del:#ffebe9;--cite:#fff8c5}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--card:#161b22;--bd:#30363d;--h:#58a6ff;--ok:#3fb950;--add:#12261e;--del:#2d1214;--cite:#3a2f00}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:16px}
h1{margin-bottom:4px}h2{margin:0}h3{margin:0 0 4px;font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.hint,.meta,.where{color:var(--mut);font-size:13px}.where{font-family:ui-monospace,monospace;margin:2px 0 8px;overflow-wrap:anywhere}
.item{border-top:1px solid var(--bd);padding:18px 0}.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:8px}
@media(max-width:860px){.cols{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--bd);border-radius:6px;padding:10px 14px;min-width:0}.card p{margin:6px 0;overflow-wrap:anywhere}
.finding{border-left:4px solid #8250df}pre{margin:0;overflow-x:auto;font:12px/1.45 ui-monospace,monospace}
pre.code div{white-space:pre;padding:0 4px}.add{background:var(--add)}.del{background:var(--del)}.cite{background:var(--cite);font-weight:600}
.choices{display:flex;gap:18px;flex-wrap:wrap;margin:12px 0 4px}.choices label{cursor:pointer}
input[type=text]{width:100%;box-sizing:border-box;background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:6px;padding:6px 8px;font:13px system-ui}
details{margin-top:8px;color:var(--mut)}summary{cursor:pointer}
textarea{width:100%;height:140px;background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:6px;padding:8px;font:13px ui-monospace,monospace}
button{background:var(--h);color:#fff;border:0;border-radius:6px;padding:8px 14px;font-size:14px;cursor:pointer}.done{color:var(--ok)}"""

_JS = """const ids=[...document.querySelectorAll('.item[data-id]')].map(s=>s.dataset.id);
const KEY='%KEY%';
function load(){try{return JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){return {}}}
function save(v){try{localStorage.setItem(KEY,JSON.stringify(v))}catch(e){}}
function render(){const v=load();
 document.getElementById('out').value=ids.filter(id=>v[id]&&v[id].c).map(id=>id+' '+v[id].c+(v[id].n?' | '+v[id].n:'')).join('\\n');
 document.getElementById('count').textContent=ids.filter(id=>v[id]&&v[id].c).length+' of '+ids.length+' answered';}
document.querySelectorAll('input[type=radio]').forEach(r=>{const v=load();if(v[r.name]&&v[r.name].c===r.value)r.checked=true;
 r.addEventListener('change',()=>{const v=load();v[r.name]=Object.assign(v[r.name]||{},{c:r.value});save(v);render();});});
document.querySelectorAll('input[type=text]').forEach(t=>{const v=load();if(v[t.name]&&v[t.name].n)t.value=v[t.name].n;
 t.addEventListener('input',()=>{const v=load();v[t.name]=Object.assign(v[t.name]||{},{n:t.value});save(v);render();});});
document.getElementById('copy').addEventListener('click',async()=>{const t=document.getElementById('out').value;
 try{await navigator.clipboard.writeText(t);document.getElementById('msg').textContent='Copied';}catch(e){document.getElementById('out').select();document.getElementById('msg').textContent='Press Cmd+C to copy';}});
render();"""


def _code_card(item: dict) -> str:
    rows = diff_window(item["diff"], item["file"], item["line"])
    if not rows:
        return ('<div class="card"><h3>Code</h3><p class="hint">The cited line is not in the diff, so this sheet has no code '
                "for it. Open the PR at the head commit if you need it.</p></div>")
    body = "".join(
        '<div class="{}">{} {}</div>'.format(
            ("cite " if r["cited"] else "") + {"add": "add", "del": "del"}.get(r["kind"], ""),
            {"add": "+", "del": "-"}.get(r["kind"], " "), html.escape(r["text"]))
        for r in rows)
    return f'<div class="card"><h3>Diff around the cited line (highlighted)</h3><pre class="code">{body}</pre></div>'


def _item_html(n: int, total: int, item: dict) -> str:
    kind, iid = item["kind"], html.escape(item["id"], quote=True)
    where = f'{html.escape(item["file"])}:{item["line"]}'
    tags = f' &middot; {html.escape(item["severity"] or "")} / {html.escape(item["category"] or "")}' if kind == "kept" else ""
    suggestion = f'<h3>Suggested change</h3><pre>{html.escape(item["suggestion"])}</pre>' if item.get("suggestion") else ""
    choices = "".join(f'<label><input type="radio" name="{iid}" value="{v}"> {html.escape(label)}</label>'
                      for v, label in CHOICES[kind])
    if kind == "drop":
        reveal = f'<p><b>Aggregator reason:</b> {html.escape(item["reason"] or "")}. <b>Raised by:</b> {html.escape(", ".join(item["reviewers"]))}.</p>'
    else:
        reveal = (f'<p><b>Raised by:</b> {html.escape(", ".join(item["reviewers"]))}. '
                  f'<b>Length:</b> {len(item["message"])} characters.</p>')
    heading = "Finding as posted" if kind == "kept" else "Dropped finding (reviewer's text)"
    url = pr_url(item["repo"], str(item["pr"]))
    pr_label = html.escape(str(item["pr"]))
    pr_html = f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">{pr_label}</a>' if url else f"{pr_label} (local run)"
    return (f'<section class="item" data-id="{iid}"><h2>Item {n} of {total}</h2>'
            f'<p class="meta">{pr_html} &middot; head {html.escape((item["head_sha"] or "")[:10])}</p>'
            f'<div class="cols"><div class="card finding"><h3>{html.escape(heading)}</h3>'
            f'<p class="where">{where}{tags}</p><p>{html.escape(item["message"])}</p>{suggestion}</div>{_code_card(item)}</div>'
            f'<div class="choices" role="radiogroup" aria-label="Item {n}">{choices}</div>'
            f'<input type="text" name="{iid}" placeholder="Optional note: why?">'
            f'<details><summary>Reveal</summary>{reveal}</details></section>')


def build_html(items: list[dict], kind: str, title: str, key: str | None = None) -> str:
    """The sheet; `key` is its browser-storage key (default `review-sheet-<kind>`), so two sheets never share answers."""
    chosen = [i for i in items if i["kind"] == kind]
    sections = "".join(_item_html(n, len(chosen), i) for n, i in enumerate(chosen, 1))
    return (f'<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{_CSS}</style>'
            f'<h1>{html.escape(title)}</h1><p class="hint">{html.escape(QUESTION[kind])} Choose first, then open Reveal if you '
            f'want the context. Your choices stay in this browser.</p>{sections}'
            '<section class="item"><h2>Your answers</h2><p class="hint" id="count"></p>'
            '<textarea id="out" readonly placeholder="Your answers appear here: item id, choice, optional note"></textarea>'
            '<p><button id="copy">Copy</button> <span id="msg" class="done"></span></p></section>'
            f'<script>{_JS.replace("%KEY%", key or f"review-sheet-{kind}")}</script>')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", type=Path, default=STORE)
    ap.add_argument("--out-dir", type=Path, default=OUT)
    ap.add_argument("--drops", type=int, default=25)
    ap.add_argument("--kept", type=int, default=20)
    ap.add_argument("--per-pr", type=int, default=2)
    ap.add_argument("--seed", type=int, default=20261120)
    ap.add_argument("--reason", help="instead of the two standard sheets, one sheet of every drop with this aggregator "
                    "reason (for example not-verified) that the owner has not labelled yet")
    args = ap.parse_args()

    items = load_items(args.store)
    if args.reason:
        labelled = set()
        for name in ("review-sheet-drops-labels.json", f"review-sheet-{args.reason}-labels.json"):
            path = args.out_dir / name
            labelled |= set(json.loads(path.read_text())["labels"]) if path.is_file() else set()
        chosen = sample(items, "drop", len(items), args.seed, per_pr=len(items), reason=args.reason, exclude=labelled)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"review-sheet-{args.reason}"
        (args.out_dir / f"{stem}.html").write_text(
            build_html(chosen, "drop", f"Right drop or wrong drop? Every unlabelled {args.reason} drop", key=stem))
        key = [{k: v for k, v in i.items() if k not in ("diff", "message", "suggestion")} for i in chosen]
        (args.out_dir / f"{stem}-key.json").write_text(json.dumps({"seed": args.seed, "reason": args.reason, "items": key}, indent=1))
        every = sum(i["kind"] == "drop" and i["reason"] == args.reason for i in items)
        print(f"{every} {args.reason} drops in the store, {len(labelled)} ids already labelled, {len(chosen)} on the new sheet")
        print(source_summary(chosen))
        return 0
    drops = sample(items, "drop", args.drops, args.seed, args.per_pr)
    kept = sample(items, "kept", args.kept, args.seed, args.per_pr)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "review-sheet-drops.html").write_text(build_html(drops, "drop", "Right drop or wrong drop?"))
    (args.out_dir / "review-sheet-kept.html").write_text(build_html(kept, "kept", "Clear and worth posting, or sloppy?"))
    key = [{k: v for k, v in i.items() if k not in ("diff", "message", "suggestion")} for i in drops + kept]
    (args.out_dir / "review-sheet-key.json").write_text(json.dumps({"seed": args.seed, "per_pr": args.per_pr, "items": key}, indent=1))
    pool = {k: sum(i["kind"] == k for i in items) for k in ("drop", "kept")}
    print(f"pool: {pool['drop']} non-duplicate drops, {pool['kept']} kept, from {len({i['run_id'] for i in items})} runs")
    print(f"sampled: {len(drops)} drops from {len({i['pr'] for i in drops})} PRs, {len(kept)} kept from {len({i['pr'] for i in kept})} PRs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
