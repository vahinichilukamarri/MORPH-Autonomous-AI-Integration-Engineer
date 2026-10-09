"""Secret redaction: known values first, then secret-shaped strings. Fail closed, never print.

Two layers on purpose. Known values (the configured keys and tokens) are removed wherever they
appear. Patterns catch secrets we were not told about. The long-run rule is deliberately narrower
than the v0.4 gate's: a long identifier is not a secret, so a run must also mix digits and both
cases and have high entropy before it is redacted.
"""

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

REDACTED = "[redacted]"
MIN_SECRET_LENGTH = 6  # shorter configured values would redact ordinary words
ENTROPY_THRESHOLD = 4.0  # bits per character
LONG_RUN_MIN = 32

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
            re.DOTALL,
        ),
    ),
    ("BEARER", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}")),
    (
        "TOKEN_PREFIX",
        re.compile(
            r"\b(?:gsk|sk)[-_][A-Za-z0-9]{10,}\b"
            r"|\b(?:ghp|gho)_[A-Za-z0-9]{20,}\b"
            r"|\bxox[abprs]-[A-Za-z0-9\-]{10,}"
            r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
        ),
    ),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("URL_CREDENTIALS", re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@")),
)
_RUN = re.compile(rf"[A-Za-z0-9+/=_\-]{{{LONG_RUN_MIN},}}")


@dataclass(frozen=True)
class Redaction:
    text: str
    known: int  # known secret values removed
    patterns: int  # secret-shaped strings removed

    @property
    def changed(self) -> bool:
        return bool(self.known or self.patterns)


def entropy(text: str) -> float:
    counts = Counter(text)
    total = len(text)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def high_entropy_secret(run: str) -> bool:
    mixed = (
        any(c.isdigit() for c in run)
        and any(c.islower() for c in run)
        and any(c.isupper() for c in run)
    )
    return mixed and entropy(run) >= ENTROPY_THRESHOLD


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        values = {s for s in secrets if len(s) >= MIN_SECRET_LENGTH}
        self._values = sorted(values, key=len, reverse=True)

    def text(self, text: str) -> Redaction:
        known = 0
        for value in self._values:
            if value in text:
                known += text.count(value)
                text = text.replace(value, REDACTED)
        patterns = 0
        for _, pattern in _PATTERNS:
            text, n = pattern.subn(REDACTED, text)
            patterns += n
        runs = 0

        def long_run(match: re.Match[str]) -> str:
            nonlocal runs
            if high_entropy_secret(match.group(0)):
                runs += 1
                return REDACTED
            return match.group(0)

        text = _RUN.sub(long_run, text)
        return Redaction(text, known, patterns + runs)

    def value(self, value: Any) -> tuple[Any, int]:
        """Redact every string inside a JSON-like value (keys too); returns (clean, hits)."""
        if isinstance(value, str):
            done = self.text(value)
            return done.text, done.known + done.patterns
        if isinstance(value, dict):
            hits = 0
            out: dict[Any, Any] = {}
            for key, item in value.items():
                clean_key, key_hits = self.value(key)
                clean_item, item_hits = self.value(item)
                out[clean_key] = clean_item
                hits += key_hits + item_hits
            return out, hits
        if isinstance(value, (list, tuple)):
            hits = 0
            items: list[Any] = []
            for item in value:
                clean, n = self.value(item)
                items.append(clean)
                hits += n
            return items, hits
        return value, 0
