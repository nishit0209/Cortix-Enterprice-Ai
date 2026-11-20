"""Organization, membership, invitation, role, AI-policy and activity endpoints."""

from __future__ import annotations

from django.contrib.auth import login
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import mixins, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.services import find_user_by_email, user_payload, users_by_id
from apps.core.auth import CsrfEnforcedSessionAuthentication, enforce_csrf, require_password
from apps.core.exceptions import Conflict, NexoraError
from apps.core.models import AuditEvent
from apps.core.permissions import DenyApiKey, HasOrgPermission
from apps.core.tenancy import tenant_context
from apps.core.tenancy.views import TenantAPIView, TenantGenericViewSet, resolve_membership

from . import ai_consent, services
from .models import Invitation, Membership, Permission, Role
from .serializers import (
    ConsentSerializer,
    InvitationAcceptSerializer,
    InviteSerializer,
    MemberUpdateSerializer,
    OrgCreateSerializer,
    OrgUpdateSerializer,
)

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


# --- /orgs (non-tenant) --------------------------------------------------------------------------------
class OrgListCreateView(APIView):
    """GET: the caller's orgs. POST {name}: create an org; the caller becomes its owner."""

    authentication_classes = [CsrfEnforcedSessionAuthentication]  # interactive users only (no API keys)
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"items": services.user_orgs(request.user.pk), "next_cursor": None, "previous_cursor": None})

    def post(self, request):
        ser = OrgCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        org = services.create_organization(name=ser.validated_data["name"], owner=request.user, request=request)
        info = resolve_membership(request.user.pk, org.slug)
        with tenant_context(org.id, info=info):
            body = services.org_payload(org, info)
        return Response(body, status=status.HTTP_201_CREATED)


