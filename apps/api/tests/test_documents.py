import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app.auth import get_current_user
from apps.api.app.database import get_db
from apps.api.app.documents import _commit_queued_then_publish
from apps.api.app.main import app
from apps.api.app.models import (
    Document,
    DocumentStatus,
    InvalidDocumentStatusTransition,
    InvoiceExtraction,
    InvoiceLineItem,
    User,
)
from apps.api.app.queue import QueueOperationError, get_document_processing_queue
from apps.api.app.settings import settings


class FakeDatabase:
    def __init__(self) -> None:
        self.documents: list[Document] = []

    def add(self, document: Document) -> None:
        self.documents.append(document)

    def commit(self) -> None:
        pass

    def delete(self, document: Document) -> None:
        self.documents.remove(document)

    def rollback(self) -> None:
        self.documents.clear()

    def get(self, model, document_id: uuid.UUID) -> Document | None:
        return next(
            (document for document in self.documents if document.id == document_id),
            None,
        )

    def _filter_documents(self, statement) -> list[Document]:
        docs = list(self.documents)
        if statement.whereclause is None:
            return docs

        clauses = (
            statement.whereclause.clauses
            if hasattr(statement.whereclause, "clauses")
            else [statement.whereclause]
        )

        for clause in clauses:
            col_name = getattr(getattr(clause, "left", None), "name", None)
            op_name = getattr(getattr(clause, "operator", None), "__name__", None)
            val = getattr(getattr(clause, "right", None), "value", None)

            if col_name == "owner_id" and op_name == "eq":
                docs = [d for d in docs if d.owner_id == val or d.owner_id is None]
            elif col_name == "status" and op_name == "eq":
                docs = [d for d in docs if d.status == val]
            elif col_name == "created_at" and op_name == "ge" and isinstance(val, datetime):
                docs = [d for d in docs if d.created_at >= val]
            elif col_name == "created_at" and op_name == "le" and isinstance(val, datetime):
                docs = [d for d in docs if d.created_at <= val]
            elif col_name == "id" and op_name == "eq":
                docs = [d for d in docs if d.id == val]

        sql_lower = str(statement).lower()
        if "invoice_extractions" in sql_lower:
            params = statement.compile().params
            is_valid_val = next(
                (v for k, v in params.items() if "is_valid" in k and isinstance(v, bool)),
                None,
            )
            if is_valid_val is None:
                if (
                    "is_valid = true" in sql_lower
                    or "is_valid is true" in sql_lower
                    or "is_valid = 1" in sql_lower
                    or "is_valid = :is_valid" in sql_lower
                ):
                    is_valid_val = True
                elif (
                    "is_valid = false" in sql_lower
                    or "is_valid is false" in sql_lower
                    or "is_valid = 0" in sql_lower
                ):
                    is_valid_val = False

            if is_valid_val is not None:
                docs = [
                    d
                    for d in docs
                    if getattr(d, "invoice_extraction", None) is not None
                    and d.invoice_extraction.is_valid is is_valid_val
                ]

        return docs

    def scalar(self, statement):
        sql = str(statement)
        if "count" in sql.lower():
            filtered = self._filter_documents(statement)
            return len(filtered)
        filtered = self._filter_documents(statement)
        return filtered[0] if filtered else None

    def scalars(self, statement):
        filtered = self._filter_documents(statement)
        documents = sorted(
            filtered,
            key=lambda document: (document.created_at, document.id),
            reverse=True,
        )
        limit = (
            statement._limit_clause.value if statement._limit_clause is not None else len(documents)
        )
        offset = statement._offset_clause.value if statement._offset_clause is not None else 0
        return FakeScalarResult(documents[offset : offset + limit])


@pytest.fixture(autouse=True)
def authenticated_user():
    user = User(id=uuid.uuid4(), email="test@example.com", password_hash="not-used")
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


class FakeScalarResult:
    def __init__(self, documents: list[Document]) -> None:
        self.documents = documents

    def all(self) -> list[Document]:
        return self.documents


class FakeQueue:
    def __init__(self) -> None:
        self.document_ids: list[uuid.UUID] = []

    def enqueue(self, document_id: uuid.UUID) -> None:
        self.document_ids.append(document_id)


class FailingQueue:
    def enqueue(self, document_id: uuid.UUID) -> None:
        raise QueueOperationError("queue unavailable")


