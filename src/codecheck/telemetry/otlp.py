"""OTLP/HTTP JSON transport: resolve the endpoint and headers from the standard OTEL_* environment, and POST.

The endpoint may come from `telemetry.endpoint` or OTEL_EXPORTER_OTLP_(TRACES_)ENDPOINT. The token comes from
OTEL_EXPORTER_OTLP_(TRACES_)HEADERS or from the file `telemetry.token_file` names: a secret is never a config value, and
code-check never writes one. A remote endpoint is only sent to with a non-empty token; localhost needs none, for testing.
"""

import os
import re
import stat
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from codecheck import __version__
from codecheck.config import TelemetryConfig
from codecheck.errors import CodeCheckError
from codecheck.shadow.scrub import scrub_text

_TOKEN = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RESERVED = {"content-type", "content-length", "host", "transfer-encoding", "connection"}
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_READ_CAP = 65536


@dataclass(frozen=True)
class Target:
    url: str
    headers: dict[str, str] = field(repr=False)  # holds the token; never printed
    resource: dict[str, str] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc


def _split_pairs(raw: str) -> list[tuple[str, str]]:
    out = []
    for part in raw.split(","):
        if not part.strip():
            continue
        key, sep, value = part.partition("=")
        out.append((key.strip(), unquote(value.strip()) if sep else ""))
    return out


def parse_headers(raw: str, name: str) -> dict[str, str]:
    headers = {}
    for key, value in _split_pairs(raw):
        if not _TOKEN.fullmatch(key) or key.lower() in _RESERVED or _CONTROL.search(value):
            raise CodeCheckError(f"{name} has an invalid entry")  # never echo it: the value may be a token
        headers[key] = value
    return headers


def _check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or _CONTROL.search(url) or " " in url:
        raise CodeCheckError("the OTLP endpoint must be an http:// or https:// URL")
    if parts.username or parts.password:
        raise CodeCheckError("the OTLP endpoint must not contain credentials: put the token in OTEL_EXPORTER_OTLP_HEADERS")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise CodeCheckError("the OTLP endpoint must use https:// unless it is localhost")
    return url


def _credential(value: str) -> str:
    """The secret part of a header value: "Bearer " alone, or blanks, is no credential."""
    parts = value.split(None, 1)
    if len(parts) == 1 and parts[0].lower() in ("bearer", "basic", "token"):
        return ""
    return value.strip()


def read_token_file(path: str) -> str:
    """The token in `path`. The file must be a regular file only its owner can read, and hold a one-line token."""
    p = os.path.expanduser(path)
    try:
        st = os.stat(p)
        if not stat.S_ISREG(st.st_mode):
            raise CodeCheckError("telemetry.token_file is not a regular file")
        if st.st_mode & 0o077:
            raise CodeCheckError("telemetry.token_file is readable by other users: chmod 600 it")
        token = open(p, encoding="utf-8").read().strip()  # noqa: SIM115 - one read, closed on return
    except OSError as e:
        raise CodeCheckError(f"cannot read telemetry.token_file: {type(e).__name__}") from None
    except UnicodeDecodeError:
        raise CodeCheckError("telemetry.token_file is not text") from None
    if not token or _CONTROL.search(token) or " " in token:
        raise CodeCheckError("telemetry.token_file must hold one non-empty token on a single line")
    return token


def _with_token(cfg: TelemetryConfig, headers: dict[str, str], url: str) -> dict[str, str]:
    name = cfg.auth_header
    present = next((k for k in headers if k.lower() == name.lower()), None)
    if present is None and cfg.token_file:
        token = read_token_file(cfg.token_file)
        headers = {**headers, name: f"Bearer {token}" if name.lower() == "authorization" else token}
        present = name
    if urlsplit(url).hostname in _LOOPBACK:
        return headers  # a collector on this machine, for testing
    if present is None or not _credential(headers[present]):
        raise CodeCheckError(
            f"a token is required to send to {urlsplit(url).netloc}: set {name} in OTEL_EXPORTER_OTLP_HEADERS "
            "or point telemetry.token_file at a file holding it")
    return headers


def resource_attributes(env: Mapping[str, str]) -> dict[str, str]:
    out = {k: scrub_text(v)[:256] for k, v in _split_pairs(env.get("OTEL_RESOURCE_ATTRIBUTES", "")) if _TOKEN.fullmatch(k)}
    out["service.name"] = env.get("OTEL_SERVICE_NAME") or out.get("service.name") or "code-check"
    out["service.version"] = __version__
    return out


def resolve_target(cfg: TelemetryConfig, env: Mapping[str, str]) -> Target | None:
    """None when no endpoint is configured (or OTEL_SDK_DISABLED is true); CodeCheckError when one is unusable."""
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None
    traces = env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    base = (cfg.endpoint or env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")).strip()
    if cfg.endpoint:
        url = _traces_url(cfg.endpoint.strip())
    elif traces:
        url = traces
    elif base:
        protocol = (env.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or env.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "").strip()
        if protocol == "grpc":
            raise CodeCheckError(
                "OTEL_EXPORTER_OTLP_PROTOCOL=grpc points at a gRPC port, but code-check sends OTLP/HTTP JSON: "
                "set telemetry.endpoint to the collector's HTTP URL (port 4318 by default)")
        url = _traces_url(base)
    else:
        return None
    header_var = "OTEL_EXPORTER_OTLP_TRACES_HEADERS" if env.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS") else "OTEL_EXPORTER_OTLP_HEADERS"
    url = _check_url(url)
    headers = _with_token(cfg, parse_headers(env.get(header_var, ""), header_var), url)
    return Target(url, headers, resource_attributes(env))


def _traces_url(base: str) -> str:
    base = base.rstrip("/")
    return base if base.endswith("/v1/traces") else f"{base}/v1/traces"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None  # a redirect would carry the Authorization header to another host


def post_json(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> tuple[int, str]:
    """POST and return (status, body). Only a transport failure raises; the caller decides what a status means."""
    req = urllib.request.Request(  # noqa: S310 - the scheme is checked by _check_url
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": f"code-check/{__version__}", **headers})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
            return resp.status, resp.read(_READ_CAP).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(_READ_CAP).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CodeCheckError(f"cannot reach the OTLP endpoint {urlsplit(url).netloc}: {type(e).__name__}") from None
