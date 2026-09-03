import hashlib
import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.orm import Session, selectinload

from apps.api.app.auth import get_current_editor, get_current_user
from apps.api.app.database import get_db
from apps.api.app.models import (
    Document,
    DocumentStatus,
    InvalidDocumentStatusTransition,
    InvoiceExtraction,
    User,
)
from apps.api.app.queue import (
    DocumentProcessingQueue,
    QueueOperationError,
    get_document_processing_queue,
)
from apps.api.app.rate_limit import RateLimiter, client_identifier, get_rate_limiter
from apps.api.app.settings import settings

router = APIRouter(prefix="/documents", tags=["documents"])

SUPPORTED_MIME_TYPES = {
    "application/pdf": ".pdf",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "image/webp": ".webp",
}
CHUNK_SIZE = 1024 * 1024
logger = logging.getLogger(__name__)


class Committer(Protocol):
    def commit(self) -> None: ...


class QueuePublisher(Protocol):
    def enqueue(self, document_id: uuid.UUID) -> None: ...


def _commit_queued_then_publish(
    document: Document,
    database: Committer,
    queue: QueuePublisher,
) -> None:
    """Publish only after QUEUED is durable and visible to another DB session."""
    if document.status is not DocumentStatus.QUEUED:
        raise RuntimeError("Processing jobs may only be published for queued documents.")
    # Critical cross-system invariant: commit PostgreSQL before Redis publication.
    database.commit()
    queue.enqueue(document.id)


class DocumentUploadResponse(BaseModel):
    id: uuid.UUID
    original_filename: str
    mime_type: str
    file_size: int
    status: DocumentStatus


class DocumentListItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    mime_type: str
    file_size: int
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime


class PaginatedDocumentListResponse(BaseModel):
    items: list[DocumentListItemResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class InvoiceLineItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    position: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal


class InvoiceExtractionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invoice_number: str | None
    invoice_date: date | None
    vendor_name: str | None
    customer_name: str | None
    currency: str | None
    subtotal: Decimal | None
    tax: Decimal | None
    total: Decimal | None
    is_valid: bool
    validation_error: str | None
    line_items: list[InvoiceLineItemResponse]


class DocumentDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    mime_type: str
    file_size: int
    status: DocumentStatus
    extracted_text: str | None
    error_details: str | None
    invoice_extraction: InvoiceExtractionResponse | None
    created_at: datetime
    updated_at: datetime


def _write_upload(upload: UploadFile, destination: Path, max_size: int) -> tuple[int, str]:
    checksum = hashlib.sha256()
    file_size = 0

    try:
        with destination.open("wb") as stored_file:
            while chunk := upload.file.read(CHUNK_SIZE):
                file_size += len(chunk)
                if file_size > max_size:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail="File exceeds the maximum allowed size.",
                    )
                checksum.update(chunk)
                stored_file.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except OSError as error:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to store the uploaded file.",
        ) from error

    if file_size == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file cannot be empty.",
        )

    return file_size, checksum.hexdigest()


def _safe_original_filename(filename: str | None) -> str:
    basename = (filename or "unnamed").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(character for character in basename if character.isprintable()).strip()
    return (cleaned or "unnamed")[:255]


def _validate_file_signature(path: Path, mime_type: str) -> None:
    with path.open("rb") as stored_file:
        header = stored_file.read(16)
    valid = {
        "application/pdf": header.startswith(b"%PDF-"),
        "image/jpeg": header.startswith(b"\xff\xd8\xff"),
        "image/jpg": header.startswith(b"\xff\xd8\xff"),
        "image/png": header.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/bmp": header.startswith(b"BM"),
        "image/gif": header.startswith((b"GIF87a", b"GIF89a")),
        "image/tiff": header.startswith((b"II*\x00", b"MM\x00*")),
        "image/webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
    }.get(mime_type, False)
    if not valid:
        path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File content does not match the declared file type.",
        )


