"""HTTP client: injectable transport and clock, auth, categorised errors, safe retries.

Retries are applied to 429/5xx, timeouts, connection failures and unparseable JSON. A method that
is not idempotent (POST) is retried only when the server certainly did not process it: a 429, or
a connection that never opened. An ambiguous failure of a POST is returned to the caller, which
must look the record up before trying again.
"""

import json
import random
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

from morph_runtime.auth import Auth
from morph_runtime.errors import Category, RuntimeFailure, category_for_status
from morph_runtime.retry import (
    RETRYABLE_STATUSES,
    Clock,
    RetryPolicy,
    SystemClock,
    parse_retry_after,
)

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})
MAX_BODY_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


class TransportError(Exception):
    def __init__(self, kind: str, *, sent: bool) -> None:
        super().__init__(kind)
        self.kind = kind  # "timeout" or "connection"
        self.sent = sent  # whether the request may have reached the server


class Transport(Protocol):
    def send(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Response: ...


class UrllibTransport:
    def send(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Response:
        if not url.startswith(("http://", "https://")):
            raise RuntimeFailure(Category.CONFIGURATION, "only http(s) URLs are allowed")
        request = urllib.request.Request(  # noqa: S310
            url, data=body, headers=dict(headers), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as raw:  # noqa: S310
                return Response(raw.status, dict(raw.headers.items()), raw.read(MAX_BODY_BYTES))
        except urllib.error.HTTPError as error:
            return Response(error.code, dict(error.headers.items()), error.read(MAX_BODY_BYTES))
        except TimeoutError as error:
            raise TransportError("timeout", sent=True) from error
        except urllib.error.URLError as error:
            kind = "timeout" if isinstance(error.reason, TimeoutError) else "connection"
            raise TransportError(kind, sent=kind == "timeout") from error
        except OSError as error:
            raise TransportError("connection", sent=False) from error


@dataclass
class HttpClient:
    base_url: str
    auth: Auth
    transport: Transport = field(default_factory=UrllibTransport)
    clock: Clock = field(default_factory=SystemClock)
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    timeout: float = 10.0
    rng: random.Random = field(default_factory=lambda: random.Random(0))
    requests_made: int = 0
    retries_made: int = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str | int] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        """Send one logical request and return the decoded JSON body (``None`` if empty)."""
        method = method.upper()
        url = self.base_url.rstrip("/") + path
        if query:
            url += "?" + urlencode({k: str(v) for k, v in query.items()})
        headers = {"Accept": "application/json", **self.auth.headers()}
        payload: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        idempotent = method in IDEMPOTENT_METHODS
        started = self.clock.now()
        attempt = 0
        while True:
            attempt += 1
            result, delay = self._attempt(method, url, headers, payload, idempotent)
            if not isinstance(result, RuntimeFailure):
                return result
            if delay is None or attempt >= self.policy.max_attempts:
                raise result
            wait = self.policy.backoff(attempt, self.rng) if delay == 0 else delay
            if self.clock.now() - started + wait > self.policy.total_budget:
                raise result
            self.retries_made += 1
            self.clock.sleep(wait)

    def _attempt(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: bytes | None,
        idempotent: bool,
    ) -> tuple[Any, float | None]:
        """One attempt: ``(decoded body, None)`` or ``(failure, retry delay or None)``.

        A delay of ``0`` means "use the backoff schedule"; ``None`` means "do not retry".
        """
        self.requests_made += 1
        try:
            response = self.transport.send(method, url, headers, payload, self.timeout)
        except TransportError as error:
            category = Category.TIMEOUT if error.kind == "timeout" else Category.NETWORK
            failure = RuntimeFailure(category, f"{method} {_path(url)}: {error.kind}")
            return failure, (0.0 if idempotent or not error.sent else None)
        if response.status >= 400:
            return self._http_failure(method, url, response, idempotent)
        if not response.body.strip():
            return None, None
        try:
            return json.loads(response.body), None
        except ValueError:
            failure = RuntimeFailure(
                Category.MALFORMED_RESPONSE,
                f"{method} {_path(url)}: response is not valid JSON",
                status=response.status,
            )
            return failure, (0.0 if idempotent else None)

    def _http_failure(
        self, method: str, url: str, response: Response, idempotent: bool
    ) -> tuple[RuntimeFailure, float | None]:
        category = category_for_status(response.status)
        detail = f"{method} {_path(url)} -> HTTP {response.status}"
        fields = _error_fields(response.body) if category is Category.VALIDATION else ()
        failure = RuntimeFailure(category, detail, status=response.status, fields=fields)
        if response.status not in RETRYABLE_STATUSES:
            return failure, None
        if response.status == 429:
            advised = parse_retry_after(_header(response.headers, "Retry-After"))
            if advised is not None and advised > self.policy.max_retry_after:
                return failure, None
            return failure, (advised if advised is not None else 0.0)
        return failure, (0.0 if idempotent else None)


def _path(url: str) -> str:
    return "/" + url.split("//", 1)[-1].split("/", 1)[-1].split("?", 1)[0]


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _error_fields(body: bytes) -> tuple[str, ...]:
    """Field names named by a validation error body (``loc`` entries or a ``field`` key)."""
    try:
        data = json.loads(body)
    except ValueError:
        return ()
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            loc = node.get("loc")
            if isinstance(loc, list) and loc and isinstance(loc[-1], str):
                found.append(loc[-1])
            if isinstance(node.get("field"), str):
                found.append(node["field"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return tuple(dict.fromkeys(found))