class OrgDetailView(TenantAPIView):
    """GET (org.view) / PATCH {name?, settings?} (org.settings.edit) /orgs/{org}."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "org.view", "patch": "org.settings.edit"}

    def get(self, request, org):
        return Response(services.org_payload(services.get_current_org(), request.tenant))

    def patch(self, request, org):
        ser = OrgUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        obj = services.update_organization(
            services.get_current_org(),
            name=ser.validated_data.get("name"),
            settings_patch=ser.validated_data.get("settings"),
            request=request,
        )
        return Response(services.org_payload(obj, request.tenant))


# --- members --------------------------------------------------------------------------------------------
class MemberViewSet(mixins.ListModelMixin, TenantGenericViewSet):
    permission_classes = [IsAuthenticated, DenyApiKey, HasOrgPermission]
    required_permissions = {"list": "members.view", "*": "members.manage"}
    lookup_value_regex = UUID_RE

    def get_queryset(self):
        return Membership.objects.select_related("user", "role").order_by("-created_at")

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response([services.member_payload(m) for m in page])

    def create(self, request, *args, **kwargs):
        """Invite by email (CONTRACT: POST /members {email, role})."""
        return _invite(request)

    def partial_update(self, request, *args, **kwargs):
        membership = self.get_object()
        ser = MemberUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        services.change_member(
            membership,
            actor_info=request.tenant,
            role_key=ser.validated_data.get("role"),
            status=ser.validated_data.get("status"),
            request=request,
        )
        membership.refresh_from_db()
        return Response(services.member_payload(membership))

    def destroy(self, request, *args, **kwargs):
        services.remove_member(self.get_object(), actor_info=request.tenant, request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _invite(request) -> Response:
    ser = InviteSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    inv, _token, sent = services.create_invitation(
        email=ser.validated_data["email"],
        role_key=ser.validated_data["role"],
        inviter=request.user,
        inviter_info=request.tenant,
        request=request,
    )
    body = services.invitation_payload(inv)
    body["email_sent"] = sent
    return Response(body, status=status.HTTP_201_CREATED)


# --- invitations ------------------------------------------------------------------------------------------
class InvitationViewSet(mixins.ListModelMixin, TenantGenericViewSet):
    permission_classes = [IsAuthenticated, DenyApiKey, HasOrgPermission]
    required_permissions = "members.manage"
    lookup_value_regex = UUID_RE

    def get_queryset(self):
        qs = Invitation.objects.select_related("role").order_by("-created_at")
        if self.action == "list" and self.request.query_params.get("status") != "all":
            qs = qs.filter(accepted_at__isnull=True, revoked_at__isnull=True)
        return qs

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response([services.invitation_payload(i) for i in page])

    def create(self, request, *args, **kwargs):
        return _invite(request)

    def destroy(self, request, *args, **kwargs):
        inv = self.get_object()
        if inv.accepted_at is not None:
            raise Conflict("This invitation has already been accepted.", code="invitation_accepted")
        services.revoke_invitation(inv, request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class InvitationAcceptView(APIView):
    """POST /invitations/accept {token, password?, full_name?} -> 200 {org, user}.

    * Logged-in user: joins if the invitation email matches their account.
    * Anonymous + existing account: `password` must be that account's password (then logged in).
    * Anonymous + new email: creates the account with `password` (+ `full_name`) and logs in.
    """

    authentication_classes = [CsrfEnforcedSessionAuthentication]
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        enforce_csrf(request)
        ser = InvitationAcceptSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        record = services.lookup_invitation(data["token"])
        if record is None:
            raise services.InvitationInvalid()
        user = request.user if request.user.is_authenticated else None
        with transaction.atomic():
            if user is None:
                existing = find_user_by_email(record.email)
                password = data.get("password") or ""
                if existing is not None:
                    if not existing.is_active or not existing.check_password(password):
                        raise NexoraError("Invalid email or password.", code="invalid_credentials")
                    user = existing
                else:
                    if not password:
                        raise ValidationError({"password": ["A password is required to create your account."]})
                    from apps.accounts.models import User

                    candidate = User(email=record.email.lower(), full_name=(data.get("full_name") or "").strip())
                    try:
                        validate_password(password, candidate)
                    except DjangoValidationError as exc:
                        raise ValidationError({"password": list(exc.messages)}) from exc
                    user = User.objects.create_user(record.email, password, full_name=candidate.full_name)
            org_body = services.accept_invitation(record=record, user=user, request=request)
        if not request.user.is_authenticated:
            login(request._request, user, backend="django.contrib.auth.backends.ModelBackend")
        return Response({"org": org_body, "user": user_payload(user)})


# --- roles ------------------------------------------------------------------------------------------------
class RoleListView(TenantAPIView):
    permission_classes = [IsAuthenticated, DenyApiKey, HasOrgPermission]
    required_permissions = "roles.view"

    def get(self, request, org):
        roles = Role.objects.prefetch_related("role_permissions__permission").order_by("-is_system", "name")
        return Response(
            [
                {
                    "id": str(r.id),
                    "key": r.key,
                    "name": r.name,
                    "description": r.description,
                    "is_system": r.is_system,
                    "permissions": sorted(rp.permission.codename for rp in r.role_permissions.all()),
                }
                for r in roles
            ]
        )


class PermissionCatalogView(APIView):
    """GET /permissions: the global permission catalog."""

    authentication_classes = [CsrfEnforcedSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response([{"codename": p.codename, "description": p.description}
                         for p in Permission.objects.order_by("codename")])


# --- AI data policy -----------------------------------------------------------------------------------------
class AIPolicyView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "org.view"

    def get(self, request, org):
        return Response(services.ai_policy_payload())


class AIConsentTextView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "org.view"

    def get(self, request, org):
        version = ai_consent.current_version()
        return Response({"version": version, "text": ai_consent.consent_text(version)})


class AIConsentGrantView(TenantAPIView):
    """POST {policy, consent_text_version, acknowledged: true, password} -> 201 AI-policy state."""

    permission_classes = [IsAuthenticated, DenyApiKey, HasOrgPermission]
    required_permissions = "org.ai_policy.consent"
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "reauth"

    def post(self, request, org):
        ser = ConsentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        require_password(request.user, data["password"])  # re-authentication
        body = services.grant_ai_consent(
            policy=data["policy"],
            consent_text_version=data["consent_text_version"],
            acknowledged=data["acknowledged"],
            actor=request.user,
            request=request,
        )
        return Response(body, status=status.HTTP_201_CREATED)


class AIConsentRevokeView(TenantAPIView):
    permission_classes = [IsAuthenticated, DenyApiKey, HasOrgPermission]
    required_permissions = "org.ai_policy.consent"

    def post(self, request, org):
        return Response(services.revoke_ai_consent(actor=request.user, request=request))


# --- activity feed -------------------------------------------------------------------------------------------
ACTIVITY_LIMIT = 50


class ActivityView(TenantAPIView):
    """Latest 50 audit events enriched with the actor's email/name (dashboard feed)."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "org.view"

    def get(self, request, org):
        events = list(AuditEvent.objects.order_by("-occurred_at", "-id")[:ACTIVITY_LIMIT])
        users = users_by_id(e.actor_id for e in events if e.actor_type == "user")
        items = []
        for e in events:
            actor = users.get(str(e.actor_id)) if e.actor_type == "user" else None
            items.append(
                {
                    "id": str(e.id),
                    "occurred_at": e.occurred_at,
                    "actor_type": e.actor_type,
                    "actor_id": str(e.actor_id) if e.actor_id else None,
                    "actor_email": actor.email if actor else None,
                    "actor_name": actor.full_name if actor else None,
                    "action": e.action,
                    "target_type": e.target_type or None,
                    "target_id": e.target_id or None,
                    "metadata": e.metadata or {},
                }
            )
        return Response({"items": items, "next_cursor": None, "previous_cursor": None})
