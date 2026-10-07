"""Controllable, deterministic failure injection shared by both mock systems.

A fault profile is set through the admin API (mounted at ``/__admin`` with its own token, so it
is not part of the public OpenAPI spec). Every non-admin request draws exactly one number from a
generator seeded by the profile, so the same profile and the same request sequence always
produce the same faults.
"""

import asyncio
import copy
import json
import os
import random
import secrets
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_ADMIN_TOKEN = "admin-dev-token"
ADMIN_PREFIX = "/__admin"
_EXEMPT_PATHS = ("/openapi.json", "/docs", "/redoc")
_BODY_METHODS = {"POST", "PUT", "PATCH"}

ErrorBodyFactory = Callable[[int, str], dict[str, Any]]
MAX_LOGGED_REQUESTS = 20_000


class FaultProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int = Field(default=0, description="Seed for the per-request fault generator.")
    http_500_rate: float = Field(default=0.0, ge=0, le=1, description="Share of requests -> 500.")
    http_429_rate: float = Field(default=0.0, ge=0, le=1, description="Share of requests -> 429.")
    retry_after_seconds: int = Field(default=1, ge=0, description="Retry-After on 429 responses.")
    malformed_json_rate: float = Field(
        default=0.0, ge=0, le=1, description="Share of requests whose response body is truncated."
    )
    latency_ms: int = Field(default=0, ge=0, le=60_000, description="Fixed delay on every request.")
    timeout: bool = Field(default=False, description="Hold every request for timeout_seconds.")
    timeout_seconds: float = Field(default=30.0, ge=0, le=300, description="Hold time for timeout.")
    drop_fields: list[str] = Field(
        default_factory=list, description="JSON keys removed from successful responses."
    )
    contract_version: Literal["v1", "v2"] = Field(
        default="v1", description="Contract served by the system (v2 renames fields)."
    )

    @model_validator(mode="after")
    def _rates_fit_in_one_draw(self) -> "FaultProfile":
        if self.http_500_rate + self.http_429_rate + self.malformed_json_rate > 1:
            raise ValueError("http_500_rate + http_429_rate + malformed_json_rate must be <= 1")
        return self


Outcome = Literal["ok", "http_500", "http_429", "malformed_json"]


@dataclass(frozen=True)
class Decision:
    profile: FaultProfile
    outcome: Outcome


class FaultController:
    def __init__(self) -> None:
        self.profile = FaultProfile()
        self._rng = random.Random(self.profile.seed)

    def set_profile(self, profile: FaultProfile) -> None:
        self.profile = profile
        self._rng = random.Random(profile.seed)

    def clear(self) -> None:
        self.set_profile(FaultProfile())

    def decide(self) -> Decision:
        draw = self._rng.random()
        p = self.profile
        outcome: Outcome = "ok"
        if draw < p.http_500_rate:
            outcome = "http_500"
        elif draw < p.http_500_rate + p.http_429_rate:
            outcome = "http_429"
        elif draw < p.http_500_rate + p.http_429_rate + p.malformed_json_rate:
            outcome = "malformed_json"
        return Decision(profile=p, outcome=outcome)