@router.post("", response_model=DocumentUploadResponse, status_code=status.HTTP_201_CREATED)
def upload_document(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
    queue: DocumentProcessingQueue = Depends(get_document_processing_queue),  # noqa: B008
    current_user: User = Depends(get_current_editor),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> Document:
    limiter.check(
        "upload",
        f"{current_user.id}:{client_identifier(request)}",
        settings.upload_rate_limit,
        settings.upload_rate_window_seconds,
    )
    mime_type = file.content_type or ""
    extension = SUPPORTED_MIME_TYPES.get(mime_type)
    if extension is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported MIME type.",
        )

    document_id = uuid.uuid4()
    relative_location = f"{document_id}{extension}"
    storage_directory = settings.storage_path
    storage_directory.mkdir(parents=True, exist_ok=True)
    destination = storage_directory / relative_location
    file_size, checksum = _write_upload(file, destination, settings.max_upload_size)
    _validate_file_signature(destination, mime_type)

    document = Document(
        id=document_id,
        original_filename=_safe_original_filename(file.filename),
        mime_type=mime_type,
        file_size=file_size,
        checksum=checksum,
        storage_location=relative_location,
        owner_id=current_user.id,
        status=DocumentStatus.UPLOADED,
    )
    try:
        database.add(document)
        document.transition_to(DocumentStatus.QUEUED)
        _commit_queued_then_publish(document, database, queue)
    except QueueOperationError:
        database.delete(document)
        database.commit()
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to enqueue document for processing.",
        ) from None
    except Exception:
        logger.exception("document_upload_failed")
        database.rollback()
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to persist the uploaded document.",
        ) from None

    return document


@router.get("", response_model=PaginatedDocumentListResponse)
def list_documents(
    page: int = Query(default=1, ge=1),  # noqa: B008
    page_size: int = Query(default=20, ge=1, le=100),  # noqa: B008
    status: DocumentStatus | None = Query(default=None),  # noqa: B008
    created_after: datetime | None = Query(default=None),  # noqa: B008
    created_before: datetime | None = Query(default=None),  # noqa: B008
    is_valid: bool | None = Query(default=None),  # noqa: B008
    mime_type: str | None = Query(default=None, max_length=255),  # noqa: B008
    search: str | None = Query(default=None, min_length=1, max_length=200),  # noqa: B008
    sort_by: Literal["created_at", "filename", "status", "invoice_date"] = Query(  # noqa: B008
        default="created_at"
    ),
    sort_direction: Literal["asc", "desc"] = Query(default="desc"),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> PaginatedDocumentListResponse:
    base_statement = select(Document).where(Document.owner_id == current_user.id)
    count_statement = select(func.count(Document.id)).where(Document.owner_id == current_user.id)
    invoice_joined = False

    if status is not None:
        base_statement = base_statement.where(Document.status == status)
        count_statement = count_statement.where(Document.status == status)

    if created_after is not None:
        base_statement = base_statement.where(Document.created_at >= created_after)
        count_statement = count_statement.where(Document.created_at >= created_after)

    if created_before is not None:
        base_statement = base_statement.where(Document.created_at <= created_before)
        count_statement = count_statement.where(Document.created_at <= created_before)

    if is_valid is not None:
        base_statement = base_statement.join(
            InvoiceExtraction, InvoiceExtraction.document_id == Document.id
        ).where(InvoiceExtraction.is_valid == is_valid)
        count_statement = count_statement.join(
            InvoiceExtraction, InvoiceExtraction.document_id == Document.id
        ).where(InvoiceExtraction.is_valid == is_valid)
        invoice_joined = True

    if mime_type is not None:
        base_statement = base_statement.where(Document.mime_type == mime_type)
        count_statement = count_statement.where(Document.mime_type == mime_type)

    if search is not None:
        term = f"%{search.strip()}%"
        if not invoice_joined:
            base_statement = base_statement.outerjoin(
                InvoiceExtraction, InvoiceExtraction.document_id == Document.id
            )
            count_statement = count_statement.outerjoin(
                InvoiceExtraction, InvoiceExtraction.document_id == Document.id
            )
            invoice_joined = True
        search_condition = or_(
            Document.original_filename.ilike(term),
            InvoiceExtraction.invoice_number.ilike(term),
            InvoiceExtraction.vendor_name.ilike(term),
            InvoiceExtraction.customer_name.ilike(term),
        )
        base_statement = base_statement.where(search_condition)
        count_statement = count_statement.where(search_condition)

    total = database.scalar(count_statement) or 0
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    offset = (page - 1) * page_size

    sort_columns = {
        "created_at": Document.created_at,
        "filename": Document.original_filename,
        "status": Document.status,
        "invoice_date": InvoiceExtraction.invoice_date,
    }
    if sort_by == "invoice_date" and not invoice_joined:
        base_statement = base_statement.outerjoin(
            InvoiceExtraction, InvoiceExtraction.document_id == Document.id
        )
    direction = asc if sort_direction == "asc" else desc
    items_statement = (
        base_statement.order_by(direction(sort_columns[sort_by]), direction(Document.id))
        .limit(page_size)
        .offset(offset)
    )
    documents = list(database.scalars(items_statement).all())
    items = [DocumentListItemResponse.model_validate(doc) for doc in documents]

    return PaginatedDocumentListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/{document_id}", response_model=DocumentDetailResponse)
def get_document(
    document_id: uuid.UUID,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> Document:
    document = database.scalar(
        select(Document)
        .options(
            selectinload(Document.invoice_extraction).selectinload(InvoiceExtraction.line_items)
        )
        .where(Document.id == document_id, Document.owner_id == current_user.id)
    )
    if document is None:
        logger.warning(
            "document_access_denied_or_missing action=detail user_id=%s document_id=%s",
            current_user.id,
            document_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )
    return document


@router.get("/{document_id}/file")
def get_document_file(
    document_id: uuid.UUID,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> FileResponse:
    document = database.scalar(
        select(Document).where(Document.id == document_id, Document.owner_id == current_user.id)
    )
    if document is None:
        logger.warning(
            "document_access_denied_or_missing action=file user_id=%s document_id=%s",
            current_user.id,
            document_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    stored_file = _stored_file_path(document.storage_location)
    if not stored_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored document file is missing or unavailable.",
        )

    return FileResponse(
        path=stored_file,
        media_type=document.mime_type,
        filename=document.original_filename,
        content_disposition_type="inline",
    )


@router.post("/{document_id}/reprocess", response_model=DocumentDetailResponse)
def reprocess_document(
    document_id: uuid.UUID,
    database: Session = Depends(get_db),  # noqa: B008
    queue: DocumentProcessingQueue = Depends(get_document_processing_queue),  # noqa: B008
    current_user: User = Depends(get_current_editor),  # noqa: B008
) -> Document:
    document = database.scalar(
        select(Document).where(Document.id == document_id, Document.owner_id == current_user.id)
    )
    if document is None:
        logger.warning(
            "document_access_denied_or_missing action=reprocess user_id=%s document_id=%s",
            current_user.id,
            document_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    if document.status == DocumentStatus.QUEUED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document is already queued for processing.",
        )

    stored_file = _stored_file_path(document.storage_location)
    if not stored_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored document file is missing or unavailable.",
        )

    previous_status = document.status
    try:
        document.transition_to(DocumentStatus.QUEUED)
        _commit_queued_then_publish(document, database, queue)
    except InvalidDocumentStatusTransition:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot reprocess document with status '{document.status.value}'.",
        ) from None

    except QueueOperationError:
        document.status = previous_status
        database.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to enqueue document for processing.",
        ) from None

    return document


