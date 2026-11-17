from apps.core.exceptions import NexoraError


class UploadTooLarge(NexoraError):
    status_code = 413
    default_code = "upload_too_large"
    default_detail = "The file is larger than the upload limit."


class UploadRejected(NexoraError):
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
    default_detail = "Classifying data as synthetic or public requires attest=true."


class DatasetNotReady(NexoraError):
    status_code = 409
    default_code = "dataset_not_ready"
    default_detail = "The dataset has no ready version yet."
