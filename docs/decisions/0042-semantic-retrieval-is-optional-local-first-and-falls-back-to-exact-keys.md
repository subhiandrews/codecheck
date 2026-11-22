# 0042 Semantic retrieval is optional, local-first and falls back to exact keys
Status: accepted 2026-11-21
Decision: With memory.embed_model set, evidence text is embedded by a local Ollama and ranked by cosine similarity after the exact-key hits. Models are pinned by digest; a mismatch sends no vectors. Server-side search needs --allow-search and level evidence on a public repo.
Why: Finds lessons whose wording matches but whose key does not, while default behaviour and any failure stay exact-key.
Rejected: A vector database; MCP; embedding the lesson statement; mixing vectors from different models.
