"""Review claims on the memory store and its callers: error translation, migration races, disk-touching refusals,
the master switch, the unchanged `context` output, lazy imports and the retrieval eval's default cases."""

import sqlite3
import subprocess
import sys
from dataclasses import replace

import pytest
from clihelp import call
from test_memory_collect import _cleanup_world
from test_memory_read import context, local_ctx

from codecheck.config import MemoryConfig
from codecheck.memory import semantic, sync
from codecheck.memory.db import SCHEMA_VERSION, MemoryDB, MemoryStoreError
from codecheck.memory.semantic import Query

pytestmark = pytest.mark.usefixtures("git_env")


# 1. only the open/PRAGMA/migrate step is translated; a caller's own database error is not "unreadable"

def test_a_caller_body_error_is_not_reported_as_an_unreadable_store(tmp_path):
    db = MemoryDB(tmp_path / "mem")
    db.put_lessons([])
    with pytest.raises(sqlite3.OperationalError, match="locked"), db._use(write=True):
        raise sqlite3.OperationalError("database is locked")


def test_a_busy_store_at_open_is_a_retryable_busy_error_not_unreadable(tmp_path, monkeypatch):
    db = MemoryDB(tmp_path / "mem")
    db.put_lessons([])

    def busy(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", busy)
    with pytest.raises(MemoryStoreError) as e, db._use(write=False):
        pass
    assert "busy" in str(e.value) and "unreadable" not in str(e.value)


def test_a_corrupt_file_is_still_unreadable(tmp_path):
    (tmp_path / "mem").mkdir()
    (tmp_path / "mem" / "memory.db").write_bytes(b"this is not a database" * 100)
    with pytest.raises(MemoryStoreError, match="unreadable"), MemoryDB(tmp_path / "mem")._use(write=False):
        pass


# 2. a second process creating the same new store must not fail on "table already exists"

class _StaleOnce:
    """A connection whose first user_version and table-count reads were taken before another process migrated."""

    def __init__(self, conn):
        self.conn, self.stale = conn, {"PRAGMA user_version", "SELECT count(*) FROM sqlite_master"}

    def execute(self, sql, *args):
        if sql in self.stale:
            self.stale.discard(sql)

            class Cur:
                def fetchone(self):
                    return (0,)

            return Cur()
        return self.conn.execute(sql, *args)


def test_a_lost_migration_race_returns_early(tmp_path):
    db = MemoryDB(tmp_path / "mem")
    db.put_lessons([])  # the "other process" already migrated
    conn = sqlite3.connect(db.file, isolation_level=None)
    try:
        db._migrate(_StaleOnce(conn), True)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


# 3. embed_at = "server" without a url refuses before anything is created

def test_server_ranked_without_a_url_touches_no_disk(tmp_path):
    ctx = local_ctx(tmp_path, memory=MemoryConfig(enabled=True, read=True, embed_model="m", embed_at="server",
                                                  store=str(tmp_path / "mem" / "store")))
    why: dict = {}
    assert semantic.server_ranked(ctx, "o/r", [Query("x")], "miss", 1.0, why) == []
    assert why["reason"] == "refused"
    assert not sync.default_solo_db(ctx).exists() and not (tmp_path / "mem").exists()


def test_server_ranked_for_an_unshared_repo_touches_no_disk(tmp_path):
    ctx = local_ctx(tmp_path, memory=MemoryConfig(enabled=True, read=True, embed_model="m", embed_at="server",
                                                  url="https://127.0.0.1:1", store=str(tmp_path / "mem" / "store")))
    why: dict = {}
    assert semantic.server_ranked(ctx, "o/r", [Query("x")], "miss", 1.0, why) == []
    assert why["reason"] == "refused" and not (tmp_path / "mem").exists()


# 4. memory.enabled is the master switch for automatic collection

def test_cleanup_collects_only_when_enabled_and_collect(tmp_path):
    ctx, out = _cleanup_world(tmp_path, True)
    ctx.config = replace(ctx.config, memory=replace(ctx.config.memory, enabled=False))
    code, _res = call(ctx, "cleanup", "--run", out["run_id"])
    assert code == 0
    assert not (tmp_path / "mem").exists()
    assert not any(k.startswith("memory:collect:") for k in ctx.state._load())


# 5. without memory enabled, `context` prints no memory key

def test_context_output_has_no_memory_key_when_memory_is_off(tmp_path):
    out, _meta = context(local_ctx(tmp_path))
    assert "memory" not in out
    (tmp_path / "on").mkdir()
    on, _ = context(local_ctx(tmp_path / "on", memory=MemoryConfig(enabled=True, store=str(tmp_path / "mem"))))
    assert "memory" in on


# 6. the memory stack is not loaded by ingest while memory is off

def test_ingest_does_not_import_the_semantic_stack():
    """`codecheck.commands` imports every command (eval_run pulls in semantic for `eval --memory-retrieval`), so isolation is
    checked on ingest's own imports: its source, and the memory modules it uses."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "codecheck" / "commands" / "ingest.py"
    imported = [n.module for n in ast.walk(ast.parse(src.read_text())) if isinstance(n, ast.ImportFrom) and n.module]
    assert "codecheck.memory.semantic" not in imported and "codecheck.memory.embed" not in imported
    root = Path(__file__).resolve().parent.parent / "src"
    code = (f"import sys; sys.path[:0] = [{str(root)!r}]; import codecheck.memory.inject, codecheck.memory.model; "
            "bad = [m for m in ('codecheck.memory.semantic', 'codecheck.memory.embed') if m in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_query_is_still_importable_from_semantic():
    from codecheck.memory.model import Query as lower

    assert semantic.Query is lower


# 7. the default retrieval cases being absent is a visible error, not a silent pass

def test_memory_retrieval_with_no_default_cases_fails_loudly(tmp_path, monkeypatch):
    from codecheck.evals import memory_retrieval

    ctx = local_ctx(tmp_path)
    monkeypatch.setattr(memory_retrieval, "DEFAULT_CASES", tmp_path / "absent")
    code, out = call(ctx, "eval", "--memory-retrieval")
    assert code != 0 and "retrieval cases" in str(out)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(memory_retrieval, "DEFAULT_CASES", empty)
    code, out = call(ctx, "eval", "--memory-retrieval")
    assert code != 0 and "retrieval cases" in str(out)
