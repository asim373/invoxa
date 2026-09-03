import json
import logging
import uuid
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select

from apps.api.app.invoice_extractor import extract_invoice_fields
from apps.api.app.models import Document, DocumentStatus
from apps.api.app.queue import DocumentProcessingQueue, QueueOperationError
from apps.api.app.settings import settings
from apps.api.app.text_extraction import (
    DocumentTextExtractionError,
    DocumentTextExtractor,
    PdfTextExtractor,
)

logger = logging.getLogger(__name__)


class WorkerOperationError(RuntimeError):
    pass


class DocumentDatabase(Protocol):
    def get(self, entity: type[Document], ident: uuid.UUID, /) -> Document | None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def execute(self, statement) -> Any: ...


class DocumentProcessingWorker:
    def __init__(
        self,
        queue: DocumentProcessingQueue,
        database: DocumentDatabase,
        pdf_extractor: DocumentTextExtractor | None = None,
        image_extractor: DocumentTextExtractor | None = None,
    ) -> None:
        self.queue = queue
        self.database = database
        self.pdf_extractor = pdf_extractor or PdfTextExtractor(self._default_ocr_extractor())
        self.image_extractor = image_extractor or self._default_ocr_extractor()

    def process_once(self) -> bool:
        payload = self.queue.acquire()
        if payload is None:
            return False

        try:
            result = self._process_payload(payload)
        except Exception:
            self.queue.release(payload)
            raise
        self.queue.acknowledge(payload)
        return result

    def _process_payload(self, payload: str | bytes) -> bool:

        document_id = self._parse_document_id(payload)
        if document_id is None:
            logger.warning("document_job_invalid payload=%r", payload)
            return False

        logger.info("document_job_received document_id=%s", document_id)

        try:
            document = self.database.get(Document, document_id)
        except Exception as error:
            self.database.rollback()
            raise WorkerOperationError("Unable to retrieve document for processing.") from error

        if document is None or document.status is not DocumentStatus.QUEUED:
            logger.warning(
                "document_job_skipped document_id=%s reason=%s",
                document_id,
                "missing" if document is None else f"status_{document.status.value}",
            )
            return False

        try:
            document.transition_to(DocumentStatus.PROCESSING)
            self.database.commit()
            logger.info("document_processing_started document_id=%s", document_id)
        except Exception as error:
            self.database.rollback()
            raise WorkerOperationError("Unable to mark document as processing.") from error

        try:
            storage_error = self._validate_storage(document.storage_location)
            if storage_error is not None:
                raise DocumentTextExtractionError(storage_error)

            if document.mime_type == "application/pdf":
                extractor = self.pdf_extractor
            elif document.mime_type in {
                "image/jpeg",
                "image/jpg",
                "image/png",
                "image/webp",
                "image/bmp",
                "image/gif",
                "image/tiff",
            }:
                extractor = self.image_extractor
            else:
                raise DocumentTextExtractionError(
                    "Unsupported document type; expected PDF or a supported image."
                )

            extracted_text = extractor.extract(self._stored_path(document.storage_location))
            document.extracted_text = extracted_text
            invoice_extraction = extract_invoice_fields(extracted_text)
            for position, line_item in enumerate(invoice_extraction.line_items):
                line_item.invoice_document_id = document.id
                line_item.position = position
            document.invoice_extraction = invoice_extraction
            document.transition_to(DocumentStatus.COMPLETED)
            self.database.commit()
            logger.info("document_processing_completed document_id=%s", document_id)
        except DocumentTextExtractionError as error:
            self.database.rollback()
            self._mark_failed(document, str(error))
            return False
        except Exception:
            self.database.rollback()
            logger.exception("document_processing_unexpected_failure document_id=%s", document_id)
            self._mark_failed(document, "Unexpected document processing failure.")
            return False

        return True

    def recover_abandoned_jobs(self) -> int:
        payloads = self.queue.inflight_jobs()
        recovered_documents = 0
        try:
            for payload in payloads:
                document_id = self._parse_document_id(payload)
                if document_id is None:
                    continue
                document = self.database.get(Document, document_id)
                if document is not None and document.status is DocumentStatus.PROCESSING:
                    document.transition_to(DocumentStatus.QUEUED)
                    recovered_documents += 1
            if recovered_documents:
                self.database.commit()
        except Exception as error:
            self.database.rollback()
            raise WorkerOperationError("Unable to restore abandoned processing state.") from error

        recovered_jobs = self.queue.recover_inflight()
        if recovered_jobs:
            logger.warning(
                "document_jobs_recovered jobs=%s documents_reset=%s",
                recovered_jobs,
                recovered_documents,
            )
        return recovered_jobs

    def reconcile_queued_documents(self) -> int:
        try:
            result = self.database.execute(
                select(Document.id).where(Document.status == DocumentStatus.QUEUED)
            )
            document_ids = result.scalars().all()
        except Exception as error:
            self.database.rollback()
            raise WorkerOperationError("Unable to reconcile queued documents.") from error

        enqueued = sum(self.queue.enqueue_if_missing(document_id) for document_id in document_ids)
        if enqueued:
            logger.warning("document_jobs_reconciled jobs=%s", enqueued)
        return enqueued

    def _mark_failed(self, document: Document, error_details: str) -> None:
        try:
            document.transition_to(DocumentStatus.FAILED)
            document.error_details = error_details
            self.database.commit()
            logger.error(
                "document_processing_failed document_id=%s error=%s",
                document.id,
                error_details,
            )
        except Exception as error:
            self.database.rollback()
            raise WorkerOperationError("Unable to mark document as failed.") from error

    @staticmethod
    def _validate_storage(storage_location: str) -> str | None:
        storage_root = settings.storage_path.resolve()
        stored_file = (storage_root / storage_location).resolve()
        try:
            stored_file.relative_to(storage_root)
        except ValueError:
            return "Stored document location is outside the configured storage root."

        if not stored_file.is_file():
            return "Stored document file is missing or invalid."

        try:
            with Path.open(stored_file, "rb") as stored_file_handle:
                stored_file_handle.read(1)
        except OSError:
            return "Stored document file is not readable."
        return None

    @staticmethod
    def _default_ocr_extractor() -> DocumentTextExtractor:
        from apps.api.app.text_extraction import OcrTextExtractor

        return OcrTextExtractor()

    @staticmethod
    def _stored_path(storage_location: str) -> Path:
        return (settings.storage_path.resolve() / storage_location).resolve()

    @staticmethod
    def _extract_pdf_text(storage_location: str) -> str:
        return PdfTextExtractor(DocumentProcessingWorker._default_ocr_extractor()).extract(
            DocumentProcessingWorker._stored_path(storage_location)
        )

    @staticmethod
    def _parse_document_id(payload: str | bytes) -> uuid.UUID | None:
        try:
            decoded_payload = payload.decode() if isinstance(payload, bytes) else payload
            document_id = json.loads(decoded_payload)["document_id"]
            return uuid.UUID(document_id)
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None


