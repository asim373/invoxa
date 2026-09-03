import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import BinaryIO

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from apps.api.app.auth import get_current_user
from apps.api.app.database import get_db
from apps.api.app.export_service import (
    ExportDocumentsNotFoundError,
    create_csv_export,
    create_json_export,
    create_xlsx_export,
)
from apps.api.app.models import User

router = APIRouter(tags=["exports"])
logger = logging.getLogger(__name__)
DOWNLOAD_CHUNK_SIZE = 64 * 1024


# ---------------------------------------------------------------------------
# CSV export endpoints
# ---------------------------------------------------------------------------


class CsvExportRequest(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


@router.get("/documents/{document_id}/exports/csv")
def export_document_csv(
    document_id: uuid.UUID,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> StreamingResponse:
    return _csv_export_response(database, current_user, [document_id])


@router.post("/exports/csv")
def export_documents_csv(
    request: CsvExportRequest,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> StreamingResponse:
    return _csv_export_response(database, current_user, request.document_ids)


def _csv_export_response(
    database: Session, current_user: User, document_ids: list[uuid.UUID]
) -> StreamingResponse:
    try:
        export_file = create_csv_export(database, current_user.id, document_ids)
    except ExportDocumentsNotFoundError:
        logger.warning("export_denied_or_missing format=csv user_id=%s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found."
        ) from None

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return StreamingResponse(
        _stream_file(export_file),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="invoice-export-{timestamp}.zip"'},
    )


# ---------------------------------------------------------------------------
# XLSX export endpoints
# ---------------------------------------------------------------------------

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class XlsxExportRequest(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


@router.get("/documents/{document_id}/exports/xlsx")
def export_document_xlsx(
    document_id: uuid.UUID,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> StreamingResponse:
    return _xlsx_export_response(database, current_user, [document_id])


@router.post("/exports/xlsx")
def export_documents_xlsx(
    request: XlsxExportRequest,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> StreamingResponse:
    return _xlsx_export_response(database, current_user, request.document_ids)


def _xlsx_export_response(
    database: Session, current_user: User, document_ids: list[uuid.UUID]
) -> StreamingResponse:
    try:
        export_file = create_xlsx_export(database, current_user.id, document_ids)
    except ExportDocumentsNotFoundError:
        logger.warning("export_denied_or_missing format=xlsx user_id=%s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found."
        ) from None

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return StreamingResponse(
        _stream_file(export_file),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="invoice-export-{timestamp}.xlsx"'},
    )


class JsonExportRequest(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


@router.get("/documents/{document_id}/exports/json")
def export_document_json(
    document_id: uuid.UUID,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> JSONResponse:
    return _json_export_response(database, current_user, [document_id])


@router.post("/exports/json")
def export_documents_json(
    request: JsonExportRequest,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> JSONResponse:
    return _json_export_response(database, current_user, request.document_ids)


def _json_export_response(
    database: Session, current_user: User, document_ids: list[uuid.UUID]
) -> JSONResponse:
    try:
        payload = create_json_export(database, current_user.id, document_ids)
    except ExportDocumentsNotFoundError:
        logger.warning("export_denied_or_missing format=json user_id=%s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found."
        ) from None

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="invoice-export-{timestamp}.json"'},
    )


# ---------------------------------------------------------------------------
# Shared streaming helper
# ---------------------------------------------------------------------------


def _stream_file(export_file: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := export_file.read(DOWNLOAD_CHUNK_SIZE):
            yield chunk
    finally:
        export_file.close()
