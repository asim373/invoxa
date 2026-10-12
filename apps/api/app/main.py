import logging
from contextlib import asynccontextmanager

import psycopg
import redis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from apps.api.app.account_lifecycle import router as lifecycle_router
from apps.api.app.api_access import external_router, management_router
from apps.api.app.auth import router as auth_router
from apps.api.app.customer_account import router as account_router
from apps.api.app.documents import router as documents_router
from apps.api.app.email_delivery_api import router as email_delivery_router
from apps.api.app.email_verification import router as verification_router
from apps.api.app.exports import router as exports_router
from apps.api.app.google_auth import router as google_auth_router
from apps.api.app.insights import router as insights_router
from apps.api.app.logging import configure_logging
from apps.api.app.organization_settings import router as organization_settings_router
from apps.api.app.saml_sso import management_router as saml_management_router
from apps.api.app.saml_sso import router as saml_router
from apps.api.app.scim import management_router as scim_management_router
from apps.api.app.scim import scim_router
from apps.api.app.security import router as security_router
from apps.api.app.settings import settings
from apps.api.app.team import router as team_router
from apps.api.app.usage_api import router as usage_router

configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("application_started")
    yield


app = FastAPI(title="Document & Invoice Analyzer API", version="0.1.0", lifespan=lifespan)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)
app.include_router(documents_router)
app.include_router(auth_router)
app.include_router(account_router)
app.include_router(lifecycle_router)
app.include_router(verification_router)
app.include_router(email_delivery_router)
app.include_router(google_auth_router)
app.include_router(exports_router)
app.include_router(insights_router)
app.include_router(team_router)
app.include_router(usage_router)
app.include_router(organization_settings_router)
app.include_router(management_router)
app.include_router(external_router)
app.include_router(saml_management_router)
app.include_router(saml_router)
app.include_router(scim_management_router)
app.include_router(scim_router)
app.include_router(security_router)


@app.exception_handler(StarletteHTTPException)
async def scim_http_error_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/scim/v2/"):
        detail = exc.detail if isinstance(exc.detail, str) else "SCIM request failed."
        payload = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
            "status": str(exc.status_code),
            "detail": detail,
        }
        headers = dict(exc.headers or {})
        return JSONResponse(
            status_code=exc.status_code,
            content=payload,
            headers=headers,
            media_type="application/scim+json",
        )
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
    )


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    if settings.is_production_like:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    logger.info("health_check_passed")
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
def readiness() -> JSONResponse:
    dependencies = {"database": "ok", "redis": "ok"}

    try:
        with psycopg.connect(settings.psycopg_database_url, connect_timeout=2) as connection:
            connection.execute("SELECT 1")
    except Exception:
        dependencies["database"] = "unavailable"
        logger.exception("readiness_database_unavailable")

    try:
        redis_client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        redis_client.ping()
        redis_client.close()
    except Exception:
        dependencies["redis"] = "unavailable"
        logger.exception("readiness_redis_unavailable")

    if any(status != "ok" for status in dependencies.values()):
        logger.warning("readiness_check_failed")
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "dependencies": dependencies},
        )

    logger.info("readiness_check_passed")
    return JSONResponse(status_code=200, content={"status": "ready", "dependencies": dependencies})
