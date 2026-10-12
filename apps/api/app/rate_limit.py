import hashlib
import logging
from typing import Protocol, cast

import redis
from fastapi import HTTPException, Request, status

from apps.api.app.redis_client import get_redis_client
from apps.api.app.settings import settings

logger = logging.getLogger(__name__)


class RateLimitClient(Protocol):
    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int: ...


INCREMENT_WITH_EXPIRY = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


class RateLimiter:
    def __init__(self, client: RateLimitClient | None = None) -> None:
        self.client = client or cast(RateLimitClient, get_redis_client())

    def check(
        self,
        scope: str,
        identifier: str,
        limit: int,
        window_seconds: int,
        *,
        fail_closed: bool = False,
    ) -> None:
        if not settings.rate_limit_enabled:
            return
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        key = f"rate-limit:{scope}:{digest}"
        try:
            count = int(self.client.eval(INCREMENT_WITH_EXPIRY, 1, key, window_seconds))
        except redis.RedisError:
            # Sensitive auth, SAML, SCIM and administration callers opt into
            # fail-closed behavior. Upload and external read traffic retain the
            # documented availability-first policy during Redis outages.
            logger.exception("rate_limit_backend_unavailable scope=%s", scope)
            if fail_closed:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Request protection is temporarily unavailable.",
                    headers={"Retry-After": str(window_seconds)},
                ) from None
            return
        if count > limit:
            logger.warning("rate_limit_exceeded scope=%s", scope)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
                headers={"Retry-After": str(window_seconds)},
            )


def get_rate_limiter() -> RateLimiter:
    return RateLimiter()


def client_identifier(request: Request) -> str:
    return request.client.host if request.client else "unknown"
