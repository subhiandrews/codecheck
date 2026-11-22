"""Talking to a memory server: over HTTP, or (solo mode) straight into a local SQLite file through the same `Service`.

Both transports answer `Response(status, body)` for every status the server can give, so callers handle a 4xx the same
way in both. Only a failure to get any usable answer raises `MemoryTransportError`: an unreachable host, a timeout, a
dropped connection, a malformed HTTP reply or a body that is not a JSON object. The transport never follows a redirect
(it would carry the token to another host), refuses http except to this machine, refuses credentials in the URL, reads
the token from a file only its owner can read, caps what it reads back, and never puts the token in a message.
"""

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from codecheck import __version__
from codecheck.config import is_loopback_host as is_loopback
from codecheck.errors import CodeCheckError
from codecheck.shadow.scrub import scrub_text

if TYPE_CHECKING:
    import urllib.request

    from codecheck.memory.server import Service

READ_CAP = 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f\s]")
SOLO_CONTRIBUTOR = "local"


class MemoryTransportError(CodeCheckError):
    """No usable answer came back. Always safe to retry later; the message never holds a token."""


@dataclass(frozen=True)
class Response:
    status: int
    body: dict


def check_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        raise CodeCheckError("memory.url is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not host or _CONTROL.search(url):
        raise CodeCheckError("memory.url must be an http:// or https:// URL")
    if parts.username or parts.password:
        raise CodeCheckError("memory.url must not contain credentials: put the token in memory.token_file")
    if parts.scheme == "http" and not is_loopback(host):
        raise CodeCheckError("memory.url must use https:// unless it points at this machine")
    return url.rstrip("/")


def read_token_file(path: str) -> str:
    """The token in `path`: a regular file only its owner can read, holding one non-empty line."""
    if not path:
        raise CodeCheckError("memory.token_file is not set")
    p = os.path.expanduser(path)
    try:
        st = os.stat(p)
        if not stat.S_ISREG(st.st_mode):
            raise CodeCheckError("memory.token_file is not a regular file")
        if st.st_mode & 0o077:
            raise CodeCheckError("memory.token_file is readable by other users: chmod 600 it")
        token = Path(p).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise CodeCheckError(f"cannot read memory.token_file: {type(e).__name__}") from None
    except UnicodeDecodeError:
        raise CodeCheckError("memory.token_file is not text") from None
    if not token or _CONTROL.search(token):
        raise CodeCheckError("memory.token_file must hold one token on a single line")
    return token


def _no_redirect_handler():
    import urllib.request  # lazy: the HTTP stack is not loaded until a request is made

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None  # a redirect would carry the Authorization header to another host

    return _NoRedirect


@dataclass(frozen=True)
class HttpTransport:
    url: str
    token: str = field(repr=False)
    timeout: float = 3.0
    ca_file: str = ""  # PEM bundle to trust instead of the system store (a private CA or a self-signed server); https only

    def __post_init__(self) -> None:
        check_url(self.url)

    def _opener(self) -> urllib.request.OpenerDirector:
        import ssl
        import urllib.request

        handlers: list = [_no_redirect_handler()]
        if self.ca_file and urlsplit(self.url).scheme == "https":
            try:  # verification and host-name checks stay on: this only changes which authority is trusted
                ctx = ssl.create_default_context(cafile=os.path.expanduser(self.ca_file))
            except (OSError, ssl.SSLError):
                raise MemoryTransportError("cannot load memory.ca_file") from None
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        return urllib.request.build_opener(*handlers)

    def _call(self, method: str, path: str, body: object | None = None) -> Response:
        import http.client
        import urllib.error
        import urllib.request

        data = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
        req = urllib.request.Request(  # noqa: S310 - scheme and host are checked in check_url
            check_url(self.url) + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                     "User-Agent": f"code-check/{__version__}", **({"Content-Type": "application/json"} if data else {})})
        try:
            with self._opener().open(req, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read(READ_CAP + 1)
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read(READ_CAP + 1)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as e:
            raise MemoryTransportError(f"cannot reach the memory server {urlsplit(self.url).netloc}: {type(e).__name__}") from None
        if len(raw) > READ_CAP:
            raise MemoryTransportError("the memory server's answer is too large")
        if 300 <= status < 400:
            raise MemoryTransportError("the memory server redirected; redirects are not followed")
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw.strip() else None
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if not isinstance(parsed, dict):
            if 200 <= status < 300:
                raise MemoryTransportError("the memory server's answer is not a JSON object")
            parsed = {"error": scrub_text(raw.decode("utf-8", errors="replace"))[:200]}
        return Response(status, parsed)

    def meta(self) -> Response:
        return self._call("GET", "/v1/meta")

    def put_lessons(self, items: list[dict]) -> Response:
        return self._call("PUT", "/v1/lessons", {"items": items})

    def put_episodes(self, episodes: list[dict]) -> Response:
        return self._call("PUT", "/v1/episodes", {"episodes": episodes})

    def get_lessons(self, repo: str, since: float = 0.0, cursor: str = "", purged_since: int = 0) -> Response:
        q = f"?repo={quote(repo, safe='/')}&since={since!r}" + (f"&cursor={quote(cursor, safe='|.')}" if cursor else "")
        if purged_since > 0:
            q += f"&purged_since={int(purged_since)}"
        return self._call("GET", "/v1/lessons" + q)

    def search(self, body: dict) -> Response:
        return self._call("POST", "/v1/search", body)


@dataclass(frozen=True)
class LocalTransport:
    """Solo mode: no daemon, no token. The caller and the store are the same person on the same machine."""
    service: Service
    contributor: str = SOLO_CONTRIBUTOR

    def _wrap(self, result: tuple[int, dict]) -> Response:
        return Response(*result)

    def meta(self) -> Response:
        return self._wrap(self.service.meta())

    def put_lessons(self, items: list[dict]) -> Response:
        # solo mode: the sender is the person whose roles these are, so its own assertions stand
        return self._wrap(self.service.put_lessons(self.contributor, {"items": items}, "maintainer"))

    def put_episodes(self, episodes: list[dict]) -> Response:
        return self._wrap(self.service.put_episodes(self.contributor, {"episodes": episodes}))

    def get_lessons(self, repo: str, since: float = 0.0, cursor: str = "", purged_since: int = 0) -> Response:
        q = {"repo": [repo], "since": [repr(since)], **({"cursor": [cursor]} if cursor else {}),
             **({"purged_since": [str(int(purged_since))]} if purged_since > 0 else {})}
        return self._wrap(self.service.get_lessons(q))

    def search(self, body: dict) -> Response:
        return self._wrap(self.service.search(body))
