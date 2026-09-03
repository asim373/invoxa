import logging
from contextlib import asynccontextmanager

import psycopg
import redis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from apps.api.app.auth import router as auth_router
from apps.api.app.documents import router as documents_router
from apps.api.app.exports import router as exports_router
from apps.api.app.logging import configure_logging
from apps.api.app.settings import settings

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
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)
app.include_router(documents_router)
app.include_router(auth_router)
app.include_router(exports_router)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    logger.info("health_check_passed")
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
def readiness() -> JSONResponse:
    dependencies = {"database": "ok", "redis": "ok"}

    try:
        with psycopg.connect(settings.database_url, connect_timeout=2) as connection:
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
