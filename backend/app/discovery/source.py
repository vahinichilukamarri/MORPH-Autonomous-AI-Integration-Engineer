"""Reading OpenAPI documents from a file or a URL, and hashing them."""

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from app.discovery.errors import ParseError, Problem


def load_spec(source: str | Path, *, timeout: float = 10.0) -> dict[str, Any]:
    """Load a JSON OpenAPI document from a local file or an http(s) URL."""
    text = str(source)
    try:
        if text.startswith(("http://", "https://")):
            response = httpx.get(text, timeout=timeout, follow_redirects=False)
            response.raise_for_status()
            data: Any = response.json()
        else:
            data = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, ValueError, httpx.HTTPError) as exc:
        raise ParseError([Problem(pointer="", message=f"cannot load {text}: {exc}")]) from exc
    if not isinstance(data, dict):
        raise ParseError([Problem(pointer="", message="the document is not a JSON object")])
    return data


def normalise(spec: dict[str, Any]) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace."""
    return json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def spec_hash(spec: dict[str, Any]) -> str:
    return hashlib.sha256(normalise(spec).encode("utf-8")).hexdigest()
