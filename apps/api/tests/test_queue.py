import json
import uuid

import pytest
import redis

from apps.api.app.queue import DocumentProcessingQueue, QueueOperationError


class FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.lists: dict[str, list[str | bytes]] = {}

    def rpush(self, name: str, *values: str) -> int:
        self.calls.extend((name, value) for value in values)
        self.lists.setdefault(name, []).extend(values)
        return len(self.lists[name])

    def lmove(self, source: str, destination: str, wherefrom: str, whereto: str):
        if not self.lists.get(source):
            return None
        value = self.lists[source].pop(0)
        self.lists.setdefault(destination, []).append(value)
        return value

    def lrem(self, name: str, count: int, value: str | bytes) -> int:
        try:
            self.lists.setdefault(name, []).remove(value)
        except ValueError:
            return 0
        return 1

    def lrange(self, name: str, start: int, end: int):
        return list(self.lists.get(name, []))

    def rpoplpush(self, source: str, destination: str):
        if not self.lists.get(source):
            return None
        value = self.lists[source].pop()
        self.lists.setdefault(destination, []).insert(0, value)
        return value

    def lpos(self, name: str, value: str):
        try:
            return self.lists.get(name, []).index(value)
        except ValueError:
            return None

    def execute_command(self, *args: str | int | bytes):
        inflight, queue, payload = str(args[3]), str(args[4]), args[5]
        assert isinstance(payload, (str, bytes))
        removed = self.lrem(inflight, 1, payload)
        if removed:
            self.rpush(queue, payload.decode() if isinstance(payload, bytes) else payload)
        return removed


class FailingRedis:
    def rpush(self, name: str, *values: str) -> int:
        raise redis.ConnectionError("Redis unavailable")

    def lmove(self, source: str, destination: str, wherefrom: str, whereto: str):
        raise redis.ConnectionError("Redis unavailable")

    def lrem(self, name: str, count: int, value: str | bytes) -> int:
        raise redis.ConnectionError("Redis unavailable")

    def lrange(self, name: str, start: int, end: int):
        raise redis.ConnectionError("Redis unavailable")

    def rpoplpush(self, source: str, destination: str):
        raise redis.ConnectionError("Redis unavailable")

    def lpos(self, name: str, value: str):
        raise redis.ConnectionError("Redis unavailable")

    def execute_command(self, *args: str | int | bytes):
        raise redis.ConnectionError("Redis unavailable")


def test_enqueue_document_pushes_minimal_json_payload() -> None:
    client = FakeRedis()
    document_id = uuid.uuid4()
    queue = DocumentProcessingQueue(client=client, queue_name="documents")

    queue.enqueue(document_id)

    assert len(client.calls) == 1
    queue_name, payload = client.calls[0]
    assert queue_name == "documents"
    assert json.loads(payload) == {"document_id": str(document_id)}


def test_enqueue_document_translates_redis_failure() -> None:
    queue = DocumentProcessingQueue(client=FailingRedis(), queue_name="documents")

    with pytest.raises(QueueOperationError, match="Unable to enqueue"):
        queue.enqueue(uuid.uuid4())


def test_job_is_held_inflight_until_acknowledged() -> None:
    client = FakeRedis()
    queue = DocumentProcessingQueue(client=client, queue_name="documents")
    document_id = uuid.uuid4()
    queue.enqueue(document_id)

    payload = queue.acquire()

    assert payload is not None
    assert client.lists["documents"] == []
    assert client.lists["documents:inflight"] == [payload]
    queue.acknowledge(payload)
    assert client.lists["documents:inflight"] == []


def test_abandoned_inflight_job_is_recovered() -> None:
    client = FakeRedis()
    queue = DocumentProcessingQueue(client=client, queue_name="documents")
    queue.enqueue(uuid.uuid4())
    payload = queue.acquire()

    assert payload is not None
    assert queue.recover_inflight() == 1
    assert client.lists["documents:inflight"] == []
    assert client.lists["documents"] == [payload]


def test_reconciliation_does_not_duplicate_visible_job() -> None:
    client = FakeRedis()
    queue = DocumentProcessingQueue(client=client, queue_name="documents")
    document_id = uuid.uuid4()

    assert queue.enqueue_if_missing(document_id) is True
    assert queue.enqueue_if_missing(document_id) is False
    assert len(client.lists["documents"]) == 1
