"""Stable connector error codes (problem+json `code`)."""

from apps.core.exceptions import NexoraError


class ConnectorError(NexoraError):
    status_code = 422
    default_code = "connector_error"
    default_detail = "The data source could not be used."
    retryable = False


class InvalidHost(ConnectorError):
    default_code = "connector_host_invalid"
    default_detail = "The host is not a valid hostname or IP address."


class InvalidHostConfig(ConnectorError):
    """A malformed `config.host` in a request body: 400 with the stable `connector_host_invalid` code."""

    status_code = 400
    default_code = "connector_host_invalid"
    default_detail = "The host is not a valid hostname or IP address."

    def __init__(self, detail: str | None = None, *, code: str | None = None, extra: dict | None = None):
        message = detail or self.default_detail
        extra = {"errors": [{"field": "config.host", "code": "connector_host_invalid", "message": message}],
                 **(extra or {})}
        super().__init__(message, code=code, extra=extra)


class HostForbidden(ConnectorError):
    default_code = "connector_host_forbidden"
    default_detail = "The host resolves to an address that connectors may not reach."


class HostUnresolvable(ConnectorError):
    default_code = "connector_host_unresolvable"
    default_detail = "The host name could not be resolved."
    retryable = True


class ConnectionFailed(ConnectorError):
    default_code = "connector_connection_failed"
    default_detail = "Could not connect to the database."
    retryable = True


class AuthenticationFailed(ConnectorError):
    default_code = "connector_auth_failed"
    default_detail = "The database rejected the user name or password."


class TlsFailed(ConnectorError):
    default_code = "connector_tls_failed"
    default_detail = "The TLS connection to the database could not be established or verified."


class RoleNotReadOnly(ConnectorError):
    default_code = "connector_role_not_readonly"
    default_detail = "The database role has write or administrative privileges; use a read-only role."


class QueryFailed(ConnectorError):
    default_code = "connector_query_failed"
    default_detail = "The database query failed."


class QueryTimeout(ConnectorError):
    default_code = "connector_timeout"
    default_detail = "The database did not answer in time."
    retryable = True


class TableNotFound(ConnectorError):
    status_code = 404
    default_code = "connector_table_not_found"
    default_detail = "The table does not exist or is not readable by the connector role."


class ColumnNotFound(ConnectorError):
    default_code = "connector_column_not_found"
    default_detail = "A requested column does not exist in the table."


class SourceNotReady(ConnectorError):
    status_code = 409
    default_code = "data_source_not_verified"
    default_detail = "The data source has not passed its connection and read-only checks yet."


class SourceInUse(ConnectorError):
    status_code = 409
    default_code = "data_source_in_use"
    default_detail = "The data source is used by pipelines; remove it from them first."


class ImportTooLarge(ConnectorError):
    default_code = "import_too_large"
    default_detail = "The table is larger than the import limit."


class UnsupportedConnector(ConnectorError):
    default_code = "connector_type_unsupported"
    default_detail = "This connector type is not supported."