def test_document_status_lifecycle_allows_expected_transitions() -> None:
    document = Document(status=DocumentStatus.UPLOADED)

    document.transition_to(DocumentStatus.QUEUED)
    document.transition_to(DocumentStatus.PROCESSING)
    document.transition_to(DocumentStatus.COMPLETED)

    assert document.status is DocumentStatus.COMPLETED


def test_document_status_lifecycle_allows_reprocess_transitions() -> None:
    doc_failed = Document(status=DocumentStatus.FAILED)
    doc_failed.transition_to(DocumentStatus.QUEUED)
    assert doc_failed.status is DocumentStatus.QUEUED

    doc_completed = Document(status=DocumentStatus.COMPLETED)
    doc_completed.transition_to(DocumentStatus.QUEUED)
    assert doc_completed.status is DocumentStatus.QUEUED

    doc_proc = Document(status=DocumentStatus.PROCESSING)
    doc_proc.transition_to(DocumentStatus.QUEUED)
    assert doc_proc.status is DocumentStatus.QUEUED


def test_document_status_lifecycle_allows_processing_failure() -> None:
    document = Document(status=DocumentStatus.UPLOADED)

    document.transition_to(DocumentStatus.QUEUED)
    document.transition_to(DocumentStatus.PROCESSING)
    document.transition_to(DocumentStatus.FAILED)

    assert document.status is DocumentStatus.FAILED


def test_document_status_lifecycle_rejects_invalid_transition() -> None:
    document = Document(status=DocumentStatus.UPLOADED)

    try:
        document.transition_to(DocumentStatus.PROCESSING)
    except InvalidDocumentStatusTransition:
        pass
    else:
        raise AssertionError("Expected invalid document status transition to be rejected")

    assert document.status is DocumentStatus.UPLOADED


def test_upload_document_persists_metadata_and_file(tmp_path: Path, monkeypatch) -> None:
    database = FakeDatabase()
    queue = FakeQueue()
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    monkeypatch.setattr(settings, "max_upload_size", 1024)
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_document_processing_queue] = lambda: queue
    content = b"%PDF-1.4 invoice contents"

    try:
        response = TestClient(app).post(
            "/documents",
            files={"file": ("invoice.pdf", content, "application/pdf")},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_document_processing_queue, None)

    assert response.status_code == 201
    payload = response.json()
    assert payload["original_filename"] == "invoice.pdf"
    assert payload["mime_type"] == "application/pdf"
    assert payload["file_size"] == len(content)
    assert database.documents[0].checksum == hashlib.sha256(content).hexdigest()
    assert "checksum" not in payload
    assert payload["status"] == "queued"
    assert "storage_location" not in payload
    assert database.documents[0].storage_location != "invoice.pdf"
    assert (tmp_path / database.documents[0].storage_location).read_bytes() == content
    assert database.documents[0].owner_id is not None
    assert queue.document_ids == [database.documents[0].id]


def test_upload_commits_queued_status_before_publishing_job(tmp_path: Path, monkeypatch) -> None:
    events: list[tuple[str, DocumentStatus]] = []

    class RecordingDatabase(FakeDatabase):
        def commit(self) -> None:
            events.append(("commit", self.documents[0].status))

    class RecordingQueue(FakeQueue):
        def enqueue(self, document_id: uuid.UUID) -> None:
            events.append(("enqueue", database.documents[0].status))
            super().enqueue(document_id)

    database = RecordingDatabase()
    queue = RecordingQueue()
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_document_processing_queue] = lambda: queue

    try:
        response = TestClient(app).post(
            "/documents",
            files={"file": ("invoice.pdf", b"%PDF-1.4 invoice", "application/pdf")},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_document_processing_queue, None)

    assert response.status_code == 201
    assert events == [
        ("commit", DocumentStatus.QUEUED),
        ("enqueue", DocumentStatus.QUEUED),
    ]


def test_publish_never_enqueues_when_queued_commit_fails() -> None:
    class CommitFailureDatabase:
        def commit(self) -> None:
            raise RuntimeError("database commit failed")

    document = Document(id=uuid.uuid4(), status=DocumentStatus.QUEUED)
    queue = FakeQueue()

    with pytest.raises(RuntimeError, match="database commit failed"):
        _commit_queued_then_publish(document, CommitFailureDatabase(), queue)

    assert queue.document_ids == []


