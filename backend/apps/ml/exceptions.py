from __future__ import annotations

from apps.core.exceptions import NexoraError


class ModelNotReady(NexoraError):
    """No production/staging version (or the requested version does not exist / has no artifact)."""

    status_code = 409
    default_code = "model_not_ready"
    default_detail = "This model has no version ready for predictions (promote one to staging or production)."


class MLInvalid(NexoraError):
    status_code = 400
    default_code = "ml_invalid"
    default_detail = "The request is not valid for this ML project."


class ProjectNotReady(NexoraError):
    status_code = 409
    default_code = "ml_not_ready"
    default_detail = "This step is not available yet."


class ArtifactUnavailable(NexoraError):
    status_code = 503
    default_code = "model_artifact_unavailable"
    default_detail = "The model artifact could not be read from storage."


class ArtifactRejected(NexoraError):
    """The stored artifact failed integrity checks (sha256 or org prefix): it is never deserialized."""

    status_code = 409
    default_code = "model_artifact_invalid"
    default_detail = "The model artifact failed its integrity check and was not loaded."


class PredictionInputInvalid(NexoraError):
    status_code = 400
    default_code = "prediction_input_invalid"
    default_detail = "The prediction input does not match the model's features."


class PolicyExcludedColumns(NexoraError):
    status_code = 403
    default_code = "ml_columns_not_allowed"
    default_detail = "Your data access policy does not allow some of the columns this needs."