def _rename(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            mapping.get(k, k): (
                mapping.get(v, v) if k == "field" and isinstance(v, str) else _rename(v, mapping)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_rename(item, mapping) for item in value]
    return value


def _drop(value: Any, names: set[str]) -> Any:
    if isinstance(value, dict):
        return {k: _drop(v, names) for k, v in value.items() if k not in names}
    if isinstance(value, list):
        return [_drop(item, names) for item in value]
    return value


def _find_keys(value: Any, names: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in names:
                found.append(key)
            found.extend(_find_keys(item, names))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_keys(item, names))
    return found


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _replay(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _is_json(headers: list[tuple[bytes, bytes]]) -> bool:
    return any(k.lower() == b"content-type" and b"json" in v.lower() for k, v in headers)


def _with_length(headers: list[tuple[bytes, bytes]], length: int) -> list[tuple[bytes, bytes]]:
    kept = [(k, v) for k, v in headers if k.lower() != b"content-length"]
    return [*kept, (b"content-length", str(length).encode())]


async def _json_response(
    send: Send, status: int, body: dict[str, Any], extra: list[tuple[bytes, bytes]] | None = None
) -> None:
    payload = json.dumps(body).encode()
    headers = _with_length([(b"content-type", b"application/json"), *(extra or [])], len(payload))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


class FaultMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        controller: FaultController,
        renames: dict[str, str],
        error_body: ErrorBodyFactory,
    ) -> None:
        self.app = app
        self.controller = controller
        self.v1_to_v2 = renames
        self.v2_to_v1 = {v: k for k, v in renames.items()}
        self.error_body = error_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] != "http" or path.startswith(ADMIN_PREFIX) or path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        decision = self.controller.decide()
        profile = decision.profile
        if profile.latency_ms:
            await asyncio.sleep(profile.latency_ms / 1000)
        if profile.timeout:
            await asyncio.sleep(profile.timeout_seconds)
            await _json_response(send, 504, self.error_body(504, "Gateway timeout."))
            return
        if decision.outcome == "http_500":
            await _json_response(send, 500, self.error_body(500, "Injected server error."))
            return
        if decision.outcome == "http_429":
            retry = [(b"retry-after", str(profile.retry_after_seconds).encode())]
            await _json_response(send, 429, self.error_body(429, "Injected rate limit."), retry)
            return

        v2 = profile.contract_version == "v2"
        downstream_receive = receive
        if v2 and scope["method"] in _BODY_METHODS:
            body = await _read_body(receive)
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            retired = _find_keys(parsed, set(self.v1_to_v2))
            if retired:
                old = retired[0]
                message = f"Field '{old}' was renamed to '{self.v1_to_v2[old]}' in contract v2."
                await _json_response(send, 422, self.error_body(422, message))
                return
            if parsed is not None:
                body = json.dumps(_rename(parsed, self.v2_to_v1)).encode()
            downstream_receive = _replay(body)
        await self.app(scope, downstream_receive, self._rewrite_response(send, decision, v2=v2))

    def _rewrite_response(self, send: Send, decision: Decision, *, v2: bool) -> Send:
        profile = decision.profile
        start: MutableMapping[str, Any] = {}
        chunks: list[bytes] = []

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            headers: list[tuple[bytes, bytes]] = list(start["headers"])
            body = b"".join(chunks)
            status = int(start["status"])
            if _is_json(headers) and body:
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = None
                else:
                    if v2:
                        parsed = _rename(parsed, self.v1_to_v2)
                    if profile.drop_fields and status < 300:
                        parsed = _drop(parsed, set(profile.drop_fields))
                    body = json.dumps(parsed).encode()
            if decision.outcome == "malformed_json":
                body = body[: max(1, len(body) // 2)]
            await send(
                {
                    **start,
                    "headers": _with_length(headers, len(body)),
                    "type": "http.response.start",
                }
            )
            await send({"type": "http.response.body", "body": body})

        return wrapped


def _rename_schema(schema: dict[str, Any], mapping: dict[str, str]) -> None:
    for component in schema.get("components", {}).get("schemas", {}).values():
        props = component.get("properties")
        if props:
            component["properties"] = {mapping.get(k, k): v for k, v in props.items()}
        if "required" in component:
            component["required"] = [mapping.get(k, k) for k in component["required"]]


class RequestLogEntry(BaseModel):
    seq: int
    method: str
    path: str
    query: str
    status: int
    auth_header: Literal["x-api-key", "authorization", "none"]


class RequestLog:
    """Every non-admin request seen by the system, for black-box verification by the oracle.

    Header values are never recorded, only which authentication header was present.
    """

    def __init__(self) -> None:
        self.entries: list[RequestLogEntry] = []
        self._seq = 0

    def clear(self) -> None:
        self.entries = []

    def add(self, entry: dict[str, Any]) -> None:
        self._seq += 1
        self.entries.append(RequestLogEntry(seq=self._seq, **entry))
        del self.entries[:-MAX_LOGGED_REQUESTS]


class RequestLogMiddleware:
    """Outermost middleware, so injected faults and authentication failures are logged too."""

    def __init__(self, app: ASGIApp, log: RequestLog) -> None:
        self.app = app
        self.log = log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] != "http" or path.startswith(ADMIN_PREFIX):
            await self.app(scope, receive, send)
            return
        names = {k.lower() for k, _ in scope.get("headers", [])}
        auth: Literal["x-api-key", "authorization", "none"] = "none"
        if b"x-api-key" in names:
            auth = "x-api-key"
        elif b"authorization" in names:
            auth = "authorization"
        status = 0

        async def recording(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, recording)
        finally:
            self.log.add(
                {
                    "method": str(scope["method"]),
                    "path": path,
                    "query": scope.get("query_string", b"").decode("latin-1"),
                    "status": status,
                    "auth_header": auth,
                }
            )


class StateDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[dict[str, Any]]


def _build_admin_app(
    controller: FaultController,
    admin_token: str,
    reset_state: Callable[[], None],
    log: RequestLog,
    dump_state: Callable[[], list[dict[str, Any]]],
    load_state: Callable[[list[dict[str, Any]]], None],
) -> FastAPI:
    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        if x_admin_token is None or not secrets.compare_digest(x_admin_token, admin_token):
            raise HTTPException(status_code=401, detail="Missing or invalid X-Admin-Token.")

    admin = FastAPI(title="Failure injection admin", dependencies=[Depends(require_admin)])

    @admin.get("/faults")
    def get_faults() -> FaultProfile:
        return controller.profile

    @admin.put("/faults")
    def put_faults(profile: FaultProfile) -> FaultProfile:
        controller.set_profile(profile)
        return controller.profile

    @admin.delete("/faults")
    def delete_faults() -> FaultProfile:
        controller.clear()
        return controller.profile

    @admin.post("/reset")
    def reset() -> FaultProfile:
        reset_state()
        controller.clear()
        log.clear()
        return controller.profile

    @admin.get("/state")
    def get_state() -> StateDocument:
        return StateDocument(records=dump_state())

    @admin.put("/state")
    def put_state(document: StateDocument) -> StateDocument:
        try:
            load_state(document.records)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return StateDocument(records=dump_state())

    @admin.get("/requests")
    def get_requests() -> list[RequestLogEntry]:
        return log.entries

    @admin.delete("/requests")
    def delete_requests() -> dict[str, int]:
        log.clear()
        return {"entries": 0}

    return admin


def install_faults(
    app: FastAPI,
    *,
    admin_token: str | None,
    renames: dict[str, str],
    reset_state: Callable[[], None],
    dump_state: Callable[[], list[dict[str, Any]]],
    load_state: Callable[[list[dict[str, Any]]], None],
    error_body: ErrorBodyFactory,
) -> FaultController:
    """Attach failure injection to a mock system.

    ``renames`` maps v1 field names to their v2 names; it is applied to requests, responses and
    the OpenAPI document while the profile's ``contract_version`` is ``v2``.
    """
    controller = FaultController()
    app.state.faults = controller
    token = admin_token or os.environ.get("ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN)
    app.add_middleware(
        FaultMiddleware, controller=controller, renames=renames, error_body=error_body
    )
    log = RequestLog()
    app.state.request_log = log
    app.add_middleware(RequestLogMiddleware, log=log)
    app.mount(
        ADMIN_PREFIX,
        _build_admin_app(controller, token, reset_state, log, dump_state, load_state),
    )

    base_openapi: Callable[[], dict[str, Any]] = app.openapi

    def versioned_openapi() -> dict[str, Any]:
        schema = copy.deepcopy(base_openapi())
        if controller.profile.contract_version == "v2":
            _rename_schema(schema, renames)
            schema["info"]["version"] = "2.0.0"
        return schema

    app.openapi = versioned_openapi  # type: ignore[method-assign]
    return controller
