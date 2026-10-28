from fastapi.middleware.cors import CORSMiddleware
from fastapi import Depends, FastAPI, Header, HTTPException
import hmac
import ipaddress
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import BaseModel, Field


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
    reject_post,
)

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
        "Marketing campaign drafts and human approval workflow. "
        "This version does not publish to social media."
    ),
    version="0.1.0",
    docs_url=None if APP_ENV == "production" else "/docs",
    redoc_url=None if APP_ENV == "production" else "/redoc",
    openapi_url=None if APP_ENV == "production" else "/openapi.json",
    lifespan=lifespan,
)


def require_auth(authorization: str | None = Header(default=None)) -> None:
    try:
        validate_runtime_config()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Production configuration is invalid.") from exc
    secret = os.getenv("MARKETING_AUTH_SECRET", "")
    if len(secret.encode("utf-8")) < 32:
        raise HTTPException(status_code=503, detail="Authentication is not configured.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required.")
    supplied = authorization[7:]
    if (
        not supplied
        or len(supplied) > 4096
        or not hmac.compare_digest(supplied.encode("utf-8"), secret.encode("utf-8"))
    ):
        raise HTTPException(status_code=401, detail="Invalid or expired session.")


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
                    await self._reject(send)
                    return
            except ValueError:
                await self._reject(send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_size:
                await self._reject(send)
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

    async def _reject(self, send):
        body = b'{"detail":"Request body too large."}'
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
)


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=1000)
    audience: str = Field(min_length=1, max_length=300)
    offer: str = Field(default="", max_length=500)
    call_to_action: str = Field(
        default="Contact us to learn more", max_length=300
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "marketing-agent"}


@app.post("/campaigns", status_code=201)
def create_campaign_endpoint(payload: CampaignCreate, _: None = Depends(require_auth)) -> dict:
    try:
        return create_campaign(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/campaigns")
def list_campaigns_endpoint(_: None = Depends(require_auth)) -> list[dict]:
    return list_campaigns()


@app.get("/campaigns/{campaign_id}")
def get_campaign_endpoint(campaign_id: str, _: None = Depends(require_auth)) -> dict:
    campaign = get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    return campaign


@app.post("/campaigns/{campaign_id}/drafts", status_code=201)
def generate_drafts_endpoint(campaign_id: str, _: None = Depends(require_auth)) -> list[dict]:
    try:
        return generate_drafts(campaign_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/posts")
def list_posts_endpoint(campaign_id: str | None = None, _: None = Depends(require_auth)) -> list[dict]:
    return list_posts(campaign_id)


@app.post("/posts/{post_id}/approve")
def approve_post_endpoint(post_id: str, _: None = Depends(require_auth)) -> dict:
    try:
        return approve_post(post_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/posts/{post_id}/publish/facebook")
def publish_facebook_post_endpoint(post_id: str, _: None = Depends(require_auth)) -> dict:
    try:
        return publish_facebook_post(post_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FacebookConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FacebookPublishError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/posts/{post_id}/reject")
def reject_post_endpoint(post_id: str, _: None = Depends(require_auth)) -> dict:
    try:
        return reject_post(post_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
