"""RFC 9457 ``application/problem+json`` errors.

Error bodies never carry stack traces, SQL or exception text from unexpected
errors: unhandled exceptions become a generic 500 with only the request ID.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger("problems")

PROBLEM_JSON = "application/problem+json"

_TITLES = {
    400: ("/problems/bad-request", "Bad request"),
    401: ("/problems/unauthenticated", "Authentication required"),
    403: ("/problems/forbidden", "Forbidden"),
    404: ("/problems/not-found", "Not found"),
    405: ("/problems/method-not-allowed", "Method not allowed"),
    409: ("/problems/conflict", "Conflict"),
    413: ("/problems/payload-too-large", "Request body too large"),
    415: ("/problems/unsupported-media-type", "Unsupported media type"),
    422: ("/problems/validation", "Validation failed"),
    429: ("/problems/rate-limited", "Too many requests"),
    500: ("/problems/internal", "Internal server error"),
    502: ("/problems/upstream-unavailable", "Upstream unavailable"),
    503: ("/problems/unavailable", "Service unavailable"),
    504: ("/problems/upstream-unavailable", "Upstream timed out"),
}


class ProblemError(Exception):
    def __init__(
        self,
        status: int,
        type_: str | None = None,
        title: str | None = None,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        default_type, default_title = _TITLES.get(status, ("about:blank", "Error"))
        self.status = status
        self.type = type_ or default_type
        self.title = title or default_title
        self.detail = detail
        self.headers = headers or {}
        self.extra = extra
        super().__init__(f"{status} {self.type}: {detail}")


def unauthenticated(detail: str = "Missing, expired or invalid token.") -> ProblemError:
    return ProblemError(401, "/problems/unauthenticated", "Unauthenticated", detail, headers={"WWW-Authenticate": "Bearer"})


def forbidden(detail: str) -> ProblemError:
    return ProblemError(403, "/problems/forbidden", "Forbidden", detail)


def not_found(detail: str = "Resource not found.") -> ProblemError:
    return ProblemError(404, "/problems/not-found", "Not found", detail)


def invalid_state(title: str, detail: str) -> ProblemError:
    return ProblemError(409, "/problems/invalid-state", title, detail)


def validation(detail: str, errors: list[dict[str, Any]] | None = None) -> ProblemError:
    return ProblemError(422, "/problems/validation", "Validation failed", detail, errors=errors or [])


def request_id_of(request: Request) -> str | None:
    rid = getattr(request.state, "request_id", None)
    return rid or request.headers.get("x-request-id")


def problem_body(
    status: int,
    type_: str,
    title: str,
    detail: str | None,
    instance: str | None,
    request_id: str | None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"type": type_, "title": title, "status": status}
    if detail is not None:
        body["detail"] = detail
    if instance is not None:
        body["instance"] = instance
    body["request_id"] = request_id
    body.update(extra)
    return body


def problem_response(
    request: Request | None,
    status: int,
    type_: str | None = None,
    title: str | None = None,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    default_type, default_title = _TITLES.get(status, ("about:blank", "Error"))
    body = problem_body(
        status,
        type_ or default_type,
        title or default_title,
        detail,
        request.url.path if request is not None else None,
        request_id_of(request) if request is not None else None,
        **extra,
    )
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def _clean_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep location, message and type; drop echoed input and internals."""
    out = []
    for e in errors:
        loc = [str(p) for p in e.get("loc", ())]
        out.append({"loc": loc, "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))})
    return out


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(request, exc.status, exc.type, exc.title, exc.detail, headers=exc.headers, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            request,
            422,
            "/problems/validation",
            "Validation failed",
            "The request did not pass validation.",
            errors=_clean_validation_errors(list(exc.errors())),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else None
        return problem_response(request, exc.status_code, detail=detail, headers=getattr(exc, "headers", None))

    @app.exception_handler(asyncpg.DataError)
    async def _bad_data(request: Request, exc: asyncpg.DataError) -> JSONResponse:
        # e.g. a NUL character in a text field: the database refuses it, so it is the caller's input
        # that is wrong, not the server. The driver's message is not echoed back.
        return problem_response(
            request, 422, "/problems/validation", "Validation failed", "The request contains characters that cannot be stored."
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception", path=request.url.path, error_type=type(exc).__name__, exc_info=exc)
        return problem_response(request, 500, detail="An unexpected error occurred.")
