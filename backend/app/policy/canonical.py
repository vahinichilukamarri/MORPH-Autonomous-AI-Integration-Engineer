"""Canonical JSON and hashes. The same data always gives the same bytes."""

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, non-ASCII kept; floats are not used in hashed data."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    return sha256_hex(canonical_json(value))
