"""Push local memory to a server and pull a repository's evidence back into the local store.

Push goes through the sharing levels (`memory/levels.py`): nothing is sent unless `memory.share_repos` names the repository.
The server is the only place counts are computed for the team; here a pull merges teammates' evidence into the local
store and the local `recompute` derives lessons, so every machine agrees on the arithmetic.

`sync.json` next to the store is the outbox. For every lesson (and run) it keeps the digest of what the server last
accepted, so a transient failure (unreachable, 5xx, 429) is simply retried by the next sync, a payload the server
permanently refused (4xx other than 429) is remembered and not resent until it changes, and an unchanged one is not
resent at all. Evidence that came from a pull is never pushed back under this machine's name.
"""

import json
import time
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from codecheck.config import expand_path
from codecheck.errors import CodeCheckError
from codecheck.memory import levels
from codecheck.memory.client import (
    HttpTransport,
    LocalTransport,
    MemoryTransportError,
    read_token_file,
)
from codecheck.memory.consolidate import Params
from codecheck.memory.db import MemoryDB
from codecheck.memory.server import ServerDB, Service
from codecheck.shadow.scrub import scrub_text
from codecheck.shadow.store import ShadowStore, write_private

STATE_FILE = "sync.json"
RETRY_AFTER_FAILED_PULL = 60.0
MAX_PAGES = 20
BATCH_BYTES = 200 * 1024
clock = time.time  # tests replace this


# ---------- state ----------

def _store_dir(ctx) -> Path:
    return expand_path(ctx.config.memory.store)


def load_state(ctx) -> dict:
    p = _store_dir(ctx) / STATE_FILE
    try:
        if p.is_symlink() or not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(ctx, state: dict) -> None:
    d = _store_dir(ctx)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(d / STATE_FILE, json.dumps(state, sort_keys=True))


def default_solo_db(ctx) -> Path:
    return _store_dir(ctx).parent / "memory-server"


def build_transport(ctx, *, solo_db: str = "", timeout: float | None = None):
    """HTTP when `memory.url` is set; otherwise (solo) the local file through the same `Service` the server runs."""
    m = ctx.config.memory
    if m.url and not solo_db:
        return HttpTransport(m.url, read_token_file(m.token_file), timeout if timeout is not None else float(max(m.timeout_seconds, 10)))
    path = expand_path(solo_db) if solo_db else default_solo_db(ctx)
    return LocalTransport(Service(ServerDB(path, now=lambda: clock()).init(), params=params_of(ctx), now=lambda: clock()))


def params_of(ctx) -> Params:
    m = ctx.config.memory
    return Params(half_days=float(m.half_life_days), expire_days=float(m.expire_days))


def target_id(ctx, solo_db: str = "") -> str:
    m = ctx.config.memory
    return levels.digest(["http", m.url] if m.url and not solo_db else ["local", str(expand_path(solo_db) if solo_db else default_solo_db(ctx))])


def _fresh_state(ctx, solo_db: str = "") -> dict:
    state = load_state(ctx)
    if state.get("target") != target_id(ctx, solo_db):
        state = {"target": target_id(ctx, solo_db)}  # another server: what the old one accepted says nothing about this one
    return state


def repo_private(ctx, repo: str) -> bool | None:
    try:
        v = ctx.provider.repo_is_private(repo)
    except Exception:  # noqa: BLE001 - any failure means unknown, and unknown is treated as private
        return None
    return v if isinstance(v, bool) else None


# ---------- push ----------

def _batches(items: list[tuple[str, dict]], size: int):
    batch, nbytes = [], 0
    for key, item in items:
        n = len(json.dumps(item))
        if batch and (len(batch) >= size or nbytes + n > BATCH_BYTES):
            yield batch
            batch, nbytes = [], 0
        batch.append((key, item))
        nbytes += n
    if batch:
        yield batch


class _Stop(Exception):
    """Stop pushing: the failure is not about any one item (auth, rate limit, server down)."""


