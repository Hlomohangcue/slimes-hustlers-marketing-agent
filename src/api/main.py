from fastapi.middleware.cors import CORSMiddleware
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import hmac
import ipaddress
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException


from src.marketing.service import (
    approve_post,
    create_campaign,
    generate_drafts,
    get_campaign,
    list_campaigns,
    list_posts,
    publish_facebook_post,
    FacebookConfigurationError,
    FacebookPublishError,
    DraftEditConflictError,
    DraftEditNotAllowedError,
    ReviewPersistenceError,
    reject_post,
    update_post_draft,
)

logger = logging.getLogger(__name__)
HTTP_ERROR_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "REQUEST_TOO_LARGE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_SERVER_ERROR",
    502: "UPSTREAM_ERROR",
    503: "SERVICE_UNAVAILABLE",
}
DEVELOPMENT_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
APP_ENV = os.getenv("APP_ENV", "").strip().lower()
if APP_ENV not in {"development", "production"}:
    raise RuntimeError("APP_ENV must be explicitly set to development or production.")


def _production_origins(value: str | None) -> list[str]:
    if value is None:
        raise RuntimeError("CORS_ALLOWED_ORIGINS is required in production.")
    configured_origins = [origin.strip() for origin in value.split(",")]
    if not configured_origins or any(not origin for origin in configured_origins):
        raise RuntimeError("CORS_ALLOWED_ORIGINS must list exact HTTPS origins in production.")

    origins = []
    if any(origin == "*" for origin in configured_origins):
        raise RuntimeError("CORS_ALLOWED_ORIGINS must list exact origins in production.")
    for origin in configured_origins:
        try:
            parsed = urlsplit(origin)
            host = parsed.hostname.lower() if parsed.hostname else ""
            try:
                ip = ipaddress.ip_address(host)
                local_address = ip.is_loopback or ip.is_unspecified
                canonical_host = ip.compressed
            except ValueError:
                local_address = host == "localhost" or host.endswith(".localhost")
                if host.endswith("."):
                    raise ValueError("Trailing-dot hosts are not canonical origins.")
                canonical_host = host.encode("idna").decode("ascii").lower()
                if any(
                    not label
                    or len(label) > 63
                    or label[0] == "-"
                    or label[-1] == "-"
                    or any(not (char.isascii() and (char.isalnum() or char == "-")) for char in label)
                    for label in canonical_host.split(".")
                ):
                    raise ValueError("Invalid host in CORS origin.")
            port = parsed.port
            valid = (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and not parsed.netloc.endswith(":")
                and not local_address
                and parsed.username is None
                and parsed.password is None
                and parsed.path == ""
                and not parsed.query
                and not parsed.fragment
                and "?" not in origin
                and "#" not in origin
                and (port is None or 1 <= port <= 65535)
            )
            if valid:
                authority = f"[{canonical_host}]" if ":" in canonical_host else canonical_host
                if port is not None and port != 443:
                    authority = f"{authority}:{port}"
                origins.append(f"https://{authority}")
        except ValueError:
            valid = False
        if not valid:
            raise RuntimeError("Production CORS origins must be exact HTTPS origins.")
    return origins


def validate_runtime_config() -> None:
    app_env = os.getenv("APP_ENV", "").strip().lower()
    if app_env not in {"development", "production"}:
        raise RuntimeError("APP_ENV must be explicitly set to development or production.")
    if app_env != "production":
        return

    secret = os.getenv("MARKETING_AUTH_SECRET", "")
    if len(secret.encode("utf-8")) < 32:
        raise RuntimeError("MARKETING_AUTH_SECRET must be at least 32 bytes in production.")
    database_value = os.getenv("MARKETING_DB_PATH", "").strip()
    if not database_value:
        raise RuntimeError("MARKETING_DB_PATH is required in production.")
    database_path = Path(database_value)
    if not database_path.is_absolute():
        raise RuntimeError("MARKETING_DB_PATH must be an absolute path in production.")
    if (
        not database_path.parent.is_dir()
        or not os.access(database_path.parent, os.W_OK)
        or (database_path.exists() and not database_path.is_file())
    ):
        raise RuntimeError("MARKETING_DB_PATH must be in an existing writable directory.")
    _production_origins(os.getenv("CORS_ALLOWED_ORIGINS"))


@asynccontextmanager
async def lifespan(_app):
    validate_runtime_config()
    yield


app = FastAPI(
    title="Slimes.Hustlers Marketing Agent",
    description=(
        "Marketing campaign planning, content drafting, and human review. "
        "Facebook publishing is available only after approval. "
        "LinkedIn publishing is not currently supported."
    ),
    version="0.1.0",
    docs_url=None if APP_ENV == "production" else "/docs",
    redoc_url=None if APP_ENV == "production" else "/redoc",
    openapi_url=None if APP_ENV == "production" else "/openapi.json",
    lifespan=lifespan,
)


