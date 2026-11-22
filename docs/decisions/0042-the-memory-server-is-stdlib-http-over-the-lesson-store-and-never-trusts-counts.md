# 0042 The memory server is stdlib HTTP over the lesson store and never trusts counts
Status: accepted 2026-11-21
Decision: memory serve is http.server plus sqlite3 over the same lesson store. Tokens are named, hashed and revocable per contributor; counts are recomputed from evidence rows; a field above the server's level is a 422; non-loopback binds need TLS or a proxy.
Why: Keeps dependencies empty, lets one person's evidence be removed, and a client cannot inflate a lesson.
Rejected: Postgres or a hosted product; trusting client counts; silently stripping fields.