def test_publish_rejects_nonqueued_document_without_enqueuing() -> None:
    document = Document(id=uuid.uuid4(), status=DocumentStatus.UPLOADED)
    queue = FakeQueue()

    with pytest.raises(RuntimeError, match="only be published for queued"):
        _commit_queued_then_publish(document, FakeDatabase(), queue)

    assert queue.document_ids == []


def test_upload_document_cleans_up_when_queue_enqueue_fails(tmp_path: Path, monkeypatch) -> None:
    database = FakeDatabase()
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_document_processing_queue] = lambda: FailingQueue()

    try:
        response = TestClient(app).post(
            "/documents",
            files={"file": ("invoice.pdf", b"%PDF-1.4 invoice contents", "application/pdf")},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_document_processing_queue, None)

    assert response.status_code == 503
    assert response.json()["detail"] == "Unable to enqueue document for processing."
    assert database.documents == []
    assert list(tmp_path.iterdir()) == []


def test_list_documents_returns_newest_first_with_pagination(authenticated_user: User) -> None:
    database = FakeDatabase()
    now = datetime.now(UTC)
    older = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="older.pdf",
        mime_type="application/pdf",
        file_size=5,
        checksum="a" * 64,
        storage_location="older-id.pdf",
        status=DocumentStatus.UPLOADED,
        extracted_text="OCR text",
        created_at=now - timedelta(minutes=2),
        updated_at=now - timedelta(minutes=2),
    )
    newest = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="newest.png",
        mime_type="image/png",
        file_size=6,
        checksum="b" * 64,
        storage_location="newest-id.png",
        status=DocumentStatus.COMPLETED,
        extracted_text="OCR text",
        created_at=now,
        updated_at=now,
    )
    middle = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="middle.jpg",
        mime_type="image/jpeg",
        file_size=7,
        checksum="c" * 64,
        storage_location="middle-id.jpg",
        status=DocumentStatus.PROCESSING,
        extracted_text="OCR text",
        created_at=now - timedelta(minutes=1),
        updated_at=now - timedelta(minutes=1),
    )
    database.documents.extend([older, newest, middle])
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).get("/documents?page=1&page_size=2")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 3
        assert payload["page"] == 1
        assert payload["page_size"] == 2
        assert payload["total_pages"] == 2
        assert len(payload["items"]) == 2
        assert [doc["original_filename"] for doc in payload["items"]] == [
            "newest.png",
            "middle.jpg",
        ]
        assert "extracted_text" not in payload["items"][0]
        assert payload["items"][0]["status"] == "completed"
        assert "created_at" in payload["items"][0]
        assert "updated_at" in payload["items"][0]

        # Page 2
        response_p2 = TestClient(app).get("/documents?page=2&page_size=2")
        assert response_p2.status_code == 200
        payload_p2 = response_p2.json()
        assert payload_p2["total"] == 3
        assert payload_p2["page"] == 2
        assert payload_p2["page_size"] == 2
        assert payload_p2["total_pages"] == 2
        assert len(payload_p2["items"]) == 1
        assert payload_p2["items"][0]["original_filename"] == "older.pdf"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_list_documents_rejects_invalid_pagination() -> None:
    assert TestClient(app).get("/documents?page=0").status_code == 422
    assert TestClient(app).get("/documents?page_size=0").status_code == 422
    assert TestClient(app).get("/documents?page_size=101").status_code == 422


