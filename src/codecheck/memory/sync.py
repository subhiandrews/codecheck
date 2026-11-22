"""Push local memory to a server and pull a repository's evidence back into the local store.

Push goes through the sharing levels (`memory/levels.py`): nothing is sent unless `memory.share_repos` names the repository.
The server is the only place counts are computed for the team; here a pull merges teammates' evidence into the local
store and the local `recompute` derives lessons, so every machine agrees on the arithmetic.

`sync.json` next to the store is the outbox. For every lesson (and run) it keeps the digest of what the server last
accepted, so a transient failure (unreachable, 5xx, 429) is simply retried by the next sync, a payload the server
permanently refused (4xx other than 429) is remembered and not resent until it changes, and an unchanged one is not
resent at all. Evidence that came from a pull is never pushed back under this machine's name.
"""

import contextlib
import json
import math
import os
import re
import threading
import time
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from codecheck.config import expand_path
from codecheck.core.state import FileLock
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
_REPO_NAME = levels._REPO
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


def save_state(ctx, state: dict, cancel: threading.Event | None = None) -> None:
    """Write `sync.json` whole or not at all (temp file, then rename), so a reader or a second writer never sees half of it.
    A sync whose caller gave up on it (`cancel` set) must not write at all: its view of the state is out of date."""
    if cancel is not None and cancel.is_set():
        return
    d = _store_dir(ctx)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = d / f"{STATE_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        write_private(tmp, json.dumps(state, sort_keys=True))
        os.replace(tmp, d / STATE_FILE)
    finally:
        tmp.unlink(missing_ok=True)


def default_solo_db(ctx) -> Path:
    return _store_dir(ctx).parent / "memory-server"


def build_transport(ctx, *, solo_db: str = "", timeout: float | None = None):
    """HTTP when `memory.url` is set; otherwise (solo) the local file through the same `Service` the server runs."""
    m = ctx.config.memory
    if m.url and not solo_db:
        return HttpTransport(m.url, read_token_file(m.token_file), timeout if timeout is not None else float(max(m.timeout_seconds, 10)),
                             m.ca_file)
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
        # another server: what the old one accepted says nothing about this one. Evidence pulled from it is still
        # somebody else's, so `pulled_ids` survives and is never pushed to the new server as ours.
        state = {"target": target_id(ctx, solo_db), "pulled_ids": list(state.get("pulled_ids") or [])}
    return state


_PULLED_META = "pulled_ids"


def _mirror_ids(db: MemoryDB) -> list[str]:
    """The pulled-evidence ids recorded in the store itself, so they survive a lost or corrupt `sync.json`."""
    try:
        raw = json.loads(db.meta_get(_PULLED_META) or "[]")
    except ValueError:
        return []
    return [i for i in raw if isinstance(i, str)] if isinstance(raw, list) else []


def _restore_pulled(db: MemoryDB, state: dict) -> list:
    """Merge the store's record of pulled ids into `state["pulled_ids"]` (ids only in `sync.json` from before the record
    existed are written back to it on `_store_pulled`). Returns the list held by `state`."""
    pulled = state.get("pulled_ids")
    if not isinstance(pulled, list):
        pulled = state["pulled_ids"] = []
    seen = set(pulled)
    for i in _mirror_ids(db):
        if i not in seen:
            seen.add(i)
            pulled.append(i)
    return pulled


def _store_pulled(db: MemoryDB, pulled: list) -> None:
    want = sorted({i for i in pulled if isinstance(i, str)})
    if want != sorted(set(_mirror_ids(db))) and (want or db.exists()):
        db.meta_set(_PULLED_META, json.dumps(want))


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


def push(ctx, transport, state: dict, *, now: float, solo_db: str = "", cancel: threading.Event | None = None) -> dict:
    m = ctx.config.memory
    if not m.share_repos:
        return {"pushed": False, "reason": "memory.share_repos is empty: nothing is pushed", "repos": []}
    meta = transport.meta()
    if meta.status != 200 or meta.body.get("max_level") not in levels.LEVELS:
        raise MemoryTransportError(f"the memory server's /v1/meta answered HTTP {meta.status} without a usable maximum level")
    server_max = meta.body["max_level"]
    db = MemoryDB(_store_dir(ctx), now=lambda: now)
    shadow = ctx.shadow or ShadowStore(expand_path(ctx.config.shadow.store))
    pulled = set(_restore_pulled(db, state))
    if pulled and db.exists():
        _store_pulled(db, state["pulled_ids"])  # records ids that so far lived only in sync.json
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
        save_state(ctx, state, cancel)
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

LOCK_WAIT_SECONDS = 2.0  # how long a run waits for another run's sync.json read-modify-write before giving up