def _stored_file_path(storage_location: str) -> Path:
    storage_root = settings.storage_path.resolve()
    stored_file = (storage_root / storage_location).resolve()
    try:
        stored_file.relative_to(storage_root)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored file location is invalid.",
        ) from error
    return stored_file


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: uuid.UUID,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_editor),  # noqa: B008
) -> None:
    document = database.scalar(
        select(Document).where(Document.id == document_id, Document.owner_id == current_user.id)
    )
    if document is None:
        logger.warning(
            "document_access_denied_or_missing action=delete user_id=%s document_id=%s",
            current_user.id,
            document_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    stored_file = _stored_file_path(document.storage_location)
    quarantined_file: Path | None = None
    try:
        if stored_file.exists():
            quarantine_name = f".{stored_file.name}.{uuid.uuid4().hex}.deleting"
            quarantined_file = stored_file.with_name(quarantine_name)
            stored_file.replace(quarantined_file)
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to remove the stored document file.",
        ) from error

    try:
        database.delete(document)
        database.commit()
    except Exception:
        database.rollback()
        if quarantined_file is not None:
            try:
                quarantined_file.replace(stored_file)
            except OSError:
                pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to delete the document record.",
        ) from None

    if quarantined_file is not None:
        try:
            quarantined_file.unlink()
        except OSError as error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Unable to remove the stored document file.",
            ) from error
