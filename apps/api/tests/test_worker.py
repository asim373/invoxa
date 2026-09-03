import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import redis
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from apps.api.app.auth import get_current_user
from apps.api.app.database import get_db
from apps.api.app.main import app
from apps.api.app.models import Document, DocumentStatus, User
from apps.api.app.queue import DocumentProcessingQueue, QueueOperationError
from apps.api.app.text_extraction import DocumentTextExtractionError, OcrTextExtractor
from apps.api.app.worker import DocumentProcessingWorker, WorkerOperationError


class FakeRedis:
    def __init__(self, payload: str | bytes | None) -> None:
        self.lists: dict[str, list[str | bytes]] = {
            "documents": [] if payload is None else [payload],
            "documents:inflight": [],
        }

    def lmove(self, source: str, destination: str, wherefrom: str, whereto: str):
        if not self.lists.get(source):
            return None
        payload = self.lists[source].pop(0)
        self.lists.setdefault(destination, []).append(payload)
        return payload

    def rpush(self, name: str, *values: str) -> int:
        self.lists.setdefault(name, []).extend(values)
        return len(self.lists[name])

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
        payload = self.lists[source].pop()
        self.lists.setdefault(destination, []).insert(0, payload)
        return payload

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


class FakeDatabase:
    def __init__(self, document: Document | None = None, fail_on_get: bool = False) -> None:
        self.document = document
        self.fail_on_get = fail_on_get
        self.commits = 0
        self.rollbacks = 0

    def get(self, model, document_id: uuid.UUID) -> Document | None:
        if self.fail_on_get:
            raise RuntimeError("Database unavailable")
        if self.document is not None and self.document.id == document_id:
            return self.document
        return None

    def scalar(self, statement):
        parameters = statement.compile().params
        document_id = next(
            (value for value in parameters.values() if isinstance(value, uuid.UUID)),
            None,
        )
        return self.get(Document, document_id) if document_id is not None else None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def execute(self, statement):
        class Result:
            def __init__(self, values) -> None:
                self.values = values

            def all(self):
                return self.values

            def scalars(self):
                return self

        values = []
        if self.document is not None and self.document.status is DocumentStatus.QUEUED:
            values.append(self.document.id)
        return Result(values)


def make_document(status: DocumentStatus) -> Document:
    now = datetime.now(UTC)
    return Document(
        id=uuid.uuid4(),
        original_filename="invoice.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="a" * 64,
        storage_location="document.pdf",
        status=status,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture(autouse=True)
def authenticated_user():
    user = User(id=uuid.uuid4(), email="worker@example.com", password_hash="not-used")
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.clear()


def write_pdf(path: Path, text: str | None = None) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=72, height=72)
    if text is not None:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as pdf_file:
        writer.write(pdf_file)


class FakeExtractor:
    def __init__(self, text: str = "OCR text") -> None:
        self.text = text

    def extract(self, document_path: Path) -> str:
        return self.text

    def recognize(self, image) -> str:
        return self.text


class FailingExtractor:
    def extract(self, document_path: Path) -> str:
        raise DocumentTextExtractionError("OCR produced no extractable text.")


class EmptyPdfExtractor:
    def extract(self, document_path: Path) -> str:
        raise DocumentTextExtractionError("PDF contains no extractable text.")


class UnexpectedExtractor:
    def extract(self, document_path: Path) -> str:
        raise RuntimeError("unexpected extractor crash")


def test_worker_processes_queued_document_to_completion(tmp_path: Path, monkeypatch) -> None:
    document = make_document(DocumentStatus.QUEUED)
    write_pdf(tmp_path / document.storage_location, "Document text")
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    assert worker.process_once() is True
    assert document.status is DocumentStatus.COMPLETED
    assert database.commits == 2

    # A single published job is sufficient; no later upload/event is needed.
    assert worker.process_once() is False


