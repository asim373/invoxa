import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy import select

from apps.api.app.invoice_extractor import extract_invoice_fields
from apps.api.app.models import (
    Document,
    DocumentStatus,
    ProcessingOperation,
    ProcessingOperationStatus,
)
from apps.api.app.queue import DocumentProcessingQueue, QueueOperationError
from apps.api.app.settings import settings
from apps.api.app.text_extraction import (
    DocumentTextExtractionError,
    DocumentTextExtractor,
    PdfTextExtractor,
)
from apps.api.app.usage import account_successful_processing, mark_processing_failed

logger = logging.getLogger(__name__)
MAX_PROCESSING_ATTEMPTS = 3


class WorkerOperationError(RuntimeError):
    pass


class DocumentDatabase(Protocol):
    def get(self, entity: type[Any], ident: uuid.UUID, /) -> Any: ...

    def scalar(self, statement) -> Any: ...

    def add(self, instance: Any) -> None: ...

    def flush(self) -> None: ...

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
        analyzer: Callable[[Any, Document], object] | None = None,
    ) -> None:
        self.queue = queue
        self.database = database
        self.pdf_extractor = pdf_extractor or PdfTextExtractor(self._default_ocr_extractor())
        self.image_extractor = image_extractor or self._default_ocr_extractor()
        self.analyzer = analyzer

    def process_once(self) -> bool:
        payload = self.queue.acquire()
        if payload is None:
            return False

        try:
            if not self._record_attempt(payload):
                self.queue.acknowledge(payload)
                return False
            result = self._process_payload(payload)
        except Exception:
            self.queue.release(payload)
            raise
        self.queue.acknowledge(payload)
        return result

    def _record_attempt(self, payload: str | bytes) -> bool:
        job = self._parse_job(payload)
        if job is None or len(job) < 3 or not hasattr(self.database, "flush"):
            return True
        operation = self.database.get(ProcessingOperation, job[2])
        if operation is None:
            return True
        operation.retry_attempts += 1
        if operation.retry_attempts > MAX_PROCESSING_ATTEMPTS:
            document = self.database.get(Document, job[0])
            operation.status = ProcessingOperationStatus.FAILED
            operation.failed_at = datetime.now(UTC)
            operation.error_details = "Processing retry limit exhausted."
            if document is not None and document.status is DocumentStatus.QUEUED:
                document.transition_to(DocumentStatus.PROCESSING)
                document.transition_to(DocumentStatus.FAILED)
                document.error_details = "Processing retry limit exhausted."
            self.database.commit()
            return False
        self.database.commit()
        return True

    def _process_payload(self, payload: str | bytes) -> bool:

        job = self._parse_job(payload)
        if job is None:
            logger.warning("document_job_invalid payload=%r", payload)
            return False
        document_id, job_organization_id = job[:2]
        operation_id = job[2] if len(job) == 3 else None

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

        if document.organization_id != job_organization_id:
            logger.warning("document_job_tenant_mismatch document_id=%s", document_id)
            return False

        try:
            document.transition_to(DocumentStatus.PROCESSING)
            if operation_id is not None and hasattr(self.database, "add"):
                operation = self.database.get(ProcessingOperation, operation_id)
                if (
                    operation is None
                    or operation.document_id != document.id
                    or operation.organization_id != document.organization_id
                ):
                    self.database.rollback()
                    return False
                operation.status = ProcessingOperationStatus.PROCESSING
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
            if operation_id is not None and hasattr(self.database, "add"):
                account_successful_processing(cast(Any, self.database), document, operation_id)
            self.database.commit()
            if document.organization_id is not None and hasattr(self.database, "scalars"):
                from apps.api.app.api_access import emit_document_event

                emit_document_event(
                    cast(Any, self.database),
                    document.organization_id,
                    "document.processing.completed",
                    {"document_id": str(document.id), "status": document.status.value},
                )
            logger.info("document_processing_completed document_id=%s", document_id)
        except DocumentTextExtractionError as error:
            self.database.rollback()
            self._mark_failed(document, str(error), operation_id)
            return False
        except Exception:
            self.database.rollback()
            logger.exception("document_processing_unexpected_failure document_id=%s", document_id)
            self._mark_failed(document, "Unexpected document processing failure.", operation_id)
            return False

        if self.analyzer is not None:
            try:
                self.analyzer(self.database, document)
                self.database.commit()
                logger.info("ai_analysis_completed document_id=%s", document_id)
            except Exception:
                self.database.rollback()
                logger.exception("ai_analysis_failed document_id=%s", document_id)
                try:
                    from apps.api.app.models import AIAnalysisStatus

                    document = self.database.get(Document, document_id)
                    if document is not None:
                        document.ai_analysis_status = AIAnalysisStatus.FAILED
                        document.ai_analysis_error = (
                            "AI Analysis could not be completed. "
                            "The extracted document remains available."
                        )
                        self.database.commit()
                except Exception:
                    self.database.rollback()
                    logger.exception(
                        "ai_analysis_failure_state_unavailable document_id=%s", document_id
                    )

        return True

    def recover_abandoned_jobs(self) -> int:
        payloads = self.queue.inflight_jobs()
        recovered_documents = 0
        try:
            for payload in payloads:
                job = self._parse_job(payload)
                if job is None:
                    continue
                document_id, job_organization_id = job[:2]
                document = self.database.get(Document, document_id)
                if (
                    document is not None
                    and document.organization_id == job_organization_id
                    and document.status is DocumentStatus.PROCESSING
                ):
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

        enqueued = 0
        for document_id in document_ids:
            document = self.database.get(Document, document_id)
            if document is not None:
                operation_id = None
                operation_priority = "normal"
                if hasattr(self.database, "scalar"):
                    operation = self.database.scalar(
                        select(ProcessingOperation)
                        .where(
                            ProcessingOperation.document_id == document.id,
                            ProcessingOperation.status.in_(
                                {
                                    ProcessingOperationStatus.QUEUED,
                                    ProcessingOperationStatus.PROCESSING,
                                }
                            ),
                        )
                        .order_by(ProcessingOperation.created_at.desc())
                    )
                    operation_id = operation.id if operation is not None else None
                    if operation is not None:
                        operation_priority = getattr(
                            operation.priority, "value", operation.priority
                        )
                enqueued += self.queue.enqueue_if_missing(
                    document_id, document.organization_id, operation_id, operation_priority
                )
        if enqueued:
            logger.warning("document_jobs_reconciled jobs=%s", enqueued)
        return enqueued

    def _mark_failed(
        self, document: Document, error_details: str, operation_id: uuid.UUID | None = None
    ) -> None:
        try:
            document.transition_to(DocumentStatus.FAILED)
            document.error_details = error_details
            if operation_id is not None and hasattr(self.database, "add"):
                mark_processing_failed(cast(Any, self.database), operation_id, error_details)
            self.database.commit()
            if document.organization_id is not None and hasattr(self.database, "scalars"):
                from apps.api.app.api_access import emit_document_event

                emit_document_event(
                    cast(Any, self.database),
                    document.organization_id,
                    "document.processing.failed",
                    {"document_id": str(document.id), "status": document.status.value},
                )
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
        job = DocumentProcessingWorker._parse_job(payload)
        return job[0] if job is not None else None

    @staticmethod
    def _parse_job(
        payload: str | bytes,
    ) -> tuple[uuid.UUID, uuid.UUID | None] | tuple[uuid.UUID, uuid.UUID | None, uuid.UUID] | None:
        try:
            decoded_payload = payload.decode() if isinstance(payload, bytes) else payload
            data = json.loads(decoded_payload)
            document_id = uuid.UUID(data["document_id"])
            organization_id = data.get("organization_id")
            parsed_organization_id = uuid.UUID(organization_id) if organization_id else None
            operation_id = data.get("operation_id")
            if operation_id:
                return document_id, parsed_organization_id, uuid.UUID(operation_id)
            return document_id, parsed_organization_id
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None


def consume_document_job(worker: DocumentProcessingWorker) -> bool:
    try:
        return worker.process_once()
    except QueueOperationError as error:
        raise WorkerOperationError("Unable to consume document processing job.") from error


if __name__ == "__main__":
    import time

    from apps.api.app.ai_analysis import analyze_document
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
            worker = DocumentProcessingWorker(queue, database, analyzer=analyze_document)
            if (
                time.monotonic() - last_reconciliation
                >= settings.queue_reconciliation_interval_seconds
            ):
                worker.reconcile_queued_documents()
                last_reconciliation = time.monotonic()
            from apps.api.app.api_access import retry_due_webhook_deliveries

            retry_due_webhook_deliveries(cast(Any, database))
            from apps.api.app.email_outbox import process_email

            try:
                process_email(database)
            except Exception:
                database.rollback()
                logger.warning("transactional_email_worker_unavailable")
            worker.process_once()
        except Exception as error:
            logger.exception("document_worker_error error=%s", error)
        finally:
            database.close()

        time.sleep(1)
