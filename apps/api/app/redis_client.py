import redis

from apps.api.app.settings import settings


def get_redis_client() -> redis.Redis:
    return redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