@contextlib.contextmanager
def _state_lock(ctx, wait: float = LOCK_WAIT_SECONDS):
    """Yield True while this run holds `sync.json.lock`, False when it could not within `wait` (or the store directory is
    unusable). Callers fail soft on False: a lost update to the watermarks is worse than a skipped run."""
    lock = None
    held = False
    try:
        d = _store_dir(ctx)
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = FileLock(d / f"{STATE_FILE}.lock", stale_after=600)
        held = lock.acquire_wait(wait)
    except OSError:
        held = False
    try:
        yield held
    finally:
        if held and lock is not None:
            lock.release()


MAX_PURGED_IDS = 5000  # what one response may make us delete: a hostile server cannot name more than this per pull
_EVIDENCE_ID = re.compile(r"[0-9a-f]{16}")


def _purge_seq(state: dict, repo: str) -> int:
    v = (state.get("purge_seq") or {}).get(repo, 0)
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _parse_purged(body: dict) -> tuple[list[str], int | None]:
    """(evidence ids the server says were purged, the new watermark), or ([], None) when the answer carries none or is malformed.
    Nothing here is believed beyond shape: `_apply_purged` only ever removes rows this machine pulled."""
    raw, seq = body.get("purged"), body.get("purged_seq")
    if not isinstance(raw, list) or isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        return [], None
    ids = [i for i in raw[:MAX_PURGED_IDS] if isinstance(i, str) and _EVIDENCE_ID.fullmatch(i)]
    return ids, seq


def _apply_purged(db: MemoryDB, state: dict, pulled: list, known_pulled: set[str], ids: Sequence[str]) -> int:
    """Delete from the local replica the purged evidence that came from a pull. Local-origin evidence is never touched,
    whatever the server says: only ids recorded in `pulled_ids` qualify."""
    doomed = [i for i in ids if i in known_pulled]
    if not doomed:
        return 0
    gone = db.delete_evidence_ids(doomed)
    known_pulled.difference_update(doomed)
    gone_ids = set(doomed)
    pulled[:] = [i for i in pulled if i not in gone_ids]
    return gone

def pull(ctx, transport, repos: Sequence[str], state: dict, *, now: float, deadline: float | None = None) -> dict:
    db = MemoryDB(_store_dir(ctx), now=lambda: now)
    params = params_of(ctx)
    pulled = _restore_pulled(db, state)
    known_pulled = set(pulled)
    out, error = [], None
    since_map = state.setdefault("pull_since", {})
    for repo in repos:
        rep = {"repo": repo, "evidence": 0, "skipped": 0}
        out.append(rep)
        if error:
            continue
        since, cursor, newest = float(since_map.get(repo, 0.0)), "", 0.0
        items, purge_ids, purge_seq = [], [], None
        purge_start = _purge_seq(state, repo)
        for _ in range(MAX_PAGES):
            if deadline is not None and clock() >= deadline:
                error = "timed out"
                break
            resp = transport.get_lessons(repo, since, cursor, **({"purged_since": purge_start} if purge_start and not cursor else {}))
            if not cursor:
                purge_ids, purge_seq = _parse_purged(resp.body) if resp.status == 200 else ([], None)
                if resp.status == 200 and resp.body.get("purged_gap") is True and purge_start:
                    # the server pruned purge entries this machine never saw: nothing pulled can be trusted, so drop it and
                    # pull this repository from scratch (local-origin evidence stays: only `pulled_ids` qualify)
                    _apply_purged(db, state, pulled, known_pulled, [e.id for e in db.evidence(repo=repo)])
                    db.recompute(params, now)
                    since, purge_start, items, newest = 0.0, 0, [], 0.0
                    since_map.pop(repo, None)
                    continue
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
                newest = max(newest, ts if isinstance(ts, (int, float)) and not isinstance(ts, bool)
                             and math.isfinite(ts) and ts >= 0 else 0.0)
                items.append(ev)
            cursor = resp.body.get("next_cursor") or ""
            if not cursor:
                break
        if purge_seq is not None:
            rep["purged"] = _apply_purged(db, state, pulled, known_pulled, purge_ids)
            state.setdefault("purge_seq", {})[repo] = purge_seq
            if rep["purged"] and not items:
                db.recompute(params, now)
        if items:
            have = {e.id for e in db.evidence(repo=repo)}
            counts = db.upsert_evidence([e for e in items if (now - e.ts) / 86400.0 < params.expire_days])
            for e in items:
                if e.id not in have and e.id not in known_pulled:
                    known_pulled.add(e.id)
                    pulled.append(e.id)
            rep["evidence"] = counts["new"] + counts["updated"]
            db.recompute(params, now)
        _store_pulled(db, pulled)
        if newest:
            since_map[repo] = max(float(since_map.get(repo, 0.0)), newest - 60.0)  # overlap is safe: writes are idempotent
    return {"pulled": error is None, "repos": out, **({"error": error} if error else {})}


