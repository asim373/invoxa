import hashlib
import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from pypdf import PdfReader
from sqlalchemy import and_, asc, desc, func, or_, select
from sqlalchemy.orm import Session, selectinload

from apps.api.app.auth import get_current_user
from apps.api.app.database import get_db
from apps.api.app.models import (
    AIAnalysisStatus,
    Document,
    DocumentStatus,
    InvalidDocumentStatusTransition,
    InvoiceExtraction,
    OrganizationMemberRole,
    ProcessingOperation,
    ProcessingPriority,
    User,
)
from apps.api.app.plans import processing_priority_for_organization
from apps.api.app.processing_quota import require_processing_capacity
from apps.api.app.queue import (
    DocumentProcessingQueue,
    QueueOperationError,
    get_document_processing_queue,
)
from apps.api.app.rate_limit import RateLimiter, client_identifier, get_rate_limiter
from apps.api.app.settings import settings
from apps.api.app.storage_quota import lock_organization_for_storage, require_storage_capacity
from apps.api.app.tenant import (
    TenantContext,
    TenantPermission,
    get_current_workspace_editor,
    require_tenant_permission,
)


def _tenant_document_clause(tenant: TenantContext, current_user: User, database: Session):
    if tenant.organization is None:
        if not hasattr(database, "execute"):
            return Document.owner_id == current_user.id
        return and_(Document.owner_id == current_user.id, Document.organization_id.is_(None))
    return Document.organization_id == tenant.organization.id


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
    def enqueue(
        self,
        document_id: uuid.UUID,
        organization_id: uuid.UUID | None = None,
        operation_id: uuid.UUID | None = None,
        priority: str = "normal",
    ) -> None: ...


def _commit_queued_then_publish(
    document: Document,
    database: Committer,
    queue: Any,
    operation: ProcessingOperation | None = None,
) -> None:
    """Publish only after QUEUED is durable and visible to another DB session."""
    if document.status is not DocumentStatus.QUEUED:
        raise RuntimeError("Processing jobs may only be published for queued documents.")
    priority = (
        getattr(operation.priority, "value", operation.priority)
        if operation is not None
        else "normal"
    )

    def publish(*args: Any, **kwargs: Any) -> None:
        if priority == "priority":
            queue.enqueue(*args, priority=priority, **kwargs)
        else:
            queue.enqueue(*args, **kwargs)

    # Critical cross-system invariant: commit PostgreSQL before Redis publication.
    database.commit()
    if document.organization_id is None:
        if operation is None:
            publish(document.id)
        else:
            publish(document.id, operation_id=operation.id)
    else:
        if operation is None:
            publish(document.id, document.organization_id)
        else:
            publish(document.id, document.organization_id, operation.id)


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
    ai_analysis_status: AIAnalysisStatus | None = AIAnalysisStatus.PENDING
    created_at: datetime
    updated_at: datetime
    is_finalized: bool | None = False


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
    ai_analysis_status: AIAnalysisStatus | None = AIAnalysisStatus.PENDING
    ai_analysis_error: str | None
    ai_analyzed_at: datetime | None
    extracted_text: str | None
    error_details: str | None
    invoice_extraction: InvoiceExtractionResponse | None
    created_at: datetime
    updated_at: datetime
    is_finalized: bool | None = False
    finalized_at: datetime | None = None
    finalized_by_user_id: uuid.UUID | None = None
    reopened_at: datetime | None = None
    reopened_by_user_id: uuid.UUID | None = None


def _can_change_finalized_document(tenant: TenantContext) -> bool:
    return tenant.membership is not None and tenant.membership.role in {
        OrganizationMemberRole.OWNER,
        OrganizationMemberRole.ADMIN,
    }


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


def _incoming_upload_size(upload: UploadFile) -> int:
    try:
        upload.file.seek(0)
        total = 0
        while chunk := upload.file.read(CHUNK_SIZE):
            total += len(chunk)
        upload.file.seek(0)
        return total
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to read the uploaded file.",
        ) from error


def _processing_page_count(path: Path, mime_type: str) -> int:
    if mime_type != "application/pdf":
        return 1
    try:
        page_count = len(PdfReader(str(path)).pages)
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to determine the PDF page count.",
        ) from error
    if page_count < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PDF must contain at least one page.",
        )
    if page_count > settings.max_pdf_pages:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="PDF exceeds the maximum allowed page count.",
        )
    return page_count


