from apps.core.exceptions import NexoraError


class WarehouseError(NexoraError):
    status_code = 502
    default_code = "warehouse_error"
    default_detail = "The data warehouse could not complete the operation."


class WarehouseUnavailable(WarehouseError):
    status_code = 503
    default_code = "warehouse_unavailable"
    default_detail = "The data warehouse is temporarily unavailable."


class WarehouseTenantMismatch(WarehouseError):
    status_code = 403
    default_code = "warehouse_tenant_mismatch"
    default_detail = "Warehouse access for another organization is refused."


class WarehouseQueryTimeout(WarehouseError):
    status_code = 504
    default_code = "query_timeout"
    default_detail = "The query exceeded its time limit."


class WarehouseQueryDenied(WarehouseError):
    status_code = 403
    default_code = "query_permission_denied"
    default_detail = "The query touched data this organization cannot read."


class WarehouseQueryFailed(WarehouseError):
    status_code = 422
    default_code = "query_failed"
    default_detail = "The query failed."
