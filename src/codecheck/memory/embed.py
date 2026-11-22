"""Text embeddings for semantic memory retrieval, from a local Ollama. Standard library only, plain HTTP, no MCP.

`OllamaEmbedder` posts to `{embed_url}/api/embed` and asks `/api/tags` for the model's digest. It never raises: a refused
URL (plain http to anything but this machine), an unreachable or slow service, a redirect, an oversized or malformed
answer, an HTTP error or a dropped connection all give `None`, and the caller falls back to exact-key retrieval. The whole
call keeps to one time budget (5 seconds by default).

A model is pinned as `Pin(model, digest, dim)`: vectors from different pins are never compared. Vectors are normalised
float32 arrays stored as blobs, so cosine similarity is one dot product in pure Python.
"""

import base64
import http.client
import json
import math
import time
import urllib.error
import urllib.request
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from codecheck.errors import CodeCheckError
from codecheck.memory.client import check_url

BATCH = 32  # texts per /api/embed request
BUDGET_SECONDS = 5.0
RESPONSE_CAP = 8 * 1024 * 1024
TEXT_CAP = 1000  # characters of one text sent for embedding
MAX_DIM = 4096
MAX_CANDIDATES = 300  # evidence vectors compared per ranking
MAX_QUERIES = 40  # query texts per ranking
QUERY_CAP = 400  # characters of one query text


@dataclass(frozen=True)
class Pin:
    """The identity of an embedding space. Two vectors are comparable only when their pins are equal."""
    model: str
    digest: str
    dim: int

    def as_dict(self) -> dict:
        return {"model": self.model, "digest": self.digest, "dim": self.dim}


def parse_pin(raw: object) -> Pin | None:
    if not isinstance(raw, dict):
        return None
    model, digest, dim = raw.get("model"), raw.get("digest"), raw.get("dim")
    if (isinstance(model, str) and model and len(model) <= 200 and isinstance(digest, str) and digest and len(digest) <= 200
            and isinstance(dim, int) and not isinstance(dim, bool) and 0 < dim <= MAX_DIM):
        return Pin(model, digest, dim)
    return None


class Embedder(Protocol):
    """What retrieval needs. `embed` returns one vector per text, or None on any failure; `digest` the model's content digest."""
    model: str

    def digest(self) -> str | None: ...

    def embed(self, texts: Sequence[str], budget: float = BUDGET_SECONDS) -> list[list[float]] | None: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class OllamaEmbedder:
    def __init__(self, url: str, model: str, budget: float = BUDGET_SECONDS) -> None:
        self.url, self.model, self.budget = url, model, budget

    def _base(self) -> str | None:
        try:
            return check_url(self.url)
        except CodeCheckError:
            return None  # plain http to another machine, credentials, junk: refused

    def _call(self, method: str, path: str, body: dict | None, timeout: float):
        base = self._base()
        if base is None or timeout <= 0:
            return None
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(  # noqa: S310 - scheme and host are checked in check_url
            base + path, data=data, method=method,
            headers={"Accept": "application/json", **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                raw = resp.read(RESPONSE_CAP + 1)
            if len(raw) > RESPONSE_CAP:
                return None
            parsed = json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError, UnicodeDecodeError):
            return None  # HTTPError is a URLError; a redirect surfaces as one
        return parsed if isinstance(parsed, dict) else None

    def digest(self) -> str | None:
        """The digest Ollama reports for the model, or None when it is unreachable or does not have the model."""
        out = self._call("GET", "/api/tags", None, self.budget)
        models = out.get("models") if out else None
        if not isinstance(models, list):
            return None
        for m in models:
            names = (self.model, f"{self.model}:latest")
            if isinstance(m, dict) and (m.get("name") in names or m.get("model") in names):
                d = m.get("digest")
                return d if isinstance(d, str) and d else None
        return None

    def embed(self, texts: Sequence[str], budget: float | None = None) -> list[list[float]] | None:
        texts = [t[:TEXT_CAP] for t in texts]
        if not texts:
            return []
        deadline = time.monotonic() + (self.budget if budget is None else budget)
        out: list[list[float]] = []
        dim = 0
        for i in range(0, len(texts), BATCH):
            chunk = texts[i:i + BATCH]
            res = self._call("POST", "/api/embed", {"model": self.model, "input": chunk}, deadline - time.monotonic())
            vecs = res.get("embeddings") if res else None
            if not isinstance(vecs, list) or len(vecs) != len(chunk):
                return None
            for v in vecs:
                if not isinstance(v, list) or not v or len(v) > MAX_DIM or (dim and len(v) != dim):
                    return None
                if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v):
                    return None
                dim = len(v)
                out.append([float(x) for x in v])
        return out


# ---------- vectors ----------

def normalise(vec: Sequence[float], dim: int | None = None) -> array:
    """A unit-length float32 array, cut to `dim` first when given (a prefix of a Matryoshka-style embedding)."""
    v = array("f", vec[:dim] if dim else vec)
    norm = math.sqrt(sum(x * x for x in v))
    if norm > 0:
        v = array("f", (x / norm for x in v))
    return v


def pack(vec: Sequence[float], dim: int | None = None) -> bytes:
    return normalise(vec, dim).tobytes()


def unpack(blob: bytes) -> array | None:
    if not isinstance(blob, (bytes, bytearray)) or len(blob) % 4 or not blob:
        return None
    v = array("f")
    v.frombytes(bytes(blob))
    return v


def to_b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


def from_b64(text: object, dim: int) -> bytes | None:
    """The float32 blob in `text`, or None unless it holds exactly `dim` finite numbers."""
    if not isinstance(text, str) or len(text) > 4 * MAX_DIM * 2:
        return None
    try:
        blob = base64.b64decode(text, validate=True)
    except ValueError:
        return None
    v = unpack(blob)
    if v is None or len(v) != dim or not all(math.isfinite(x) for x in v):
        return None
    return blob


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two unit vectors (those `normalise` makes)."""
    return sum(x * y for x, y in zip(a, b, strict=True)) if len(a) == len(b) else 0.0


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in pure Python. Vectors built by `normalise` are unit length, so this is a plain dot product."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
