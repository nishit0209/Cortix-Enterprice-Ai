from __future__ import annotations

from apps.core.exceptions import NexoraError


class DocumentTooLarge(NexoraError):
    status_code = 413
    default_code = "upload_too_large"
    default_detail = "The file is larger than the document upload limit."


class DocumentRejected(NexoraError):
    status_code = 422
    default_code = "upload_rejected"
    default_detail = "The file was rejected."


class StorageUnavailable(NexoraError):
    status_code = 503
    default_code = "storage_unavailable"
    default_detail = "File storage is temporarily unavailable."


class AttestationRequired(NexoraError):
    status_code = 422
    default_code = "attestation_required"
    default_detail = "Classifying a document as synthetic or public requires attest=true."


class DocumentNotReady(NexoraError):
    status_code = 409
    default_code = "document_not_ready"
    default_detail = "The document is still processing or failed."


class ExtractionError(Exception):
    """Raised by the extraction pipeline; `code` is stored on the Document and reported on the job."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
