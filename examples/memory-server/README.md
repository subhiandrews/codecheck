# Memory server example

code-check can share what it learns from review feedback through a small server it ships. Nothing here is required: with `memory.url` empty, or `memory.share_repos` empty, nothing is ever sent. The full explanation (levels, trust, limits) is in [Hosting the memory server](../../docs/MAINTAINING.md#hosting-the-memory-server).

The server is one standard-library Python process. Docker is only one way to run it.

## Solo, on your machine, no Docker

```
code-check memory serve                       # 127.0.0.1:8787, prints one JSON line, then runs
code-check memory token add me                # prints the token once
umask 077 && printf '%s\n' cck_PASTE_IT > ~/.config/code-check/memory.token
```

In `~/.config/code-check/config.toml`:

```toml
[memory]
url = "http://127.0.0.1:8787"
token_file = "~/.config/code-check/memory.token"
share_repos = ["owner/name"]      # empty pushes nothing
```

Then `code-check memory sync` and `code-check memory status`. `http://` is accepted only for this machine.

## Team

1. Run the server where everyone can reach it, behind a reverse proxy that terminates TLS (Caddy, nginx, a cloud load balancer). The server binds loopback by default and refuses any other address unless you pass `--behind-proxy` (a proxy in front) or `--tls-cert` and `--tls-key` (it terminates TLS itself), and then also requires at least one `--allow-host your.public.name` (it will not start without it). With your own certificate authority or a self-signed certificate, each client sets `memory.ca_file` to the PEM file.
2. Give each person their own token: `code-check memory token add alice`. The name is the contributor id; `--admin` additionally allows deleting a contributor's evidence. **Give `--maintainer` to everyone with write access to the repositories:** a token without it can only send evidence as a contributor (a claimed `maintainer` is stored as `contributor`), and a lesson needs a maintainer behind it to activate. List with `memory token list`; remove with `memory token revoke alice --purge` (the evidence they sent goes too, and teammates' replicas drop it on their next pull).
3. Each person sets `memory.url = "https://memory.example.com"`, a `token_file` (mode 0600) and `share_repos`.
4. Choose the most detail the server accepts with `--max-level lessons|evidence|episodes` (default `lessons`). Each client can send less with `memory.share_level`; private repositories, and repositories whose visibility cannot be determined, are capped at `memory.private_level` (default `lessons`).

| Level | What leaves the machine |
|---|---|
| `lessons` | each lesson's evidence: ids, scope, signal, role, PR, thread, time. No quote, no finding text |
| `evidence` | adds a scrubbed quote (240 characters, 3 per lesson) and the finding's severity and message (400) |
| `episodes` | adds scrubbed shadow records per run, never the diff |

A field above the server's maximum is refused with 422, never silently dropped.

## Docker

```
cd examples/memory-server
docker compose up -d --build
docker compose exec memory code-check memory token add alice --maintainer --db /data
```

`--db /data` is required: the server in the image keeps its store in `/data`, and `token add` without it writes to a different, unused store in the container, so the token would be rejected with 401.

`compose.yaml` publishes the server on `127.0.0.1:8787` only, keeps the store in a volume, and starts an `ollama/ollama` container for the optional [semantic retrieval](../../docs/MAINTAINING.md#semantic-retrieval-opt-in-default-off) (see the comments in `compose.yaml`). The image runs `code-check memory serve --behind-proxy`; keep the published port on loopback and put your proxy in front, and replace `memory.example.com` in the `command` of `compose.yaml` with your hostname (`--allow-host` is required; the server refuses to start without one).
## Operating notes

- Back up the store by copying the `/data` volume (SQLite, WAL mode) while the server is stopped, or with `sqlite3 memory.db ".backup copy.db"`.
- Logs hold method, path (no query), status and contributor name, never tokens or text.
- Roles in evidence are asserted by clients but capped by the token's ceiling (`--maintainer`); otherwise anyone holding a token is trusted. Revoke tokens that should not be.
- After 10 failed tokens from one address in a minute the server answers 429 with `Retry-After` for a minute. Behind a proxy it reads the last `X-Forwarded-For` entry, so make the proxy set it.
- A review pulls the repository it reviews whenever `memory.url` is set, even if it is not in `share_repos`; that sends only the repository name, which you as the operator can see.
