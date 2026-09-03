import uuid
from datetime import UTC, datetime

import jwt
import pytest
from fastapi.testclient import TestClient

from apps.api.app.auth import create_access_token, verify_password
from apps.api.app.database import get_db
from apps.api.app.main import app
from apps.api.app.models import Document, DocumentStatus, User, UserRole
from apps.api.app.queue import get_document_processing_queue
from apps.api.app.settings import settings


class FakeScalarResult:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class FakeDatabase:
    def __init__(self) -> None:
        self.users: list[User] = []
        self.documents: list[Document] = []

    def add(self, value) -> None:
        if isinstance(value, User):
            value.id = value.id or uuid.uuid4()
            value.is_active = True if value.is_active is None else value.is_active
            self.users.append(value)
        else:
            self.documents.append(value)

    def delete(self, value) -> None:
        self.documents.remove(value)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def refresh(self, user: User) -> None:
        now = datetime.now(UTC)
        user.created_at = now
        user.updated_at = now

    def get(self, model, identifier):
        values = self.users if model is User else self.documents
        return next((value for value in values if value.id == identifier), None)

    def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        parameters = statement.compile().params
        if entity is User:
            return next((user for user in self.users if user.email in parameters.values()), None)
        for document in self.documents:
            if document.id in parameters.values() and document.owner_id in parameters.values():
                return document
        return None

    def scalars(self, statement):
        parameters = statement.compile().params
        owner_id = next(
            (value for value in parameters.values() if isinstance(value, uuid.UUID)),
            None,
        )
        limit = statement._limit_clause.value
        offset = statement._offset_clause.value
        documents = [document for document in self.documents if document.owner_id == owner_id]
        documents.sort(key=lambda document: (document.created_at, document.id), reverse=True)
        return FakeScalarResult(documents[offset : offset + limit])


class FakeQueue:
    def enqueue(self, document_id: uuid.UUID) -> None:
        pass


@pytest.fixture
def database():
    database = FakeDatabase()
    app.dependency_overrides[get_db] = lambda: database
    yield database
    app.dependency_overrides.clear()


def register(
    client: TestClient,
    email: str = "User@Example.COM",
    password: str = "secure-password-123",
):
    return client.post("/auth/register", json={"email": email, "password": password})


def test_registration_hashes_password_and_returns_public_user(
    database: FakeDatabase,
) -> None:
    response = register(TestClient(app))

    assert response.status_code == 201
    assert response.json()["email"] == "user@example.com"
    assert "password_hash" not in response.json()
    assert len(database.users) == 1
    assert database.users[0].password_hash != "secure-password-123"
    assert verify_password("secure-password-123", database.users[0].password_hash)


def test_duplicate_email_is_rejected_after_normalization(
    database: FakeDatabase,
) -> None:
    client = TestClient(app)
    assert register(client).status_code == 201

    response = register(client, email="user@example.com")

    assert response.status_code == 409
    assert len(database.users) == 1


def test_login_returns_bearer_token_for_active_user(database: FakeDatabase) -> None:
    client = TestClient(app)
    assert register(client).status_code == 201

    response = client.post(
        "/auth/login",
        json={"email": "USER@example.com", "password": "secure-password-123"},
    )

    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    assert response.json()["access_token"]


def test_login_rejects_invalid_password_and_inactive_user(
    database: FakeDatabase,
) -> None:
    client = TestClient(app)
    assert register(client).status_code == 201

    invalid_password = client.post(
        "/auth/login", json={"email": "user@example.com", "password": "wrong-password"}
    )
    database.users[0].is_active = False
    inactive_user = client.post(
        "/auth/login",
        json={"email": "user@example.com", "password": "secure-password-123"},
    )

    assert invalid_password.status_code == 401
    assert inactive_user.status_code == 401
    assert invalid_password.json()["detail"] == inactive_user.json()["detail"]


def test_malformed_registration_and_login_credentials_are_rejected(
    database: FakeDatabase,
) -> None:
    client = TestClient(app)

    malformed_registration = client.post(
        "/auth/register", json={"email": "not-an-email", "password": "short"}
    )
    malformed_login = client.post("/auth/login", json={"email": "not-an-email", "password": ""})

    assert malformed_registration.status_code == 422
    assert malformed_login.status_code == 422


def test_document_routes_require_authentication(database: FakeDatabase) -> None:
    response = TestClient(app).get("/documents")

    assert response.status_code == 401