def _send(state_map: dict, rejected_map: dict, batch, call, server_max: str, *, id_key: str) -> tuple[int, int]:
    """Send one batch; record what the server accepted or permanently refused. Returns (accepted, refused)."""
    resp = call([item for _, item in batch])
    if resp.status == 200:
        by_key = {r.get(id_key): r for r in resp.body.get("results", []) if isinstance(r, dict)}
        ok = bad = 0
        for key, item in batch:
            res = by_key.get(key)
            dig = levels.digest(item)
            if res and res.get("status") == "ok":
                state_map[key] = dig
                rejected_map.pop(key, None)
                ok += 1
            elif res and res.get("status") == "rejected":
                rejected_map[key] = {"digest": dig, "max_level": server_max, "error": scrub_text(str(res.get("error")))[:200]}
                bad += 1
            # no answer for an item: leave it, the next sync resends
        return ok, bad
    if resp.status in (401, 403, 408, 429) or resp.status >= 500:
        raise _Stop(f"HTTP {resp.status}: " + scrub_text(str(resp.body.get("error", "")))[:200])
    for key, item in batch:  # permanent: this payload will not be accepted as it is
        rejected_map[key] = {"digest": levels.digest(item), "max_level": server_max,
                             "error": f"HTTP {resp.status}: " + scrub_text(str(resp.body.get("error", "")))[:200]}
    return 0, len(batch)


def _eligible_evidence(ctx, db: MemoryDB, repo: str, pulled: set[str]):
    groups = defaultdict(list)
    for e in db.evidence(repo=repo):
        if e.id not in pulled:
            groups[(e.lesson_id, "miss" if e.signal == "human_miss" else "finding")].append(e)
    return groups


def push(ctx, transport, state: dict, *, now: float, solo_db: str = "") -> dict:
    m = ctx.config.memory
    if not m.share_repos:
        return {"pushed": False, "reason": "memory.share_repos is empty: nothing is pushed", "repos": []}
    meta = transport.meta()
    if meta.status != 200 or meta.body.get("max_level") not in levels.LEVELS:
        raise MemoryTransportError(f"the memory server's /v1/meta answered HTTP {meta.status} without a usable maximum level")
    server_max = meta.body["max_level"]
    db = MemoryDB(_store_dir(ctx), now=lambda: now)
    shadow = ctx.shadow or ShadowStore(expand_path(ctx.config.shadow.store))
    pulled = set(state.get("pulled_ids", []))
    out, error = [], None
    vec_pin, vec_note = _vector_pin(ctx, db, state, meta.body)
    for repo in m.share_repos:
        private = repo_private(ctx, repo)
        level = levels.effective_level(m.share_level, server_max, m.private_level, private)
        rep = {"repo": repo, "private": private, "level": level,
               "lessons": {"sent": 0, "unchanged": 0, "refused": 0, "skipped_refused": 0},
               "episodes": {"sent": 0, "unchanged": 0, "refused": 0, "skipped_refused": 0}}
        out.append(rep)
        if error:
            continue
        pushed = state.setdefault("pushed", {}).setdefault(repo, {})
        rejected = state.setdefault("rejected", {}).setdefault(repo, {})
        try:
            todo = []
            for (lid, kind), evs in sorted(_eligible_evidence(ctx, db, repo, pulled).items()):
                item = levels.lesson_item(lid, kind, evs, level)
                if vec_pin is not None:
                    from codecheck.memory import semantic

                    semantic.attach_vectors(ctx, db, item, evs, level, vec_pin)
                dig = levels.digest(item)
                refused = rejected.get(lid)
                if pushed.get(lid) == dig:
                    rep["lessons"]["unchanged"] += 1
                elif refused and refused.get("digest") == dig and refused.get("max_level") == server_max:
                    rep["lessons"]["skipped_refused"] += 1
                else:
                    todo.append((lid, item))
            for batch in _batches(todo, levels.MAX_BATCH_ITEMS):
                ok, bad = _send(pushed, rejected, batch, transport.put_lessons, server_max, id_key="lesson_id")
                rep["lessons"]["sent"] += ok
                rep["lessons"]["refused"] += bad
            if level == "episodes":
                _push_episodes(shadow, transport, state, repo, server_max, rep["episodes"])
        except _Stop as e:
            error = str(e)
        save_state(ctx, state)
    return {"pushed": error is None, "repos": out, **({"error": error} if error else {}),
            **({"vectors": vec_note} if m.embed_model else {})}


def _vector_pin(ctx, db: MemoryDB, state: dict, meta_body: dict):
    """The pin to send vectors under (None sends none) and why. Also remembers the server's pin for retrieval."""
    m = ctx.config.memory
    if not m.embed_model:
        return None, ""
    from codecheck.memory import semantic

    state["server_embedding"] = meta_body.get("embedding")
    try:
        return semantic.push_pin(ctx, db, meta_body.get("embedding"))
    except Exception as e:  # noqa: BLE001 - vectors are optional; the push goes ahead without them
        return None, f"error: {type(e).__name__}"