def test_worker_extracts_text_from_valid_pdf(tmp_path: Path, monkeypatch) -> None:
    document = make_document(DocumentStatus.QUEUED)
    document.storage_location = "document.pdf"
    write_pdf(tmp_path / document.storage_location, "Total: 100")
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    assert worker.process_once() is True
    assert document.extracted_text == "Total: 100"
    assert document.status is DocumentStatus.COMPLETED
    assert document.invoice_extraction is not None
    assert document.invoice_extraction.total == Decimal("100.00")
    assert database.commits == 2


@pytest.mark.parametrize(
    "mime_type",
    [
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
        "image/bmp",
        "image/gif",
        "image/tiff",
    ],
)
def test_worker_persists_ocr_text_and_reuses_invoice_pipeline(
    tmp_path: Path, monkeypatch, mime_type: str
) -> None:
    document = make_document(DocumentStatus.QUEUED)
    document.mime_type = mime_type
    suffix = ".jpg" if mime_type == "image/jpeg" else f".{mime_type.split('/')[-1]}"
    document.storage_location = f"document{suffix}"
    (tmp_path / document.storage_location).write_bytes(b"image")
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"),
        database,
        image_extractor=FakeExtractor("Invoice Number: OCR-1\nTotal: 10"),
    )

    assert worker.process_once() is True
    assert document.extracted_text == "Invoice Number: OCR-1\nTotal: 10"
    assert document.invoice_extraction is not None
    assert document.invoice_extraction.invoice_number == "OCR-1"
    assert document.invoice_extraction.total == Decimal("10.00")
    assert document.status is DocumentStatus.COMPLETED


def test_worker_marks_ocr_failure_as_failed(tmp_path: Path, monkeypatch) -> None:
    document = make_document(DocumentStatus.QUEUED)
    document.mime_type = "image/png"
    document.storage_location = "document.png"
    (tmp_path / document.storage_location).write_bytes(b"image")
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"),
        database,
        image_extractor=FailingExtractor(),
    )

    assert worker.process_once() is False
    assert document.status is DocumentStatus.FAILED
    assert document.extracted_text is None
    assert document.error_details == "OCR produced no extractable text."


def test_worker_unexpected_exception_reaches_failed_terminal_state(
    tmp_path: Path, monkeypatch
) -> None:
    document = make_document(DocumentStatus.QUEUED)
    write_pdf(tmp_path / document.storage_location, "text")
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"),
        FakeDatabase(document),
        pdf_extractor=UnexpectedExtractor(),
    )

    assert worker.process_once() is False
    assert document.status is DocumentStatus.FAILED
    assert document.error_details == "Unexpected document processing failure."


def test_worker_marks_corrupt_image_as_failed_with_meaningful_error(
    tmp_path: Path, monkeypatch
) -> None:
    document = make_document(DocumentStatus.QUEUED)
    document.mime_type = "image/jpeg"
    document.storage_location = "corrupt.jpg"
    (tmp_path / document.storage_location).write_bytes(b"not a valid JPEG")
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"),
        database,
        image_extractor=OcrTextExtractor(ocr=FakeExtractor()),
    )

    assert worker.process_once() is False
    assert document.status is DocumentStatus.FAILED
    assert document.error_details == ("Image file is corrupt or uses an unsupported format.")


def test_worker_rejects_unsupported_image_type(tmp_path: Path, monkeypatch) -> None:
    document = make_document(DocumentStatus.QUEUED)
    document.mime_type = "image/svg+xml"
    document.storage_location = "document.svg"
    (tmp_path / document.storage_location).write_bytes(b"image")
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    assert worker.process_once() is False
    assert document.status is DocumentStatus.FAILED
    assert document.error_details == "Unsupported document type; expected PDF or a supported image."