class APIError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code=status_code, detail=message)
        self.code = code


def _error_response(
    request_id: str,
    status_code: int,
    code: str,
    message: str,
    field_errors: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, object] = {"code": code, "message": message}
    if field_errors:
        error["field_errors"] = field_errors
    return JSONResponse(
        status_code=status_code,
        content={
            "error": error,
            "request_id": request_id,
            "detail": message,
        },
        headers=headers,
    )


def _request_id(request: Request) -> str:
    return request.state.request_id


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    code = getattr(
        exc,
        "code",
        HTTP_ERROR_CODES.get(exc.status_code, "HTTP_ERROR"),
    )
    message = exc.detail if isinstance(exc.detail, str) else "The request could not be completed."
    request.state.error_code = code
    return _error_response(
        _request_id(request),
        exc.status_code,
        code,
        message,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_exception(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    field_errors = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        field_errors.append({
            "field": location or "$",
            "code": str(error.get("type", "invalid_value")),
            "message": str(error.get("msg", "Invalid value.")),
        })
    request.state.error_code = "VALIDATION_ERROR"
    return _error_response(
        _request_id(request),
        422,
        "VALIDATION_ERROR",
        "Request validation failed.",
        field_errors=field_errors,
    )


class RequestCorrelationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid4())
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        response_status = 500
        response_started = False
        started_at = perf_counter()

        async def send_correlated(message):
            nonlocal response_status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                response_status = message["status"]
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_correlated)
        except Exception:
            state["error_code"] = "INTERNAL_SERVER_ERROR"
            response_status = 500
            if not response_started:
                response = _error_response(
                    request_id,
                    500,
                    "INTERNAL_SERVER_ERROR",
                    "An unexpected error occurred. Provide the request ID when contacting support.",
                )
                await response(scope, receive, send_correlated)
        finally:
            route = scope.get("route")
            operation = getattr(route, "path", "unmatched")
            error_code = state.get("error_code")
            if response_status >= 500:
                level = logging.ERROR
                error_code = error_code or "INTERNAL_SERVER_ERROR"
            elif response_status >= 400:
                level = logging.WARNING
            else:
                level = logging.INFO
            event = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "http_request",
                "request_id": request_id,
                "workspace_id": None,
                "operation": operation,
                "provider": "facebook" if operation.endswith("/publish/facebook") else None,
                "method": scope.get("method"),
                "status_code": response_status,
                "outcome": "failure" if response_status >= 400 else "success",
                "error_code": error_code,
                "duration_ms": round((perf_counter() - started_at) * 1000, 2),
            }
            logger.log(
                level,
                json.dumps(event, separators=(",", ":"), sort_keys=True),
            )


def require_auth(authorization: str | None = Header(default=None)) -> None:
    try:
        validate_runtime_config()
    except RuntimeError as exc:
        raise APIError(
            status_code=503,
            code="SERVER_CONFIGURATION_ERROR",
            message="Production configuration is invalid.",
        ) from exc
    secret = os.getenv("MARKETING_AUTH_SECRET", "")
    if len(secret.encode("utf-8")) < 32:
        raise APIError(
            status_code=503,
            code="AUTHENTICATION_UNAVAILABLE",
            message="Authentication is not configured.",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise APIError(
            status_code=401,
            code="AUTHENTICATION_REQUIRED",
            message="Authentication required.",
        )
    supplied = authorization[7:]
    if (
        not supplied
        or len(supplied) > 4096
        or not hmac.compare_digest(supplied.encode("utf-8"), secret.encode("utf-8"))
    ):
        raise APIError(
            status_code=401,
            code="AUTHENTICATION_INVALID",
            message="Invalid or expired session.",
        )


class RequestBodyLimitMiddleware:
    """Bound request buffering to max_body_size, including chunked bodies."""

    def __init__(self, app, max_body_size: int = 1_048_576):
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = next(
            (value for name, value in scope.get("headers", []) if name.lower() == b"content-length"),
            None,
        )
        if content_length is not None:
            try:
                if int(content_length) > self.max_body_size:
                    await self._reject(send, scope)
                    return
            except ValueError:
                await self._reject(send, scope)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_size:
                await self._reject(send, scope)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        body_bytes = bytes(body)
        body_sent = False

        async def bounded_receive():
            nonlocal body_sent
            if body_sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            body_sent = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        await self.app(scope, bounded_receive, send)

    async def _reject(self, send, scope):
        state = scope.setdefault("state", {})
        request_id = state.setdefault("request_id", str(uuid4()))
        state["error_code"] = "REQUEST_TOO_LARGE"
        body = json.dumps({
            "error": {
                "code": "REQUEST_TOO_LARGE",
                "message": "Request body too large.",
            },
            "request_id": request_id,
            "detail": "Request body too large.",
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


app.add_middleware(RequestBodyLimitMiddleware, max_body_size=1_048_576)
app.add_middleware(
    CORSMiddleware,
    allow_origins=(
        _production_origins(os.getenv("CORS_ALLOWED_ORIGINS"))
        if APP_ENV == "production"
        else DEVELOPMENT_ORIGINS
    ),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["Content-Type", "Authorization"],
    expose_headers=["X-Request-ID"],
)
app.add_middleware(RequestCorrelationMiddleware)


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=1000)
    audience: str = Field(min_length=1, max_length=300)
    offer: str = Field(default="", max_length=500)
    call_to_action: str = Field(
        default="Contact us to learn more", max_length=300
    )


class DraftUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=10_000, strict=True)
    version: int = Field(ge=1, strict=True)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "marketing-agent"}


