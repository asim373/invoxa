"""Bounded, non-destructive worker dependency health check for Compose."""

import logging
import shutil
import sys

import psycopg
import redis

from apps.api.app.settings import settings

logger = logging.getLogger(__name__)


def check_worker_dependencies() -> None:
    executable = str(settings.tesseract_executable or "tesseract")
    if shutil.which(executable) is None:
        raise RuntimeError("OCR executable is unavailable")
    try:
        with psycopg.connect(settings.psycopg_database_url, connect_timeout=2) as connection:
            connection.execute("SELECT 1")
    except Exception:
        raise RuntimeError("Database dependency is unavailable") from None
    client = redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        client.ping()
    except Exception:
        raise RuntimeError("Redis dependency is unavailable") from None
    finally:
        client.close()


def main() -> int:
    try:
        check_worker_dependencies()
    except Exception as error:
        logger.error("worker_dependency_health_failed reason=%s", str(error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