def test_worker_persists_invalid_invoice_result_without_crashing(
    tmp_path: Path, monkeypatch
) -> None:
    document = make_document(DocumentStatus.QUEUED)
    write_pdf(tmp_path / document.storage_location, "Subtotal: 100\nTax: 20\nTotal: 125")
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    assert worker.process_once() is True
    assert document.status is DocumentStatus.COMPLETED
    assert document.invoice_extraction is not None
    assert document.invoice_extraction.is_valid is False
    assert document.invoice_extraction.validation_error == (
        "Subtotal plus tax does not match total."
    )


def test_worker_persists_invoice_line_items(tmp_path: Path, monkeypatch) -> None:
    document = make_document(DocumentStatus.QUEUED)
    write_pdf(
        tmp_path / document.storage_location,
        "Product A | 2 | 10.00 | 20.00\nSubtotal: 20.00",
    )
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    assert worker.process_once() is True
    assert document.invoice_extraction is not None
    assert len(document.invoice_extraction.line_items) == 1
    assert document.invoice_extraction.line_items[0].invoice_document_id == document.id
    assert document.invoice_extraction.line_items[0].position == 0


def test_worker_persists_line_item_positions_in_source_order(tmp_path: Path, monkeypatch) -> None:
    document = make_document(DocumentStatus.QUEUED)
    write_pdf(
        tmp_path / document.storage_location,
        "First | 1 | 2.00 | 2.00\nSecond | 1 | 3.00 | 3.00\nSubtotal: 5.00",
    )
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), FakeDatabase(document)
    )

    assert worker.process_once() is True
    assert document.invoice_extraction is not None
    assert [item.description for item in document.invoice_extraction.line_items] == [
        "First",
        "Second",
    ]
    assert [item.position for item in document.invoice_extraction.line_items] == [0, 1]


def test_extracted_text_is_available_from_document_retrieval(tmp_path: Path, monkeypatch) -> None:
    document = make_document(DocumentStatus.QUEUED)
    write_pdf(tmp_path / document.storage_location, "Persisted invoice text")
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    database = FakeDatabase(document)
    payload = json.dumps({"document_id": str(document.id)})
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )
    worker.process_once()
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).get(f"/documents/{document.id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["extracted_text"] == "Persisted invoice text"


def test_worker_marks_textless_pdf_as_failed(tmp_path: Path, monkeypatch) -> None:
    document = make_document(DocumentStatus.QUEUED)
    write_pdf(tmp_path / document.storage_location)
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"),
        database,
        pdf_extractor=EmptyPdfExtractor(),
    )

    assert worker.process_once() is False
    assert document.status is DocumentStatus.FAILED
    assert document.extracted_text is None
    assert document.error_details == "PDF contains no extractable text."


def test_worker_marks_malformed_pdf_as_failed(tmp_path: Path, monkeypatch) -> None:
    document = make_document(DocumentStatus.QUEUED)
    (tmp_path / document.storage_location).write_bytes(b"not a PDF")
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    assert worker.process_once() is False
    assert document.status is DocumentStatus.FAILED
    assert document.extracted_text is None
    assert document.error_details == "PDF text extraction failed."


def test_worker_marks_unsupported_document_type_as_failed(tmp_path: Path, monkeypatch) -> None:
    document = make_document(DocumentStatus.QUEUED)
    document.mime_type = "application/octet-stream"
    (tmp_path / document.storage_location).write_bytes(b"image")
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    assert worker.process_once() is False
    assert document.status is DocumentStatus.FAILED
    assert document.extracted_text is None
    assert document.error_details == "Unsupported document type; expected PDF or a supported image."


def test_worker_marks_missing_file_as_failed(tmp_path: Path, monkeypatch) -> None:
    document = make_document(DocumentStatus.QUEUED)
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    assert worker.process_once() is False
    assert document.status is DocumentStatus.FAILED
    assert document.error_details == "Stored document file is missing or invalid."
    assert database.commits == 2


def test_worker_marks_outside_root_file_as_failed(tmp_path: Path, monkeypatch) -> None:
    document = make_document(DocumentStatus.QUEUED)
    document.storage_location = "../outside.pdf"
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    assert worker.process_once() is False
    assert document.status is DocumentStatus.FAILED
    assert document.error_details == (
        "Stored document location is outside the configured storage root."
    )


