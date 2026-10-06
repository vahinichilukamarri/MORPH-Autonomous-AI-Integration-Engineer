import os
import secrets
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException

from common.faults import install_faults
from support.models import ErrorBody, ErrorDetail, ErrorResponse, User
from support.store import UserStore

DEFAULT_TOKEN = "support-dev-token"
V2_RENAMES = {"tier": "serviceTier"}
_bearer = HTTPBearer(auto_error=False, description="Support API bearer token.")

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid bearer token."},
    422: {"model": ErrorResponse, "description": "Request failed strict validation."},
}


def _error(
    status: int, code: str, message: str, details: list[ErrorDetail] | None = None
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details or []))
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def _expected(error: Any) -> str:
    ctx = error.get("ctx") or {}
    if "expected" in ctx:
        return f"one of {ctx['expected']}"
    if "pattern" in ctx:
        return f"a string matching pattern {ctx['pattern']}"
    return str(error["msg"]).removeprefix("Value error, ")


def _validation_details(exc: RequestValidationError) -> list[ErrorDetail]:
    details: list[ErrorDetail] = []
    for err in exc.errors():
        loc = [str(part) for part in err["loc"]]
        location = loc[0] if loc else "body"
        field = ".".join(loc[1:]) if len(loc) > 1 else location
        received = None if err["type"] == "missing" else err.get("input")
        if isinstance(received, dict | list):
            received = None
        details.append(
            ErrorDetail(location=location, field=field, expected=_expected(err), received=received)
        )
    return details


def create_app(token: str | None = None, admin_token: str | None = None) -> FastAPI:
    expected_token = token or os.environ.get("SUPPORT_TOKEN", DEFAULT_TOKEN)
    store = UserStore()

    def require_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
    ) -> None:
        if credentials is None or not secrets.compare_digest(
            credentials.credentials, expected_token
        ):
            raise StarletteHTTPException(401, "Missing or invalid bearer token.")

    app = FastAPI(
        title="Mock Support",
        version="1.0.0",
        description="Mock support desk. camelCase fields, numeric user ids, epoch timestamps.",
        dependencies=[Depends(require_token)],
    )
    app.state.store = store

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(
            422, "VALIDATION_ERROR", "Request failed validation.", _validation_details(exc)
        )

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {401: "UNAUTHORIZED", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        return _error(exc.status_code, codes.get(exc.status_code, "ERROR"), str(exc.detail))

    @app.get(
        "/users/{userId}",
        response_model=User,
        summary="Get a user",
        responses={
            **_ERROR_RESPONSES,
            404: {"model": ErrorResponse, "description": "Unknown user."},
        },
    )
    def get_user(userId: int) -> User:
        user = store.get(userId)
        if user is None:
            raise StarletteHTTPException(404, "User not found.")
        return user

    @app.post(
        "/users",
        response_model=User,
        status_code=201,
        summary="Create a user",
        responses={
            **_ERROR_RESPONSES,
            409: {"model": ErrorResponse, "description": "A user with this userId exists."},
        },
    )
    def create_user(body: User) -> User | JSONResponse:
        if not store.create(body):
            return _error(409, "DUPLICATE_USER", f"User {body.userId} already exists.")
        return body

    @app.put(
        "/users/{userId}",
        response_model=User,
        summary="Create or replace a user (upsert)",
        responses={**_ERROR_RESPONSES, 201: {"model": User, "description": "User created."}},
    )
    def put_user(userId: int, body: User, response: Response) -> User | JSONResponse:
        if body.userId != userId:
            detail = ErrorDetail(
                location="body",
                field="userId",
                expected=f"{userId} (the userId in the path)",
                received=body.userId,
            )
            return _error(422, "VALIDATION_ERROR", "Request failed validation.", [detail])
        if store.upsert(body):
            response.status_code = 201
        return body

    install_faults(
        app,
        admin_token=admin_token,
        renames=V2_RENAMES,
        reset_state=store.reset,
        error_body=lambda status, message: {
            "error": {"code": "INJECTED_FAULT", "message": message, "details": []}
        },
    )
    return app


app = create_app()