def pull_if_stale(ctx, max_age_seconds: float = 600.0, repos: Sequence[str] | None = None) -> dict:
    """Refresh the local replica from the server when it is older than `max_age_seconds`, within `memory.timeout_seconds`.

    Never raises and never blocks past the budget: on any failure the replica is left as it is. Only a configured
    `memory.url` is ever contacted. Without `repos` the repositories are `memory.share_repos`. With `repos` (the one being
    reviewed) they are pulled whether or not they are listed there: a pull is read-only and sends the server nothing but the
    repository name, so `share_repos` stays the list that gates what is pushed, not what may be read. The read-modify-write
    of `sync.json` holds `sync.json.lock`; a run that cannot get it quickly skips."""
    try:
        with _state_lock(ctx) as held:
            if not held:
                return {"pulled": False, "reason": "another sync is running"}
            return _pull_if_stale(ctx, max_age_seconds, repos)
    except Exception as e:  # noqa: BLE001 - reading memory must never break the caller
        return {"pulled": False, "error": type(e).__name__}


def _pull_if_stale(ctx, max_age_seconds: float = 600.0, repos: Sequence[str] | None = None) -> dict:
    try:
        m = ctx.config.memory
        if not m.url:
            return {"pulled": False, "reason": "no memory.url"}
        wanted = [r for r in dict.fromkeys(repos if repos is not None else m.share_repos) if _REPO_NAME.fullmatch(r)]
        if not wanted:
            return {"pulled": False, "reason": "no repositories to pull"}
        now = clock()
        state = _fresh_state(ctx)
        by_repo = state.get("last_pull_repo") if isinstance(state.get("last_pull_repo"), dict) else {}
        last = min(float(by_repo.get(r, 0.0)) for r in wanted) if repos is not None else float(state.get("last_pull_ts", 0.0))
        if now - last < max_age_seconds:
            return {"pulled": False, "reason": "replica is fresh"}
        if now - float(state.get("last_pull_failed_ts", 0.0)) < RETRY_AFTER_FAILED_PULL:
            return {"pulled": False, "reason": "a recent pull failed"}
        deadline = now + float(m.timeout_seconds)
        try:
            result = pull(ctx, HttpTransport(m.url, read_token_file(m.token_file), float(m.timeout_seconds), m.ca_file), wanted, state,
                          now=now, deadline=deadline)
        except CodeCheckError as e:
            result = {"pulled": False, "error": type(e).__name__}
        if result["pulled"]:
            state["last_pull_ts"] = now
            state["last_pull_repo"] = {**by_repo, **dict.fromkeys(wanted, now)}
        else:
            state["last_pull_failed_ts"] = now
        save_state(ctx, state)
        return result
    except Exception as e:  # noqa: BLE001 - reading memory must never break the caller
        return {"pulled": False, "error": type(e).__name__}


def note_pull_failure(ctx) -> None:
    """Stamp a failed pull so the next `RETRY_AFTER_FAILED_PULL` seconds do not try again. For a caller that stopped
    waiting for `pull_if_stale` (its worker thread dies with the process before it could write the stamp). Never raises."""
    try:
        if not ctx.config.memory.url:
            return
        with _state_lock(ctx) as held:
            if not held:
                return
            state = _fresh_state(ctx)
            state["last_pull_failed_ts"] = clock()
            save_state(ctx, state)
    except Exception:  # noqa: BLE001 - bookkeeping only
        return


# ---------- sync, status ----------

def sync(ctx, *, do_push: bool = True, do_pull: bool = True, solo_db: str = "", extra_repos: Sequence[str] = (),
         http_timeout: float | None = None, cancel: threading.Event | None = None) -> dict:
    """`http_timeout` bounds each HTTP call; `cancel`, once set by a caller that stopped waiting, stops every state write.
    The whole read-modify-write of `sync.json` holds `sync.json.lock`; when another run holds it, this one reports not ok."""
    with _state_lock(ctx) as held:
        if not held:
            return {"mode": "server" if ctx.config.memory.url and not solo_db else "solo", "ok": False,
                    "error": "another sync is running"}
        return _sync(ctx, do_push, do_pull, solo_db, extra_repos, http_timeout, cancel)


def _sync(ctx, do_push: bool, do_pull: bool, solo_db: str, extra_repos: Sequence[str], http_timeout: float | None,
          cancel: threading.Event | None) -> dict:
    m = ctx.config.memory
    now = clock()
    state = _fresh_state(ctx, solo_db)
    transport = build_transport(ctx, solo_db=solo_db, timeout=http_timeout)
    out: dict = {"mode": "server" if m.url and not solo_db else "solo", "ok": True}
    try:
        if do_push:
            out["push"] = push(ctx, transport, state, now=now, solo_db=solo_db, cancel=cancel)
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
    save_state(ctx, state, cancel)
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