@app.post("/campaigns", status_code=201)
def create_campaign_endpoint(payload: CampaignCreate, _: None = Depends(require_auth)) -> dict:
    try:
        return create_campaign(**payload.model_dump())
    except ValueError as exc:
        raise APIError(422, "CAMPAIGN_VALIDATION_ERROR", str(exc)) from exc


@app.get("/campaigns")
def list_campaigns_endpoint(_: None = Depends(require_auth)) -> list[dict]:
    return list_campaigns()


@app.get("/campaigns/{campaign_id}")
def get_campaign_endpoint(campaign_id: str, _: None = Depends(require_auth)) -> dict:
    campaign = get_campaign(campaign_id)
    if campaign is None:
        raise APIError(404, "CAMPAIGN_NOT_FOUND", "Campaign not found.")
    return campaign


@app.post("/campaigns/{campaign_id}/drafts", status_code=201)
def generate_drafts_endpoint(campaign_id: str, _: None = Depends(require_auth)) -> list[dict]:
    try:
        return generate_drafts(campaign_id)
    except LookupError as exc:
        raise APIError(404, "CAMPAIGN_NOT_FOUND", str(exc)) from exc


@app.get("/posts")
def list_posts_endpoint(campaign_id: str | None = None, _: None = Depends(require_auth)) -> list[dict]:
    return list_posts(campaign_id)


@app.patch("/posts/{post_id}/draft")
def update_post_draft_endpoint(
    post_id: str, payload: DraftUpdate, _: None = Depends(require_auth)
) -> dict:
    try:
        return update_post_draft(post_id, payload.content, payload.version)
    except LookupError as exc:
        raise APIError(404, "POST_NOT_FOUND", str(exc)) from exc
    except ValueError as exc:
        raise APIError(422, "DRAFT_VALIDATION_ERROR", str(exc)) from exc
    except (DraftEditConflictError, DraftEditNotAllowedError) as exc:
        raise APIError(409, "DRAFT_CONFLICT", str(exc)) from exc


@app.post("/posts/{post_id}/approve")
def approve_post_endpoint(post_id: str, _: None = Depends(require_auth)) -> dict:
    try:
        return approve_post(post_id)
    except LookupError as exc:
        raise APIError(404, "POST_NOT_FOUND", str(exc)) from exc
    except ValueError as exc:
        raise APIError(409, "REVIEW_CONFLICT", str(exc)) from exc
    except ReviewPersistenceError as exc:
        raise APIError(
            status_code=500,
            code="REVIEW_PERSISTENCE_FAILED",
            message="Review action could not be saved.",
        ) from exc


@app.post("/posts/{post_id}/publish/facebook")
def publish_facebook_post_endpoint(post_id: str, _: None = Depends(require_auth)) -> dict:
    try:
        return publish_facebook_post(post_id)
    except LookupError as exc:
        raise APIError(404, "POST_NOT_FOUND", str(exc)) from exc
    except ValueError as exc:
        raise APIError(409, "PUBLISH_CONFLICT", str(exc)) from exc
    except FacebookConfigurationError as exc:
        raise APIError(503, "FACEBOOK_CONFIGURATION_ERROR", str(exc)) from exc
    except FacebookPublishError as exc:
        raise APIError(502, "FACEBOOK_PUBLISH_ERROR", str(exc)) from exc


@app.post("/posts/{post_id}/reject")
def reject_post_endpoint(post_id: str, _: None = Depends(require_auth)) -> dict:
    try:
        return reject_post(post_id)
    except LookupError as exc:
        raise APIError(404, "POST_NOT_FOUND", str(exc)) from exc
    except ValueError as exc:
        raise APIError(409, "REVIEW_CONFLICT", str(exc)) from exc
    except ReviewPersistenceError as exc:
        raise APIError(
            status_code=500,
            code="REVIEW_PERSISTENCE_FAILED",
            message="Review action could not be saved.",
        ) from exc