def test_worker_marks_unreadable_storage_condition_as_failed(tmp_path: Path, monkeypatch) -> None:
    document = make_document(DocumentStatus.QUEUED)
    (tmp_path / document.storage_location).mkdir()
    monkeypatch.setattr("apps.api.app.worker.settings.storage_path", tmp_path)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    assert worker.process_once() is False
    assert document.status is DocumentStatus.FAILED
    assert document.error_details == "Stored document file is missing or invalid."


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps({"wrong": "field"}),
        json.dumps({"document_id": "invalid"}),
    ],
)
def test_worker_discards_invalid_payload(payload: str) -> None:
    database = FakeDatabase()
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    assert worker.process_once() is False
    assert database.commits == 0


def test_worker_discards_missing_document() -> None:
    document_id = uuid.uuid4()
    payload = json.dumps({"document_id": str(document_id)})
    database = FakeDatabase()
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    assert worker.process_once() is False
    assert database.commits == 0


def test_worker_discards_document_that_is_not_queued() -> None:
    document = make_document(DocumentStatus.PROCESSING)
    payload = json.dumps({"document_id": str(document.id)})
    database = FakeDatabase(document)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    assert worker.process_once() is False
    assert document.status is DocumentStatus.PROCESSING
    assert database.commits == 0


def test_worker_translates_redis_failure() -> None:
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FailingRedis(), "documents"), FakeDatabase()
    )

    with pytest.raises(QueueOperationError):
        worker.process_once()


def test_worker_translates_database_failure() -> None:
    document_id = uuid.uuid4()
    payload = json.dumps({"document_id": str(document_id)})
    database = FakeDatabase(fail_on_get=True)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(FakeRedis(payload), "documents"), database
    )

    with pytest.raises(WorkerOperationError, match="retrieve document"):
        worker.process_once()

    assert database.rollbacks == 1


def test_database_failure_releases_acquired_job_for_retry() -> None:
    document_id = uuid.uuid4()
    payload = json.dumps({"document_id": str(document_id)})
    redis_client = FakeRedis(payload)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(redis_client, "documents"),
        FakeDatabase(fail_on_get=True),
    )

    with pytest.raises(WorkerOperationError):
        worker.process_once()

    assert redis_client.lists["documents"] == [payload]
    assert redis_client.lists["documents:inflight"] == []


def test_worker_restart_recovers_abandoned_processing_job() -> None:
    document = make_document(DocumentStatus.PROCESSING)
    payload = json.dumps({"document_id": str(document.id)}, separators=(",", ":"))
    redis_client = FakeRedis(None)
    redis_client.lists["documents:inflight"] = [payload]
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(redis_client, "documents"), FakeDatabase(document)
    )

    assert worker.recover_abandoned_jobs() == 1
    assert document.status is DocumentStatus.QUEUED
    assert redis_client.lists["documents:inflight"] == []
    assert redis_client.lists["documents"] == [payload]


def test_reconciler_restores_committed_queued_job_without_duplicate() -> None:
    document = make_document(DocumentStatus.QUEUED)
    redis_client = FakeRedis(None)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(redis_client, "documents"), FakeDatabase(document)
    )

    assert worker.reconcile_queued_documents() == 1
    assert worker.reconcile_queued_documents() == 0
    assert len(redis_client.lists["documents"]) == 1


def test_duplicate_delivery_after_completion_is_safely_acknowledged() -> None:
    document = make_document(DocumentStatus.COMPLETED)
    payload = json.dumps({"document_id": str(document.id)})
    redis_client = FakeRedis(payload)
    worker = DocumentProcessingWorker(
        DocumentProcessingQueue(redis_client, "documents"), FakeDatabase(document)
    )

    assert worker.process_once() is False
    assert document.status is DocumentStatus.COMPLETED
    assert redis_client.lists["documents:inflight"] == []
