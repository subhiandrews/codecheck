"""The semantic retriever for `eval --memory-retrieval --retriever semantic`.

It runs the shipped second stage (`memory/semantic.py`: embed evidence text, rank by cosine similarity, merge after the
exact-key hits through the same gate) over the seeded case lessons. A seeded lesson has no evidence, so each lesson's
statement stands in as the text of one evidence row. Nothing here touches the user's store or the network beyond the
configured embedder: the case store is a temporary directory.

`register(ctx)` adds the retriever only when `memory.embed_model` is set; the retriever is then available only while the
embedding service answers, so `--if-reachable` turns an unreachable Ollama into a skip."""

from types import SimpleNamespace

from codecheck.evals.memory_retrieval import (
    NOW,
    RetrievalQuery,
    exact_retriever,
    register_retriever,
)
from codecheck.memory import semantic
from codecheck.memory.embed import Embedder
from codecheck.memory.model import Evidence, evidence_id
from codecheck.memory.retrieve import merge_ranked

NAME = "semantic"
BUDGET = 10.0


def _seed_evidence(db, repo: str, embedder: Embedder, cfg) -> None:
    """One evidence row per lesson, with the lesson's statement as its text, embedded once per case store."""
    if semantic.vector_stats(db)["vectors"]:
        return
    rows = []
    for le in db.lessons(repo=repo):
        miss = le.kind == "miss"
        rows.append(Evidence(
            evidence_id(le.scope.host, repo, f"{repo}#1", le.id, "human_miss" if miss else "verdict_valid"), le.id, le.scope,
            "human_miss" if miss else "verdict_valid", NOW - 86400, f"{repo}#1", le.id, "maintainer", "1",
            le.statement if miss else None, None if miss else {"severity": "warning", "message": le.statement}))
    db.upsert_evidence(rows)
    semantic.refresh_local(SimpleNamespace(config=SimpleNamespace(memory=cfg)), db, embedder=embedder, budget=BUDGET)


def _queries(q: RetrievalQuery) -> list[semantic.Query]:
    """What the shipped code would send: a diff query is the changed path with a summary of its added lines (a case may
    give that summary as `text`), a finding query is its category and message."""
    if q.mode == "diff":
        text = q.extra.get("text") or ""
        return [semantic.Query(f"{p}: {text}".strip(": "), p) for p in q.paths]
    return [semantic.Query(f"{f.get('category', '')} {f.get('message', '')}".strip(), f.get("file", ""), f.get("category", ""))
            for f in q.findings]


def make_retriever(embedder: Embedder, cfg):
    def retrieve(q: RetrievalQuery) -> list[str]:
        kind = "miss" if q.mode == "diff" else "finding"
        _seed_evidence(q.db, q.repo, embedder, cfg)
        ranked = semantic.rank_lessons(q.db, q.repo, _queries(q), embedder, min_similarity=cfg.min_similarity, budget=BUDGET,
                                       kind=kind) or []
        exact = [le for i in exact_retriever(q) if (le := q.db.lesson(i)) is not None]
        merged = merge_ranked(q.db, q.repo, kind, exact, ranked, q.files_at_base, q.limit, now=lambda: q.now)
        return [le.id for le in merged]

    return retrieve


def register(ctx, embedder: Embedder | None = None) -> bool:
    """Register `semantic` when `memory.embed_model` is set. Returns whether it did. `embedder` is for tests."""
    cfg = ctx.config.memory
    if not cfg.embed_model:
        return False
    emb = embedder or semantic.make_embedder(cfg)
    if emb is None:
        return False
    register_retriever(NAME, make_retriever(emb, cfg),
                       lambda: None if emb.digest() is not None else "the embedding service is unreachable or lacks the model")
    return True