def test_list_documents_out_of_range_page_returns_empty_items(authenticated_user: User) -> None:
    database = FakeDatabase()
    database.documents.append(
        Document(
            id=uuid.uuid4(),
            owner_id=authenticated_user.id,
            original_filename="doc.pdf",
            mime_type="application/pdf",
            file_size=10,
            checksum="a" * 64,
            storage_location="doc.pdf",
            status=DocumentStatus.UPLOADED,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).get("/documents?page=5&page_size=10")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == []
    assert payload["total"] == 1
    assert payload["page"] == 5
    assert payload["page_size"] == 10
    assert payload["total_pages"] == 1


def test_list_documents_empty_database_returns_zero_metadata() -> None:
    app.dependency_overrides[get_db] = lambda: FakeDatabase()

    try:
        response = TestClient(app).get("/documents")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == []
    assert payload["total"] == 0
    assert payload["page"] == 1
    assert payload["page_size"] == 20
    assert payload["total_pages"] == 0


def test_list_documents_filter_by_status(authenticated_user: User) -> None:
    database = FakeDatabase()
    now = datetime.now(UTC)
    doc_completed = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="comp.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="a" * 64,
        storage_location="comp.pdf",
        status=DocumentStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )
    doc_failed = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="fail.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="b" * 64,
        storage_location="fail.pdf",
        status=DocumentStatus.FAILED,
        created_at=now - timedelta(minutes=1),
        updated_at=now - timedelta(minutes=1),
    )
    database.documents.extend([doc_completed, doc_failed])
    app.dependency_overrides[get_db] = lambda: database

    try:
        res_comp = TestClient(app).get("/documents?status=completed")
        assert res_comp.status_code == 200
        data_comp = res_comp.json()
        assert data_comp["total"] == 1
        assert data_comp["items"][0]["original_filename"] == "comp.pdf"

        res_fail = TestClient(app).get("/documents?status=failed")
        assert res_fail.status_code == 200
        data_fail = res_fail.json()
        assert data_fail["total"] == 1
        assert data_fail["items"][0]["original_filename"] == "fail.pdf"

        res_queued = TestClient(app).get("/documents?status=queued")
        assert res_queued.status_code == 200
        data_queued = res_queued.json()
        assert data_queued["total"] == 0
        assert data_queued["items"] == []
        assert data_queued["total_pages"] == 0

        res_invalid = TestClient(app).get("/documents?status=not_a_status")
        assert res_invalid.status_code == 422
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_list_documents_filter_by_date_range(authenticated_user: User) -> None:
    database = FakeDatabase()
    t0 = datetime(2026, 8, 25, 10, 0, 0, tzinfo=UTC)
    doc1 = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="doc1.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="a" * 64,
        storage_location="doc1.pdf",
        status=DocumentStatus.COMPLETED,
        created_at=t0,
        updated_at=t0,
    )
    doc2 = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="doc2.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="b" * 64,
        storage_location="doc2.pdf",
        status=DocumentStatus.COMPLETED,
        created_at=t0 + timedelta(hours=1),
        updated_at=t0 + timedelta(hours=1),
    )
    doc3 = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="doc3.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="c" * 64,
        storage_location="doc3.pdf",
        status=DocumentStatus.COMPLETED,
        created_at=t0 + timedelta(hours=2),
        updated_at=t0 + timedelta(hours=2),
    )
    database.documents.extend([doc1, doc2, doc3])
    app.dependency_overrides[get_db] = lambda: database

    try:
        t_after = (t0 + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t_before = (t0 + timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t_out = (t0 + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

        res_after = TestClient(app).get(f"/documents?created_after={t_after}")
        assert res_after.status_code == 200
        assert res_after.json()["total"] == 2
        assert [d["original_filename"] for d in res_after.json()["items"]] == [
            "doc3.pdf",
            "doc2.pdf",
        ]

        res_before = TestClient(app).get(f"/documents?created_before={t_before}")
        assert res_before.status_code == 200
        assert res_before.json()["total"] == 2
        assert [d["original_filename"] for d in res_before.json()["items"]] == [
            "doc2.pdf",
            "doc1.pdf",
        ]

        res_between = TestClient(app).get(
            f"/documents?created_after={t_after}&created_before={t_before}"
        )
        assert res_between.status_code == 200
        assert res_between.json()["total"] == 1
        assert res_between.json()["items"][0]["original_filename"] == "doc2.pdf"

        res_out = TestClient(app).get(f"/documents?created_after={t_out}")
        assert res_out.status_code == 200
        assert res_out.json()["total"] == 0
        assert res_out.json()["items"] == []
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_list_documents_filter_by_is_valid(authenticated_user: User) -> None:
    database = FakeDatabase()
    now = datetime.now(UTC)
    doc_valid = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="valid.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="a" * 64,
        storage_location="valid.pdf",
        status=DocumentStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )
    doc_valid.invoice_extraction = InvoiceExtraction(
        document_id=doc_valid.id,
        is_valid=True,
    )
    doc_invalid = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="invalid.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="b" * 64,
        storage_location="invalid.pdf",
        status=DocumentStatus.COMPLETED,
        created_at=now - timedelta(minutes=1),
        updated_at=now - timedelta(minutes=1),
    )
    doc_invalid.invoice_extraction = InvoiceExtraction(
        document_id=doc_invalid.id,
        is_valid=False,
    )
    doc_unextracted = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="no_ext.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="c" * 64,
        storage_location="no_ext.pdf",
        status=DocumentStatus.PROCESSING,
        created_at=now - timedelta(minutes=2),
        updated_at=now - timedelta(minutes=2),
    )
    database.documents.extend([doc_valid, doc_invalid, doc_unextracted])
    app.dependency_overrides[get_db] = lambda: database

    try:
        res_valid = TestClient(app).get("/documents?is_valid=true")
        assert res_valid.status_code == 200
        assert res_valid.json()["total"] == 1
        assert res_valid.json()["items"][0]["original_filename"] == "valid.pdf"

        res_invalid = TestClient(app).get("/documents?is_valid=false")
        assert res_invalid.status_code == 200
        assert res_invalid.json()["total"] == 1
        assert res_invalid.json()["items"][0]["original_filename"] == "invalid.pdf"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_list_documents_combined_filters(authenticated_user: User) -> None:
    database = FakeDatabase()
    t0 = datetime(2026, 8, 25, 10, 0, 0, tzinfo=UTC)
    doc_match = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="match.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="a" * 64,
        storage_location="match.pdf",
        status=DocumentStatus.COMPLETED,
        created_at=t0 + timedelta(hours=1),
        updated_at=t0 + timedelta(hours=1),
    )
    doc_match.invoice_extraction = InvoiceExtraction(
        document_id=doc_match.id,
        is_valid=True,
    )
    doc_wrong_status = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="wrong_status.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="b" * 64,
        storage_location="wrong_status.pdf",
        status=DocumentStatus.FAILED,
        created_at=t0 + timedelta(hours=1),
        updated_at=t0 + timedelta(hours=1),
    )
    doc_wrong_status.invoice_extraction = InvoiceExtraction(
        document_id=doc_wrong_status.id,
        is_valid=True,
    )
    database.documents.extend([doc_match, doc_wrong_status])
    app.dependency_overrides[get_db] = lambda: database

    try:
        t_str = t0.strftime("%Y-%m-%dT%H:%M:%SZ")
        res = TestClient(app).get(
            f"/documents?status=completed&is_valid=true&created_after={t_str}"
        )
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 1
        assert data["items"][0]["original_filename"] == "match.pdf"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_list_documents_owner_isolation(authenticated_user: User) -> None:
    database = FakeDatabase()
    other_user_id = uuid.uuid4()
    now = datetime.now(UTC)

    my_doc = Document(
        id=uuid.uuid4(),
        owner_id=authenticated_user.id,
        original_filename="my_doc.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="a" * 64,
        storage_location="my_doc.pdf",
        status=DocumentStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )
    other_doc = Document(
        id=uuid.uuid4(),
        owner_id=other_user_id,
        original_filename="other_doc.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="b" * 64,
        storage_location="other_doc.pdf",
        status=DocumentStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )
    database.documents.extend([my_doc, other_doc])
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).get("/documents")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["original_filename"] == "my_doc.pdf"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_list_documents_requires_authentication() -> None:
    app.dependency_overrides.pop(get_current_user, None)
    try:
        response = TestClient(app).get("/documents")
        assert response.status_code == 401
    finally:
        pass


