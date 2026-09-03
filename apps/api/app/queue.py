import json
import uuid
from typing import Protocol, cast

import redis

from apps.api.app.redis_client import get_redis_client
from apps.api.app.settings import settings


class QueueOperationError(RuntimeError):
    pass


class RedisQueueClient(Protocol):
    def rpush(self, name: str, *values: str) -> int: ...

    def lmove(
        self, source: str, destination: str, wherefrom: str, whereto: str
    ) -> str | bytes | None: ...

    def lrem(self, name: str, count: int, value: str | bytes) -> int: ...

    def lrange(self, name: str, start: int, end: int) -> list[str | bytes]: ...

    def rpoplpush(self, source: str, destination: str) -> str | bytes | None: ...

    def lpos(self, name: str, value: str) -> int | None: ...

    def execute_command(self, *args: str | int | bytes) -> object: ...


RELEASE_INFLIGHT_JOB = """
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed == 1 then redis.call('RPUSH', KEYS[2], ARGV[1]) end
return removed
"""


class DocumentProcessingQueue:
    def __init__(
        self,
        client: RedisQueueClient | None = None,
        queue_name: str | None = None,
    ) -> None:
        self.client = client or cast(RedisQueueClient, get_redis_client())
        self.queue_name = queue_name or settings.document_queue_name
        self.inflight_name = f"{self.queue_name}:inflight"

    @staticmethod
    def payload_for(document_id: uuid.UUID) -> str:
        return json.dumps({"document_id": str(document_id)}, separators=(",", ":"))

    def enqueue(self, document_id: uuid.UUID) -> None:
        payload = self.payload_for(document_id)
        try:
            self.client.rpush(self.queue_name, payload)
        except redis.RedisError as error:
            raise QueueOperationError("Unable to enqueue document for processing.") from error

    def enqueue_if_missing(self, document_id: uuid.UUID) -> bool:
        payload = self.payload_for(document_id)
        try:
            if self.client.lpos(self.queue_name, payload) is not None:
                return False
            if self.client.lpos(self.inflight_name, payload) is not None:
                return False
            self.client.rpush(self.queue_name, payload)
        except redis.RedisError as error:
            raise QueueOperationError("Unable to reconcile document processing job.") from error
        return True

    def acquire(self) -> str | bytes | None:
        try:
            payload = self.client.lmove(self.queue_name, self.inflight_name, "LEFT", "RIGHT")
        except redis.RedisError as error:
            raise QueueOperationError("Unable to consume document processing job.") from error
        if payload is None or isinstance(payload, (str, bytes)):
            return payload
        raise QueueOperationError("Redis returned an invalid document processing job.")

    def acknowledge(self, payload: str | bytes) -> None:
        try:
            self.client.lrem(self.inflight_name, 1, payload)
        except redis.RedisError as error:
            raise QueueOperationError("Unable to acknowledge document processing job.") from error

    def release(self, payload: str | bytes) -> None:
        try:
            self.client.execute_command(
                "EVAL",
                RELEASE_INFLIGHT_JOB,
                2,
                self.inflight_name,
                self.queue_name,
                payload,
            )
        except redis.RedisError as error:
            raise QueueOperationError("Unable to release document processing job.") from error

    def inflight_jobs(self) -> list[str | bytes]:
        try:
            return self.client.lrange(self.inflight_name, 0, -1)
        except redis.RedisError as error:
            raise QueueOperationError("Unable to inspect in-flight processing jobs.") from error

    def recover_inflight(self) -> int:
        recovered = 0
        try:
            while self.client.rpoplpush(self.inflight_name, self.queue_name) is not None:
                recovered += 1
        except redis.RedisError as error:
            raise QueueOperationError("Unable to recover in-flight processing jobs.") from error
        return recovered


def get_document_processing_queue() -> DocumentProcessingQueue:
    return DocumentProcessingQueue()