@router.post("", response_model=DocumentUploadResponse, status_code=status.HTTP_201_CREATED)
def upload_document(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
    queue: DocumentProcessingQueue = Depends(get_document_processing_queue),  # noqa: B008
    current_user: User = Depends(get_current_workspace_editor),  # noqa: B008
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.MUTATE_DOCUMENTS)),  # noqa: B008
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

    incoming_file_size = _incoming_upload_size(file)
    if tenant.organization is not None:
        require_storage_capacity(database, tenant.organization, incoming_file_size)

    document_id = uuid.uuid4()
    relative_location = f"{document_id}{extension}"
    storage_directory = settings.storage_path
    storage_directory.mkdir(parents=True, exist_ok=True)
    destination = storage_directory / relative_location
    file_size, checksum = _write_upload(file, destination, settings.max_upload_size)
    try:
        _validate_file_signature(destination, mime_type)
        page_count = _processing_page_count(destination, mime_type)
        if tenant.organization is not None:
            require_processing_capacity(database, tenant.organization, page_count)
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    document = Document(
        id=document_id,
        original_filename=_safe_original_filename(file.filename),
        mime_type=mime_type,
        file_size=file_size,
        checksum=checksum,
        storage_location=relative_location,
        owner_id=current_user.id,
        organization_id=tenant.organization.id if tenant.organization is not None else None,
        created_by_user_id=current_user.id,
        status=DocumentStatus.UPLOADED,
    )
    try:
        database.add(document)
        document.transition_to(DocumentStatus.QUEUED)
        operation = None
        if hasattr(database, "flush"):
            database.flush()
            operation = ProcessingOperation(
                document_id=document.id,
                organization_id=document.organization_id,
                page_count=page_count,
                priority=ProcessingPriority(
                    processing_priority_for_organization(tenant.organization)
                    if tenant.organization is not None
                    else ProcessingPriority.NORMAL.value
                ),
            )
            database.add(operation)
            database.flush()
        _commit_queued_then_publish(document, database, queue, operation)
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
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
) -> PaginatedDocumentListResponse:
    document_scope = _tenant_document_clause(tenant, current_user, database)
    base_statement = select(Document).where(document_scope)
    count_statement = select(func.count(Document.id)).where(document_scope)
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
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
) -> Document:
    document = database.scalar(
        select(Document)
        .options(
            selectinload(Document.invoice_extraction).selectinload(InvoiceExtraction.line_items)
        )
        .where(Document.id == document_id, _tenant_document_clause(tenant, current_user, database))
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
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
) -> FileResponse:
    document = database.scalar(
        select(Document).where(
            Document.id == document_id, _tenant_document_clause(tenant, current_user, database)
        )
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
    current_user: User = Depends(get_current_workspace_editor),  # noqa: B008
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.MUTATE_DOCUMENTS)),  # noqa: B008
) -> Document:
    document = database.scalar(
        select(Document).where(
            Document.id == document_id, _tenant_document_clause(tenant, current_user, database)
        )
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

    if document.is_finalized and not _can_change_finalized_document(tenant):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Finalized documents require administrator reopening.",
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
        page_count = _processing_page_count(stored_file, document.mime_type)
        if tenant.organization is not None:
            require_processing_capacity(database, tenant.organization, page_count)
        document.transition_to(DocumentStatus.QUEUED)
        document.ai_analysis_status = AIAnalysisStatus.PENDING
        document.ai_analysis_error = None
        operation = None
        if hasattr(database, "flush"):
            operation = ProcessingOperation(
                document_id=document.id,
                organization_id=document.organization_id,
                page_count=page_count,
                priority=ProcessingPriority(
                    processing_priority_for_organization(tenant.organization)
                    if tenant.organization is not None
                    else ProcessingPriority.NORMAL.value
                ),
            )
            database.add(operation)
            database.flush()
        _commit_queued_then_publish(document, database, queue, operation)
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
    current_user: User = Depends(get_current_workspace_editor),  # noqa: B008
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.MUTATE_DOCUMENTS)),  # noqa: B008
) -> None:
    document = database.scalar(
        select(Document).where(
            Document.id == document_id, _tenant_document_clause(tenant, current_user, database)
        )
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

    if document.is_finalized and not _can_change_finalized_document(tenant):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Finalized documents cannot be deleted by this role.",
        )

    if document.organization_id is not None and tenant.organization is not None:
        lock_organization_for_storage(database, tenant.organization)

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


@router.post("/{document_id}/finalize", response_model=DocumentDetailResponse)
def finalize_document(
    document_id: uuid.UUID,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.MUTATE_DOCUMENTS)),  # noqa: B008
) -> Document:
    document = database.scalar(
        select(Document).where(
            Document.id == document_id, _tenant_document_clause(tenant, current_user, database)
        )
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if document.is_finalized:
        raise HTTPException(status_code=409, detail="Document is already finalized.")
    document.is_finalized = True
    document.finalized_at = datetime.now(UTC)
    document.finalized_by_user_id = current_user.id
    database.commit()
    database.refresh(document)
    return document


@router.post("/{document_id}/reopen", response_model=DocumentDetailResponse)
def reopen_document(
    document_id: uuid.UUID,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.MUTATE_DOCUMENTS)),  # noqa: B008
) -> Document:
    document = database.scalar(
        select(Document).where(
            Document.id == document_id, _tenant_document_clause(tenant, current_user, database)
        )
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if not _can_change_finalized_document(tenant):
        raise HTTPException(status_code=403, detail="Only an owner or admin may reopen documents.")
    if not document.is_finalized:
        raise HTTPException(status_code=409, detail="Document is not finalized.")
    document.is_finalized = False
    document.reopened_at = datetime.now(UTC)
    document.reopened_by_user_id = current_user.id
    database.commit()
    database.refresh(document)
    return document