def test_get_document_returns_complete_metadata(authenticated_user: User) -> None:
    database = FakeDatabase()
    document_id = uuid.uuid4()
    created_at = datetime.now(UTC)
    database.documents.append(
        Document(
            id=document_id,
            owner_id=authenticated_user.id,
            original_filename="invoice.pdf",
            mime_type="application/pdf",
            file_size=123,
            checksum="d" * 64,
            storage_location="document-id.pdf",
            status=DocumentStatus.UPLOADED,
            extracted_text="Sample text",
            created_at=created_at,
            updated_at=created_at,
        )
    )
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).get(f"/documents/{document_id}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == str(document_id)
    assert payload["original_filename"] == "invoice.pdf"
    assert payload["mime_type"] == "application/pdf"
    assert payload["file_size"] == 123
    assert "checksum" not in payload
    assert "storage_location" not in payload
    assert payload["status"] == "uploaded"
    assert payload["extracted_text"] == "Sample text"
    assert payload["created_at"]
    assert payload["updated_at"]
    assert "content" not in payload


def test_get_document_returns_structured_invoice_results(authenticated_user: User) -> None:
    database = FakeDatabase()
    document_id = uuid.uuid4()
    now = datetime.now(UTC)
    document = Document(
        id=document_id,
        owner_id=authenticated_user.id,
        original_filename="invoice.pdf",
        mime_type="application/pdf",
        file_size=123,
        checksum="d" * 64,
        storage_location="document-id.pdf",
        status=DocumentStatus.COMPLETED,
        error_details=None,
        created_at=now,
        updated_at=now,
    )
    extraction = InvoiceExtraction(
        document_id=document_id,
        invoice_number="INV-42",
        invoice_date=date(2026, 9, 2),
        vendor_name="Acme",
        customer_name=None,
        currency="USD",
        subtotal=Decimal("10.00"),
        tax=Decimal("1.00"),
        total=Decimal("11.00"),
        is_valid=True,
        validation_error=None,
    )
    extraction.line_items = [
        InvoiceLineItem(
            position=0,
            description="Consulting",
            quantity=Decimal("1.0000"),
            unit_price=Decimal("10.00"),
            line_total=Decimal("10.00"),
        )
    ]
    document.invoice_extraction = extraction
    database.documents.append(document)
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).get(f"/documents/{document_id}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    invoice = response.json()["invoice_extraction"]
    assert invoice["invoice_number"] == "INV-42"
    assert invoice["customer_name"] is None
    assert invoice["total"] == "11.00"
    assert invoice["line_items"] == [
        {
            "position": 0,
            "description": "Consulting",
            "quantity": "1.0000",
            "unit_price": "10.00",
            "line_total": "10.00",
        }
    ]


def test_get_document_returns_not_found_for_unknown_id() -> None:
    app.dependency_overrides[get_db] = lambda: FakeDatabase()
    document_id = uuid.uuid4()

    try:
        response = TestClient(app).get(f"/documents/{document_id}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found."


def test_get_document_rejects_invalid_id() -> None:
    response = TestClient(app).get("/documents/not-a-uuid")

    assert response.status_code == 422


def test_get_document_file_returns_stored_file(
    tmp_path: Path, monkeypatch, authenticated_user: User
) -> None:
    database = FakeDatabase()
    document_id = uuid.uuid4()
    stored_file = tmp_path / f"{document_id}.pdf"
    file_bytes = b"%PDF-1.4 test invoice content"
    stored_file.write_bytes(file_bytes)
    database.documents.append(
        Document(
            id=document_id,
            owner_id=authenticated_user.id,
            original_filename="my_invoice.pdf",
            mime_type="application/pdf",
            file_size=len(file_bytes),
            checksum="a" * 64,
            storage_location=stored_file.name,
            status=DocumentStatus.COMPLETED,
        )
    )
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).get(f"/documents/{document_id}/file")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert response.content == file_bytes
    assert response.headers["content-type"] == "application/pdf"
    assert 'filename="my_invoice.pdf"' in response.headers["content-disposition"]
    assert "inline" in response.headers["content-disposition"]


def test_get_document_file_owner_isolation(
    tmp_path: Path, monkeypatch, authenticated_user: User
) -> None:
    database = FakeDatabase()
    document_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    stored_file = tmp_path / f"{document_id}.pdf"
    stored_file.write_bytes(b"content")
    database.documents.append(
        Document(
            id=document_id,
            owner_id=other_user_id,
            original_filename="other.pdf",
            mime_type="application/pdf",
            file_size=7,
            checksum="a" * 64,
            storage_location=stored_file.name,
            status=DocumentStatus.COMPLETED,
        )
    )
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).get(f"/documents/{document_id}/file")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found."


