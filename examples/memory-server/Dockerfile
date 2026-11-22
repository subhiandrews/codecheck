# Example only: code-check's memory server is one standard-library Python process, so Docker is optional.
# Build from the repository root:  docker build -f examples/memory-server/Dockerfile -t code-check-memory .
FROM python:3.14-slim

WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /src

# The store lives in a volume, owned by an unprivileged user.
RUN useradd --create-home --uid 10001 memory && mkdir -p /data && chown memory /data
USER memory
VOLUME /data
EXPOSE 8787

# --behind-proxy: put a TLS-terminating reverse proxy in front (see README.md). For a server on loopback only,
# publish the port as 127.0.0.1:8787:8787 and keep this flag.
# --allow-host: REQUIRED (the server refuses to start without one): the name clients use; other Host headers are rejected.
# The default CMD below therefore fails; pass `--allow-host your.name` (compose.yaml does).
ENTRYPOINT ["code-check", "memory", "serve", "--host", "0.0.0.0", "--port", "8787", "--db", "/data", "--behind-proxy"]
CMD ["--max-level", "lessons"]