def _push_episodes(shadow: ShadowStore, transport, state: dict, repo: str, server_max: str, rep: dict) -> None:
    pushed = state.setdefault("episodes_pushed", {}).setdefault(repo, {})
    rejected = state.setdefault("episodes_rejected", {}).setdefault(repo, {})
    todo = []
    for rid in shadow.ordered_run_ids():
        records = shadow.read(rid)
        run = next((r for r in records if r.get("type") == "run"), None)
        if not run or run.get("repo") != repo or not isinstance(run.get("pr"), str):
            continue
        ep = levels.episode_payload(rid, repo, run["pr"], records)
        dig = levels.digest(ep)
        if len(json.dumps(ep)) > levels.MAX_EPISODE_BYTES:
            rejected[rid] = {"digest": dig, "max_level": server_max, "error": "episode is too large to send"}
            rep["refused"] += 1
        elif pushed.get(rid) == dig:
            rep["unchanged"] += 1
        elif (r := rejected.get(rid)) and r.get("digest") == dig and r.get("max_level") == server_max:
            rep["skipped_refused"] += 1
        else:
            todo.append((rid, ep))
    for batch in _batches(todo, levels.MAX_EPISODES_PER_BATCH):
        ok, bad = _send(pushed, rejected, batch, transport.put_episodes, server_max, id_key="run_id")
        rep["sent"] += ok
        rep["refused"] += bad


# ---------- pull ----------

def pull(ctx, transport, repos: Sequence[str], state: dict, *, now: float, deadline: float | None = None) -> dict:
    db = MemoryDB(_store_dir(ctx), now=lambda: now)
    params = params_of(ctx)
    pulled = state.setdefault("pulled_ids", [])
    known_pulled = set(pulled)
    out, error = [], None
    since_map = state.setdefault("pull_since", {})
    for repo in repos:
        rep = {"repo": repo, "evidence": 0, "skipped": 0}
        out.append(rep)
        if error:
            continue
        since, cursor, newest = float(since_map.get(repo, 0.0)), "", 0.0
        items = []
        for _ in range(MAX_PAGES):
            if deadline is not None and clock() >= deadline:
                error = "timed out"
                break
            resp = transport.get_lessons(repo, since, cursor)
            if resp.status in (401, 403, 408, 429) or resp.status >= 500:
                error = f"HTTP {resp.status}"
                break
            if resp.status != 200:
                rep["error"] = f"HTTP {resp.status}"  # permanent for this repo; try the others
                break
            for raw in resp.body.get("evidence") or []:
                try:
                    ts = raw.pop("received_ts", 0.0) if isinstance(raw, dict) else 0.0
                    ev = levels.parse_evidence(raw, "episodes", now)
                except (levels.PayloadError, AttributeError):
                    rep["skipped"] += 1
                    continue
                if ev.scope.repo != repo:
                    rep["skipped"] += 1  # a server answering for another repository is not believed
                    continue
                newest = max(newest, ts if isinstance(ts, (int, float)) else 0.0)
                items.append(ev)
            cursor = resp.body.get("next_cursor") or ""
            if not cursor:
                break
        if items:
            have = {e.id for e in db.evidence(repo=repo)}
            counts = db.upsert_evidence([e for e in items if (now - e.ts) / 86400.0 < params.expire_days])
            for e in items:
                if e.id not in have and e.id not in known_pulled:
                    known_pulled.add(e.id)
                    pulled.append(e.id)
            rep["evidence"] = counts["new"] + counts["updated"]
            db.recompute(params, now)
        if newest:
            since_map[repo] = max(float(since_map.get(repo, 0.0)), newest - 60.0)  # overlap is safe: writes are idempotent
    return {"pulled": error is None, "repos": out, **({"error": error} if error else {})}