def test_get_document_file_missing_on_disk_returns_500(
    tmp_path: Path, monkeypatch, authenticated_user: User
) -> None:
    database = FakeDatabase()
    document_id = uuid.uuid4()
    database.documents.append(
        Document(
            id=document_id,
            owner_id=authenticated_user.id,
            original_filename="missing.pdf",
            mime_type="application/pdf",
            file_size=10,
            checksum="a" * 64,
            storage_location="missing-on-disk.pdf",
            status=DocumentStatus.COMPLETED,
        )
    )
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).get(f"/documents/{document_id}/file")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 500
    assert response.json()["detail"] == "Stored document file is missing or unavailable."


def test_reprocess_document_success(tmp_path: Path, monkeypatch, authenticated_user: User) -> None:
    database = FakeDatabase()
    queue = FakeQueue()
    document_id = uuid.uuid4()
    stored_file = tmp_path / f"{document_id}.pdf"
    stored_file.write_bytes(b"pdf content")
    document = Document(
        id=document_id,
        owner_id=authenticated_user.id,
        original_filename="invoice.pdf",
        mime_type="application/pdf",
        file_size=11,
        checksum="a" * 64,
        storage_location=stored_file.name,
        status=DocumentStatus.FAILED,
        error_details="OCR failed previously",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    database.documents.append(document)
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_document_processing_queue] = lambda: queue

    try:
        response = TestClient(app).post(f"/documents/{document_id}/reprocess")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_document_processing_queue, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == str(document_id)
    assert payload["status"] == "queued"
    assert document.status is DocumentStatus.QUEUED
    assert queue.document_ids == [document_id]