def consume_document_job(worker: DocumentProcessingWorker) -> bool:
    try:
        return worker.process_once()
    except QueueOperationError as error:
        raise WorkerOperationError("Unable to consume document processing job.") from error


if __name__ == "__main__":
    import time

    from apps.api.app.database import SessionLocal
    from apps.api.app.logging import configure_logging

    configure_logging(settings.log_level)
    queue = DocumentProcessingQueue()

    logger.info("document_processing_worker_started")

    startup_database = SessionLocal()
    try:
        startup_worker = DocumentProcessingWorker(queue, startup_database)
        startup_worker.recover_abandoned_jobs()
        startup_worker.reconcile_queued_documents()
    except Exception:
        logger.exception("document_worker_startup_recovery_failed")
    finally:
        startup_database.close()

    last_reconciliation = time.monotonic()

    while True:
        database = SessionLocal()
        try:
            worker = DocumentProcessingWorker(queue, database)
            if (
                time.monotonic() - last_reconciliation
                >= settings.queue_reconciliation_interval_seconds
            ):
                worker.reconcile_queued_documents()
                last_reconciliation = time.monotonic()
            worker.process_once()
        except Exception as error:
            logger.exception("document_worker_error error=%s", error)
        finally:
            database.close()

        time.sleep(1)