def pull_if_stale(ctx, max_age_seconds: float = 600.0, repos: Sequence[str] | None = None) -> dict:
    """Refresh the local replica from the server when it is older than `max_age_seconds`, within `memory.timeout_seconds`.

    Never raises and never blocks past the budget: on any failure the replica is left as it is. Only a configured
    `memory.url` is ever contacted."""
    try:
        m = ctx.config.memory
        if not m.url:
            return {"pulled": False, "reason": "no memory.url"}
        wanted = list(repos if repos is not None else m.share_repos)
        if not wanted:
            return {"pulled": False, "reason": "no repositories to pull"}
        now = clock()
        state = _fresh_state(ctx)
        if now - float(state.get("last_pull_ts", 0.0)) < max_age_seconds:
            return {"pulled": False, "reason": "replica is fresh"}
        if now - float(state.get("last_pull_failed_ts", 0.0)) < RETRY_AFTER_FAILED_PULL:
            return {"pulled": False, "reason": "a recent pull failed"}
        deadline = now + float(m.timeout_seconds)
        try:
            result = pull(ctx, HttpTransport(m.url, read_token_file(m.token_file), float(m.timeout_seconds)), wanted, state,
                          now=now, deadline=deadline)
        except CodeCheckError as e:
            result = {"pulled": False, "error": type(e).__name__}
        if result["pulled"]:
            state["last_pull_ts"] = now
        else:
            state["last_pull_failed_ts"] = now
        save_state(ctx, state)
        return result
    except Exception as e:  # noqa: BLE001 - reading memory must never break the caller
        return {"pulled": False, "error": type(e).__name__}


# ---------- sync, status ----------

def sync(ctx, *, do_push: bool = True, do_pull: bool = True, solo_db: str = "", extra_repos: Sequence[str] = ()) -> dict:
    m = ctx.config.memory
    now = clock()
    state = _fresh_state(ctx, solo_db)
    transport = build_transport(ctx, solo_db=solo_db)
    out: dict = {"mode": "server" if m.url and not solo_db else "solo", "ok": True}
    try:
        if do_push:
            out["push"] = push(ctx, transport, state, now=now, solo_db=solo_db)
            out["ok"] &= out["push"]["pushed"] or "reason" in out["push"]
            if "error" in out["push"]:
                state["last_error"] = out["push"]["error"]
        if do_pull:
            if m.embed_model and not do_push:
                state["server_embedding"] = transport.meta().body.get("embedding")  # retrieval compares models without a network call
            repos = list(dict.fromkeys([*m.share_repos, *extra_repos]))
            if repos:
                out["pull"] = pull(ctx, transport, repos, state, now=now)
                out["ok"] &= out["pull"]["pulled"]
                if out["pull"]["pulled"]:
                    state["last_pull_ts"] = now
            else:
                out["pull"] = {"pulled": False, "reason": "no repositories: set memory.share_repos or pass --repo"}
    except MemoryTransportError as e:
        out["ok"] = False
        out["error"] = str(e)
        state["last_error"] = str(e)
    except CodeCheckError as e:  # a local store problem, reported the same way
        out["ok"] = False
        out["error"] = str(e)
    state["last_sync_ts"] = now
    state["last_ok"] = bool(out["ok"])
    if out["ok"]:
        state.pop("last_error", None)
    save_state(ctx, state)
    return out


def status(ctx, *, offline: bool = False, solo_db: str = "") -> dict:
    """One summary. Never contains a token: only whether a token file is configured."""
    m = ctx.config.memory
    state = load_state(ctx)
    server_mode = bool(m.url) and not solo_db
    meta: dict | None = None
    server: dict = {"mode": "server" if server_mode else "solo", "url": m.url or None}
    if not offline and (server_mode or solo_db):
        try:
            resp = build_transport(ctx, solo_db=solo_db, timeout=float(m.timeout_seconds)).meta()
            if resp.status == 200:
                meta = resp.body
            server["error"] = None if resp.status == 200 else f"HTTP {resp.status}"
        except CodeCheckError as e:
            server["error"] = str(e)
    server["meta"] = meta
    server_max = (meta or {}).get("max_level")
    repos = []
    for repo in m.share_repos:
        private = None if offline else repo_private(ctx, repo)
        level = levels.effective_level(m.share_level, server_max or "episodes", m.private_level, private)
        repos.append({"repo": repo, "private": private, "level": level, "assumes_server_max": None if server_max else "episodes"})
    return {
        "config": {"url": m.url or None, "token_file_set": bool(m.token_file), "share_level": m.share_level,
                   "private_level": m.private_level, "share_repos": list(m.share_repos), "store": str(_store_dir(ctx)),
                   "pushes_anything": bool(m.share_repos)},
        "effective": repos, "server": server,
        "last_sync": {"at": state.get("last_sync_ts"), "ok": state.get("last_ok"), "pull_at": state.get("last_pull_ts"),
                      "error": state.get("last_error")},
        "store": MemoryDB(_store_dir(ctx)).status(),
    }