def test_document_access_is_owner_scoped(database: FakeDatabase) -> None:
    owner = User(
        id=uuid.uuid4(),
        email="owner@example.com",
        password_hash="unused",
        is_active=True,
    )
    other = User(
        id=uuid.uuid4(),
        email="other@example.com",
        password_hash="unused",
        is_active=True,
    )
    document = Document(
        id=uuid.uuid4(),
        owner_id=owner.id,
        original_filename="owner.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum="a" * 64,
        storage_location="owner.pdf",
        status=DocumentStatus.UPLOADED,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    database.users.extend([owner, other])
    database.documents.append(document)
    client = TestClient(app)

    own_response = client.get(
        f"/documents/{document.id}",
        headers={"Authorization": f"Bearer {create_access_token(owner.id)}"},
    )
    other_response = client.get(
        f"/documents/{document.id}",
        headers={"Authorization": f"Bearer {create_access_token(other.id)}"},
    )
    delete_response = client.delete(
        f"/documents/{document.id}",
        headers={"Authorization": f"Bearer {create_access_token(other.id)}"},
    )

    assert own_response.status_code == 200
    assert other_response.status_code == 404
    assert delete_response.status_code == 404
    assert database.documents == [document]


def test_document_listing_and_upload_are_owner_scoped(database: FakeDatabase, tmp_path) -> None:
    owner = User(
        id=uuid.uuid4(),
        email="owner@example.com",
        password_hash="unused",
        is_active=True,
    )
    other = User(
        id=uuid.uuid4(),
        email="other@example.com",
        password_hash="unused",
        is_active=True,
    )
    database.users.extend([owner, other])
    for user, filename in ((owner, "owner.pdf"), (other, "other.pdf")):
        database.documents.append(
            Document(
                id=uuid.uuid4(),
                owner_id=user.id,
                original_filename=filename,
                mime_type="application/pdf",
                file_size=1,
                checksum="a" * 64,
                storage_location=filename,
                status=DocumentStatus.UPLOADED,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    app.dependency_overrides[get_document_processing_queue] = lambda: FakeQueue()
    previous_path = settings.storage_path
    settings.storage_path = tmp_path
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_access_token(owner.id)}"}
    try:
        listed = client.get("/documents", headers=headers)
        uploaded = client.post(
            "/documents",
            headers=headers,
            files={"file": ("new.pdf", b"%PDF-1.4 invoice", "application/pdf")},
        )
    finally:
        settings.storage_path = previous_path

    assert [item["original_filename"] for item in listed.json()["items"]] == ["owner.pdf"]
    assert uploaded.status_code == 201
    assert database.documents[-1].owner_id == owner.id
    assert "storage_location" not in uploaded.json()


def test_inactive_token_cannot_access_documents(database: FakeDatabase) -> None:
    user = User(
        id=uuid.uuid4(),
        email="inactive@example.com",
        password_hash="unused",
        is_active=False,
    )
    database.users.append(user)

    response = TestClient(app).get(
        "/documents",
        headers={"Authorization": f"Bearer {create_access_token(user.id)}"},
    )

    assert response.status_code == 401


def test_line_item_positions_are_assigned_in_extraction_order() -> None:
    from apps.api.app.invoice_extractor import extract_invoice_fields

    extraction = extract_invoice_fields("First | 1 | 2.00 | 2.00\nSecond | 1 | 3.00 | 3.00")
    for position, line_item in enumerate(extraction.line_items):
        line_item.position = position

    assert [line_item.position for line_item in extraction.line_items] == [0, 1]


def test_get_me_returns_authenticated_user_profile(database: FakeDatabase) -> None:
    user = User(
        id=uuid.uuid4(),
        email="me@example.com",
        password_hash="hashed-pw",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    database.users.append(user)
    token = create_access_token(user.id)

    response = TestClient(app).get(
        "/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == str(user.id)
    assert payload["email"] == "me@example.com"
    assert payload["is_active"] is True
    assert "password_hash" not in payload
    assert "password" not in payload
    assert "created_at" in payload
    assert "updated_at" in payload


def test_get_me_rejects_unauthenticated_request() -> None:
    response = TestClient(app).get("/auth/me")
    assert response.status_code == 401


def test_get_me_rejects_inactive_user(database: FakeDatabase) -> None:
    user = User(
        id=uuid.uuid4(),
        email="inactive-me@example.com",
        password_hash="hashed-pw",
        is_active=False,
    )
    database.users.append(user)
    token = create_access_token(user.id)

    response = TestClient(app).get(
        "/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_expired_and_malformed_tokens_are_rejected(database: FakeDatabase, caplog) -> None:
    user = User(id=uuid.uuid4(), email="token@example.com", password_hash="unused", is_active=True)
    database.users.append(user)
    expired = jwt.encode(
        {"sub": str(user.id), "exp": datetime(2020, 1, 1, tzinfo=UTC)},
        settings.auth_secret_key.get_secret_value(),
        algorithm="HS256",
    )
    client = TestClient(app)

    with caplog.at_level("WARNING"):
        expired_response = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
        malformed_response = client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})

    assert expired_response.status_code == 401
    assert malformed_response.status_code == 401
    assert expired_response.json() == malformed_response.json() == {"detail": "Not authenticated."}
    assert expired not in caplog.text
    assert "not-a-jwt" not in caplog.text


def test_viewer_can_read_but_cannot_mutate_owned_document(database: FakeDatabase, tmp_path) -> None:
    viewer = User(
        id=uuid.uuid4(),
        email="viewer@example.com",
        password_hash="unused",
        is_active=True,
        role=UserRole.VIEWER,
    )
    document = Document(
        id=uuid.uuid4(),
        owner_id=viewer.id,
        original_filename="owned.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum="a" * 64,
        storage_location="owned.pdf",
        status=DocumentStatus.COMPLETED,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    database.users.append(viewer)
    database.documents.append(document)
    (tmp_path / "owned.pdf").write_bytes(b"%PDF-1.4")
    previous_path = settings.storage_path
    settings.storage_path = tmp_path
    headers = {"Authorization": f"Bearer {create_access_token(viewer.id)}"}
    client = TestClient(app)
    try:
        detail = client.get(f"/documents/{document.id}", headers=headers)
        reprocess = client.post(f"/documents/{document.id}/reprocess", headers=headers)
        delete = client.delete(f"/documents/{document.id}", headers=headers)
        upload = client.post(
            "/documents",
            headers=headers,
            files={"file": ("new.pdf", b"%PDF-1.4", "application/pdf")},
        )
    finally:
        settings.storage_path = previous_path

    assert detail.status_code == 200
    assert {reprocess.status_code, delete.status_code, upload.status_code} == {403}
    assert database.documents == [document]