def test_reprocess_document_rejects_already_queued(
    tmp_path: Path, monkeypatch, authenticated_user: User
) -> None:
    database = FakeDatabase()
    document_id = uuid.uuid4()
    stored_file = tmp_path / f"{document_id}.pdf"
    stored_file.write_bytes(b"content")
    database.documents.append(
        Document(
            id=document_id,
            owner_id=authenticated_user.id,
            original_filename="invoice.pdf",
            mime_type="application/pdf",
            file_size=7,
            checksum="a" * 64,
            storage_location=stored_file.name,
            status=DocumentStatus.QUEUED,
        )
    )
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).post(f"/documents/{document_id}/reprocess")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 409
    assert response.json()["detail"] == "Document is already queued for processing."


def test_reprocess_document_queue_failure_rolls_back_status(
    tmp_path: Path, monkeypatch, authenticated_user: User
) -> None:
    database = FakeDatabase()
    document_id = uuid.uuid4()
    stored_file = tmp_path / f"{document_id}.pdf"
    stored_file.write_bytes(b"content")
    document = Document(
        id=document_id,
        owner_id=authenticated_user.id,
        original_filename="invoice.pdf",
        mime_type="application/pdf",
        file_size=7,
        checksum="a" * 64,
        storage_location=stored_file.name,
        status=DocumentStatus.FAILED,
    )
    database.documents.append(document)
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_document_processing_queue] = lambda: FailingQueue()

    try:
        response = TestClient(app).post(f"/documents/{document_id}/reprocess")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_document_processing_queue, None)

    assert response.status_code == 503
    assert response.json()["detail"] == "Unable to enqueue document for processing."
    assert document.status is DocumentStatus.FAILED


