from __future__ import annotations

from uuid import UUID

from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import HasOrgPermission
from apps.core.tenancy.views import TenantAPIView

from . import services


class LineageView(TenantAPIView):
    """GET /orgs/{org}/lineage?node_type=dataset&node_id=<uuid>&depth=3"""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "datasets.view"

    def get(self, request, *args, **kwargs):
        node_type = request.query_params.get("node_type", "")
        raw_id = request.query_params.get("node_id", "")
        errors = {}
        if node_type not in services.VALID_TYPES:
            errors["node_type"] = [f"Must be one of: {', '.join(sorted(services.VALID_TYPES))}."]
        try:
            node_id = UUID(raw_id)
        except ValueError:
            errors["node_id"] = ["Must be a UUID."]
        try:
            depth = int(request.query_params.get("depth", 3))
        except ValueError:
            errors["depth"] = ["Must be an integer."]
        if errors:
            raise ValidationError(errors)
        result = services.graph(node_type, node_id, depth=depth)
        if not result["nodes"]:
            raise NotFound()  # unknown node or another org's node: 404, never 403
        return Response(result)
