import pytest

from apps.api.app import worker_health


def test_worker_health_checks_ocr_database_and_redis_without_queue_mutation(monkeypatch):
    calls = []

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query):
            calls.append(("database", query))

    class Redis:
        def ping(self):
            calls.append(("redis", "ping"))

        def close(self):
            calls.append(("redis", "close"))

    monkeypatch.setattr(worker_health.psycopg, "connect", lambda *_args, **_kwargs: Database())
    monkeypatch.setattr(
        worker_health.redis.Redis,
        "from_url",
        staticmethod(lambda *_args, **_kwargs: Redis()),
    )
    monkeypatch.setattr(worker_health.shutil, "which", lambda _path: "tesseract")

    worker_health.check_worker_dependencies()

    assert calls == [("database", "SELECT 1"), ("redis", "ping"), ("redis", "close")]


def test_worker_health_fails_when_dependencies_or_ocr_are_unavailable(monkeypatch):
    monkeypatch.setattr(worker_health.shutil, "which", lambda _path: None)
    with pytest.raises(RuntimeError, match="OCR"):
        worker_health.check_worker_dependencies()

    monkeypatch.setattr(worker_health.shutil, "which", lambda _path: "tesseract")

    def fail_connect(*_args, **_kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr(worker_health.psycopg, "connect", fail_connect)
    with pytest.raises(RuntimeError, match="Database dependency"):
        worker_health.check_worker_dependencies()


def test_worker_health_main_returns_nonzero_on_failure(monkeypatch):
    monkeypatch.setattr(
        worker_health,
        "check_worker_dependencies",
        lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    assert worker_health.main() == 1


def test_worker_health_fails_when_redis_is_unavailable_and_closes_client(monkeypatch):
    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _query):
            return None

    class Redis:
        closed = False

        def ping(self):
            raise OSError("broker unavailable")

        def close(self):
            self.closed = True

    client = Redis()
    monkeypatch.setattr(worker_health.psycopg, "connect", lambda *_args, **_kwargs: Database())
    monkeypatch.setattr(
        worker_health.redis.Redis, "from_url", staticmethod(lambda *_args, **_kwargs: client)
    )
    monkeypatch.setattr(worker_health.shutil, "which", lambda _path: "tesseract")
    with pytest.raises(RuntimeError, match="Redis dependency"):
        worker_health.check_worker_dependencies()
    assert client.closed