def test_delete_document_removes_record_and_stored_file(
    tmp_path: Path, monkeypatch, authenticated_user: User
) -> None:
    database = FakeDatabase()
    document_id = uuid.uuid4()
    stored_file = tmp_path / "stored-document.pdf"
    stored_file.write_bytes(b"document")
    database.documents.append(
        Document(
            id=document_id,
            owner_id=authenticated_user.id,
            original_filename="invoice.pdf",
            mime_type="application/pdf",
            file_size=8,
            checksum="e" * 64,
            storage_location=stored_file.name,
            status=DocumentStatus.UPLOADED,
        )
    )
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).delete(f"/documents/{document_id}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 204
    assert response.content == b""
    assert database.documents == []
    assert not stored_file.exists()


def test_delete_document_succeeds_when_stored_file_is_missing(
    tmp_path: Path, monkeypatch, authenticated_user: User
) -> None:
    database = FakeDatabase()
    document_id = uuid.uuid4()
    database.documents.append(
        Document(
            id=document_id,
            owner_id=authenticated_user.id,
            original_filename="missing.pdf",
            mime_type="application/pdf",
            file_size=8,
            checksum="f" * 64,
            storage_location="missing-file.pdf",
            status=DocumentStatus.UPLOADED,
        )
    )
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).delete(f"/documents/{document_id}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 204
    assert database.documents == []


def test_delete_document_returns_not_found_for_unknown_id() -> None:
    app.dependency_overrides[get_db] = lambda: FakeDatabase()
    document_id = uuid.uuid4()

    try:
        response = TestClient(app).delete(f"/documents/{document_id}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found."


def test_delete_document_rejects_invalid_id() -> None:
    response = TestClient(app).delete("/documents/not-a-uuid")

    assert response.status_code == 422


def test_upload_rejects_empty_file(tmp_path: Path, monkeypatch) -> None:
    database = FakeDatabase()
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).post(
            "/documents",
            files={"file": ("empty.pdf", b"", "application/pdf")},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded file cannot be empty."


def test_upload_rejects_unsupported_mime_type() -> None:
    app.dependency_overrides[get_db] = lambda: FakeDatabase()

    try:
        response = TestClient(app).post(
            "/documents",
            files={"file": ("notes.txt", b"notes", "text/plain")},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 415
    assert response.json()["detail"] == "Unsupported MIME type."


def test_upload_rejects_files_over_configured_limit(tmp_path: Path, monkeypatch) -> None:
    database = FakeDatabase()
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    monkeypatch.setattr(settings, "max_upload_size", 3)
    app.dependency_overrides[get_db] = lambda: database

    try:
        response = TestClient(app).post(
            "/documents",
            files={"file": ("large.pdf", b"1234", "application/pdf")},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 413
    assert response.json()["detail"] == "File exceeds the maximum allowed size."
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("filename", "mime_type", "content", "extension"),
    [
        ("invoice.pdf", "application/pdf", b"%PDF-1.4 secure", ".pdf"),
        ("scan.jpg", "image/jpeg", b"\xff\xd8\xff\xe0jpeg", ".jpg"),
        ("scan.jpeg", "image/jpg", b"\xff\xd8\xff\xe0jpeg", ".jpg"),
        ("scan.png", "image/png", b"\x89PNG\r\n\x1a\npng", ".png"),
    ],
)
def test_upload_validates_supported_file_signatures(
    tmp_path: Path, monkeypatch, filename: str, mime_type: str, content: bytes, extension: str
) -> None:
    database = FakeDatabase()
    queue = FakeQueue()
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_document_processing_queue] = lambda: queue
    try:
        response = TestClient(app).post(
            "/documents", files={"file": (filename, content, mime_type)}
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_document_processing_queue, None)

    assert response.status_code == 201
    assert database.documents[0].storage_location.endswith(extension)


def test_upload_rejects_mime_spoofed_content_and_removes_file(tmp_path: Path, monkeypatch) -> None:
    database = FakeDatabase()
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_document_processing_queue] = lambda: FakeQueue()
    try:
        response = TestClient(app).post(
            "/documents",
            files={"file": ("fake.png", b"not-a-png", "image/png")},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_document_processing_queue, None)

    assert response.status_code == 400
    assert response.json() == {"detail": "File content does not match the declared file type."}
    assert database.documents == []
    assert list(tmp_path.iterdir()) == []


def test_upload_sanitizes_dangerous_original_filename(tmp_path: Path, monkeypatch) -> None:
    database = FakeDatabase()
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_document_processing_queue] = lambda: FakeQueue()
    try:
        response = TestClient(app).post(
            "/documents",
            files={"file": ("../../windows\\invoice.pdf", b"%PDF-1.4", "application/pdf")},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_document_processing_queue, None)

    assert response.status_code == 201
    assert response.json()["original_filename"] == "invoice.pdf"
    assert database.documents[0].storage_location != "invoice.pdf"
    assert (tmp_path / database.documents[0].storage_location).is_file()


def test_file_endpoint_rejects_stored_path_outside_storage_root(
    tmp_path: Path, monkeypatch, authenticated_user: User
) -> None:
    database = FakeDatabase()
    document_id = uuid.uuid4()
    now = datetime.now(UTC)
    database.documents.append(
        Document(
            id=document_id,
            owner_id=authenticated_user.id,
            original_filename="invoice.pdf",
            mime_type="application/pdf",
            file_size=1,
            checksum="a" * 64,
            storage_location="../outside.pdf",
            status=DocumentStatus.COMPLETED,
            created_at=now,
            updated_at=now,
        )
    )
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    app.dependency_overrides[get_db] = lambda: database
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            f"/documents/{document_id}/file"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 500
    assert response.json() == {"detail": "Stored file location is invalid."}
    assert str(tmp_path) not in response.text
