"""ML workspace API (API_SPECIFICATION Phase 9; mounted under /api/v1/orgs/{org}/).

    GET, POST  ml/projects                          list (?status=active|archived|all) / create (ml.manage)
    GET, PATCH, DELETE ml/projects/{id}             detail / update / archive
    GET, POST  ml/projects/{id}/eda                 latest EDA (404 until run) / run EDA (202 + job)
    GET        ml/projects/{id}/recommendations     ranked algorithms with reasons
    GET, POST  ml/projects/{id}/snapshots           list / freeze the training data (202 + job)
    GET, POST  ml/projects/{id}/experiments         list / train (202 + job)
    GET        ml/experiments/{id}                  detail (metrics, confusion matrix, SHAP, importances)
    GET        ml/experiments/compare?ids=a,b       side by side, best by the primary metric
    POST       ml/experiments/{id}/register         -> 201 model version (ml.manage)
    GET        ml/models Â· ml/models/{id} Â· ml/models/{id}/versions
    POST       ml/models/{id}/versions/{v}/promote  {stage} (ml.promote; one production version per model;
                                                   production -> 202 + approval_id, applied on approval)
    POST       ml/models/{id}/predict               rows <= 1000 (+ explain); session ml.view or API key ml.predict
    POST       ml/models/{id}/batch-predictions     202 + job; writes a new dataset (ml.manage)
    GET        ml/models/{id}/monitoring            drift (PSI) + volume + performance
"""

from __future__ import annotations

from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import HasOrgPermission
from apps.core.tenancy.views import TenantGenericViewSet

from . import serializers as s
from . import services
from .models import DatasetSnapshot, Experiment, MLModel, MLProject, ModelVersion

MAX_COMPARE = 10


def _uid(request):
    return getattr(request.user, "pk", None)


def _accepted(request, job, **extra) -> Response:
    return Response({**services.job_links(request.tenant.slug, job), **extra}, status=status.HTTP_202_ACCEPTED)


class CanPredict(BasePermission):
    """`ml.predict` (API-key scope, also a role permission) or `ml.view`."""

    message = "You do not have permission to perform this action."

    def has_permission(self, request, view) -> bool:
        tenant = getattr(request, "tenant", None)
        return bool(tenant and (tenant.has_perm("ml.predict") or tenant.has_perm("ml.view")))


class ProjectViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, TenantGenericViewSet):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {
        "list": "ml.view", "retrieve": "ml.view", "recommendations": "ml.view",
        "*": "ml.manage",
    }
    serializer_class = s.ProjectSerializer

    def get_permissions(self):
        if self.action in ("eda", "snapshots", "experiments") and self.request.method == "GET":
            self.required_permissions = {**type(self).required_permissions, self.action: "ml.view"}
        return super().get_permissions()

    def get_queryset(self):
        qs = MLProject.objects.all()
        if self.action == "list":
            wanted = self.request.query_params.get("status") or "active"
            if wanted != "all":
                qs = qs.filter(status=wanted)
        return qs.order_by("-created_at")

    def get_object(self):
        return services.get_project(self.kwargs["pk"])

    def retrieve(self, request, *args, **kwargs):
        return Response(s.ProjectDetailSerializer(self.get_object()).data)

    def create(self, request, *args, **kwargs):
        ser = s.ProjectCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        project = services.create_project(user_id=_uid(request), data=ser.validated_data, request=request)
        return Response(s.ProjectDetailSerializer(project).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        project = self.get_object()
        ser = s.ProjectUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        project = services.update_project(project, ser.validated_data, request=request)
        return Response(s.ProjectDetailSerializer(project).data)

    def destroy(self, request, *args, **kwargs):
        """Archive (projects keep their experiments, models and audit trail)."""
        services.update_project(self.get_object(), {"status": "archived"}, request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get", "post"])
    def eda(self, request, pk=None, org=None):
        project = self.get_object()
        if request.method == "GET":
            if not project.eda:
                raise NotFound("EDA has not been run for this project yet.")
            return Response({**project.eda, "status": project.eda_status, "job_id": project.eda_job_id,
                             "narrative": project.eda_narrative or None,
                             "narrative_source": project.eda_narrative_source or None})
        ser = s.EdaRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        job = services.start_eda(project, user_id=_uid(request), narrate=ser.validated_data["narrate"],
                                 request=request)
        return _accepted(request, job, project_id=str(project.id))

    @action(detail=True, methods=["get"])
    def recommendations(self, request, pk=None, org=None):
        return Response(services.recommendations(self.get_object()))

    @action(detail=True, methods=["get", "post"])
    def snapshots(self, request, pk=None, org=None):
        project = self.get_object()
        if request.method == "GET":
            qs = DatasetSnapshot.objects.filter(project=project).order_by("-created_at")[:200]
            return Response({"items": s.SnapshotSerializer(qs, many=True).data})
        ser = s.SnapshotRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        snap, job = services.start_snapshot(project, user_id=_uid(request), data=ser.validated_data,
                                            request=request)
        return _accepted(request, job, snapshot_id=str(snap.id), snapshot=s.SnapshotSerializer(snap).data)

    @action(detail=True, methods=["get", "post"])
    def experiments(self, request, pk=None, org=None):
        project = self.get_object()
        if request.method == "GET":
            qs = Experiment.objects.filter(project=project).order_by("-created_at")[:500]
            return Response({"items": s.ExperimentSerializer(qs, many=True).data})
        ser = s.ExperimentRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        created, job, snap = services.start_experiments(project, user_id=_uid(request), data=ser.validated_data,
                                                        request=request)
        return _accepted(request, job, experiment_id=str(created[0].id), snapshot_id=str(snap.id),
                         experiments=s.ExperimentSerializer(created, many=True).data)


class ExperimentViewSet(TenantGenericViewSet):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"retrieve": "ml.view", "compare": "ml.view", "*": "ml.manage"}
    serializer_class = s.ExperimentSerializer

    def get_queryset(self):
        return Experiment.objects.all()

    def retrieve(self, request, pk=None, org=None):
        return Response(s.ExperimentDetailSerializer(services.get_experiment(pk)).data)

    @action(detail=False, methods=["get"])
    def compare(self, request, org=None):
        ids = [i for i in (request.query_params.get("ids") or "").split(",") if i.strip()]
        if not 1 <= len(ids) <= MAX_COMPARE:
            raise ValidationError({"ids": [f"Pass 1 to {MAX_COMPARE} experiment ids, comma-separated."]})
        out = services.compare([i.strip() for i in ids])
        details = {str(e.id): e for e in Experiment.objects.filter(id__in=[i["id"] for i in out["items"]])}
        out["experiments"] = [s.ExperimentSerializer(details[i["id"]]).data | {"primary_value": i["primary_value"]}
                              for i in out["items"]]
        return Response(out)

    @action(detail=True, methods=["post"])
    def register(self, request, pk=None, org=None):
        experiment = services.get_experiment(pk)
        ser = s.RegisterSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        version = services.register(experiment, user_id=_uid(request), data=ser.validated_data,
                                    request=request)
        model = MLModel.objects.get(id=version.model_id)
        return Response({"model_id": str(model.id), "version": version.version,
                         "model": s.ModelSerializer(model).data, "model_version": s.VersionSerializer(version).data},
                        status=status.HTTP_201_CREATED)


class ModelViewSet(mixins.ListModelMixin, TenantGenericViewSet):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {
        "list": "ml.view", "retrieve": "ml.view", "versions": "ml.view", "monitoring": "ml.view",
        "promote": "ml.promote", "*": "ml.manage",
    }
    serializer_class = s.ModelSerializer

    def get_permissions(self):
        if self.action == "predict":
            return [IsAuthenticated(), CanPredict()]
        return super().get_permissions()

    def get_queryset(self):
        qs = MLModel.objects.prefetch_related("versions").order_by("-created_at")
        project = self.request.query_params.get("project_id")
        if project:
            qs = qs.filter(project_id=services._uuid(project))
        return qs

    def get_object(self):
        return services.get_model(self.kwargs["pk"])

    def retrieve(self, request, pk=None, org=None):
        return Response(s.ModelDetailSerializer(self.get_object()).data)

    @action(detail=True, methods=["get"])
    def versions(self, request, pk=None, org=None):
        qs = ModelVersion.objects.filter(model=self.get_object()).order_by("-version")
        return Response({"items": s.VersionSerializer(qs, many=True).data})

    @action(detail=True, methods=["post"], url_path=r"versions/(?P<version>[^/.]+)/promote")
    def promote(self, request, pk=None, org=None, version=None):
        model = self.get_object()
        if not str(version).isdigit():
            raise NotFound()
        services.get_version(model, int(version))
        ser = s.PromoteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        outcome, obj = services.request_promotion(model, int(version), ser.validated_data["stage"],
                                                  user_id=_uid(request), request=request)
        if outcome == "pending":  # CONTRACT W5: production promotion waits for an approval (202 + approval_id)
            return Response({"status": "pending_approval", "approval_id": str(obj.id), "model_id": str(model.id),
                             "version": int(version), "stage": "production",
                             "model_version": s.VersionSerializer(services.get_version(model, int(version))).data},
                            status=status.HTTP_202_ACCEPTED)
        return Response(s.VersionSerializer(obj).data)

    @action(detail=True, methods=["post"])
    def predict(self, request, pk=None, org=None):
        model = self.get_object()
        ser = s.PredictSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        source = "api" if getattr(request, "_nexora_api_key_id", None) else (
            "ui" if request.headers.get("X-Nexora-Client") == "ui" else "api")
        return Response(services.predict_rows(model, d.get("rows"), version=d.get("version"),
                                              explain=d["explain"], horizon=d.get("horizon"), source=source,
                                              request=request))

    @action(detail=True, methods=["post"], url_path="batch-predictions")
    def batch_predictions(self, request, pk=None, org=None):
        model = self.get_object()
        ser = s.BatchPredictionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        job, mv = services.start_batch_prediction(model, user_id=_uid(request),
                                                  data=ser.validated_data, request=request)
        return _accepted(request, job, model_id=str(mv.model_id), version=mv.version)

    @action(detail=True, methods=["get"])
    def monitoring(self, request, pk=None, org=None):
        return Response(services.monitoring(self.get_object()))
