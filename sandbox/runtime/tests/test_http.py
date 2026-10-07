import json
import random
from collections.abc import Mapping

import pytest

from morph_runtime.auth import ApiKeyAuth, BearerAuth
from morph_runtime.errors import Category, RuntimeFailure
from morph_runtime.http import HttpClient, Response, TransportError
from morph_runtime.retry import RetryPolicy, parse_retry_after


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class ScriptedTransport:
    """Returns scripted outcomes in order: a Response or a TransportError to raise."""

    def __init__(self, *script: Response | TransportError) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str, Mapping[str, str], bytes | None]] = []

    def send(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Response:
        self.calls.append((method, url, headers, body))
        step = self.script.pop(0)
        if isinstance(step, TransportError):
            raise step
        return step


def ok(payload: object, status: int = 200) -> Response:
    return Response(status, {}, json.dumps(payload).encode())


def client(transport: ScriptedTransport, **policy: float) -> tuple[HttpClient, FakeClock]:
    clock = FakeClock()
    return (
        HttpClient(
            "http://crm:8101",
            ApiKeyAuth("X-API-Key", "k"),
            transport=transport,
            clock=clock,
            policy=RetryPolicy(**policy),  # type: ignore[arg-type]
            rng=random.Random(1),
        ),
        clock,
    )


def test_auth_headers_and_json_body() -> None:
    transport = ScriptedTransport(ok({"a": 1}))
    http, _ = client(transport)
    assert http.request("POST", "/x", body={"n": 1}, query={"page": 2}) == {"a": 1}
    method, url, headers, body = transport.calls[0]
    assert (method, url) == ("POST", "http://crm:8101/x?page=2")
    assert headers["X-API-Key"] == "k" and headers["Content-Type"] == "application/json"
    assert body == b'{"n": 1}'


def test_bearer_header_and_no_secret_in_repr() -> None:
    assert BearerAuth("tok").headers() == {"Authorization": "Bearer tok"}
    assert "tok" not in repr(BearerAuth("tok"))
    assert "secret" not in repr(ApiKeyAuth("X-API-Key", "secret"))


def test_retries_5xx_then_succeeds_with_backoff() -> None:
    transport = ScriptedTransport(Response(500, {}, b""), Response(503, {}, b""), ok([1]))
    http, clock = client(transport)
    assert http.request("GET", "/x") == [1]
    assert http.requests_made == 3 and http.retries_made == 2
    assert len(clock.slept) == 2 and clock.slept[1] > clock.slept[0] > 0


def test_gives_up_after_max_attempts_with_server_category() -> None:
    transport = ScriptedTransport(*[Response(500, {}, b"")] * 4)
    http, _ = client(transport)
    with pytest.raises(RuntimeFailure) as caught:
        http.request("GET", "/x")
    assert caught.value.category is Category.SERVER and http.requests_made == 4


def test_429_honours_retry_after() -> None:
    transport = ScriptedTransport(Response(429, {"retry-after": "3"}, b""), ok({}))
    http, clock = client(transport)
    http.request("GET", "/x")
    assert clock.slept == [3.0]


def test_retry_after_beyond_cap_fails_without_waiting() -> None:
    transport = ScriptedTransport(Response(429, {"Retry-After": "600"}, b""))
    http, clock = client(transport)
    with pytest.raises(RuntimeFailure) as caught:
        http.request("GET", "/x")
    assert caught.value.category is Category.RATE_LIMIT and clock.slept == []


def test_total_budget_stops_retrying() -> None:
    transport = ScriptedTransport(*[Response(429, {"Retry-After": "20"}, b"")] * 4)
    http, clock = client(transport, total_budget=30.0)
    with pytest.raises(RuntimeFailure):
        http.request("GET", "/x")
    assert clock.t <= 30.0 and http.requests_made == 2


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_is_not_retried(status: int) -> None:
    transport = ScriptedTransport(Response(status, {}, b""))
    http, _ = client(transport)
    with pytest.raises(RuntimeFailure) as caught:
        http.request("GET", "/x")
    assert caught.value.category is Category.AUTH and http.requests_made == 1


def test_post_5xx_is_not_retried_but_429_is() -> None:
    transport = ScriptedTransport(Response(500, {}, b""))
    http, _ = client(transport)
    with pytest.raises(RuntimeFailure) as caught:
        http.request("POST", "/x", body={})
    assert caught.value.category is Category.SERVER and http.requests_made == 1
    transport = ScriptedTransport(Response(429, {}, b""), ok({"id": 1}, 201))
    http, _ = client(transport)
    assert http.request("POST", "/x", body={}) == {"id": 1}


def test_post_timeout_not_retried_connection_refused_is() -> None:
    http, _ = client(ScriptedTransport(TransportError("timeout", sent=True)))
    with pytest.raises(RuntimeFailure) as caught:
        http.request("POST", "/x", body={})
    assert caught.value.category is Category.TIMEOUT and http.requests_made == 1
    http, _ = client(ScriptedTransport(TransportError("connection", sent=False), ok({})))
    assert http.request("POST", "/x", body={}) == {}


def test_get_timeout_retried_then_categorised() -> None:
    http, _ = client(ScriptedTransport(*[TransportError("timeout", sent=True)] * 4))
    with pytest.raises(RuntimeFailure) as caught:
        http.request("GET", "/x")
    assert caught.value.category is Category.TIMEOUT and http.requests_made == 4


def test_malformed_json_retried_for_get() -> None:
    http, _ = client(ScriptedTransport(Response(200, {}, b'{"a": '), ok({"a": 1})))
    assert http.request("GET", "/x") == {"a": 1}


def test_malformed_json_persistent_is_categorised() -> None:
    http, _ = client(ScriptedTransport(*[Response(200, {}, b"{")] * 4))
    with pytest.raises(RuntimeFailure) as caught:
        http.request("GET", "/x")
    assert caught.value.category is Category.MALFORMED_RESPONSE


def test_validation_error_names_fields() -> None:
    body = json.dumps({"detail": [{"loc": ["body", "email"], "msg": "bad"}]}).encode()
    http, _ = client(ScriptedTransport(Response(422, {}, body)))
    with pytest.raises(RuntimeFailure) as caught:
        http.request("POST", "/x", body={})
    assert caught.value.category is Category.VALIDATION and caught.value.fields == ("email",)


def test_empty_body_is_none_and_errors_carry_status_not_body() -> None:
    http, _ = client(ScriptedTransport(Response(204, {}, b"")))
    assert http.request("DELETE", "/x") is None
    http, _ = client(ScriptedTransport(Response(409, {}, b"secret-token-in-body")))
    with pytest.raises(RuntimeFailure) as caught:
        http.request("POST", "/x", body={})
    assert caught.value.category is Category.CONFLICT and "secret" not in str(caught.value)


def test_parse_retry_after() -> None:
    assert parse_retry_after("2") == 2.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert parse_retry_after("-1") is None
