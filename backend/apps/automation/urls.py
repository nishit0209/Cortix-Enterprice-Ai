from django.urls import path, re_path

from .views import (
    UUID_RE,
    AgentDetailView,
    AgentListView,
    AgentRunsView,
    ApprovalDecisionView,
    ApprovalDetailView,
    ApprovalListView,
    RunCancelView,
    RunDetailView,
    RunListView,
    RunStepsView,
    StepCatalogView,
    TriggerDetailView,
    TriggerListView,
    TriggerRotateSecretView,
    WorkflowDetailView,
    WorkflowListView,
    WorkflowPublishView,
    WorkflowRunsView,
    WorkflowValidateView,
    WorkflowVersionsView,
)

_PK = rf"(?P<pk>{UUID_RE})"
_TID = rf"(?P<trigger_id>{UUID_RE})"

urlpatterns = [
    path("workflows", WorkflowListView.as_view(), name="automation-workflow-list"),
    path("workflows/validate", WorkflowValidateView.as_view(), name="automation-workflow-validate"),
    re_path(rf"^workflows/{_PK}$", WorkflowDetailView.as_view(), name="automation-workflow-detail"),
    re_path(rf"^workflows/{_PK}/versions$", WorkflowVersionsView.as_view(), name="automation-workflow-versions"),
    re_path(rf"^workflows/{_PK}/publish$", WorkflowPublishView.as_view(), name="automation-workflow-publish"),
    re_path(rf"^workflows/{_PK}/triggers$", TriggerListView.as_view(), name="automation-trigger-list"),
    re_path(rf"^workflows/{_PK}/triggers/{_TID}$", TriggerDetailView.as_view(), name="automation-trigger-detail"),
    re_path(rf"^workflows/{_PK}/triggers/{_TID}/rotate-secret$", TriggerRotateSecretView.as_view(),
            name="automation-trigger-rotate-secret"),
    re_path(rf"^workflows/{_PK}/runs$", WorkflowRunsView.as_view(), name="automation-workflow-runs"),
    path("workflow-runs", RunListView.as_view(), name="automation-run-list"),
    re_path(rf"^workflow-runs/{_PK}$", RunDetailView.as_view(), name="automation-run-detail"),
    re_path(rf"^workflow-runs/{_PK}/steps$", RunStepsView.as_view(), name="automation-run-steps"),
    re_path(rf"^workflow-runs/{_PK}/cancel$", RunCancelView.as_view(), name="automation-run-cancel"),
    path("automation/steps", StepCatalogView.as_view(), name="automation-step-catalog"),
    path("agents", AgentListView.as_view(), name="automation-agent-list"),
    re_path(rf"^agents/{_PK}$", AgentDetailView.as_view(), name="automation-agent-detail"),
    re_path(rf"^agents/{_PK}/runs$", AgentRunsView.as_view(), name="automation-agent-runs"),
    path("approvals", ApprovalListView.as_view(), name="automation-approval-list"),
    re_path(rf"^approvals/{_PK}$", ApprovalDetailView.as_view(), name="automation-approval-detail"),
    re_path(rf"^approvals/{_PK}/decision$", ApprovalDecisionView.as_view(), name="automation-approval-decision"),
]
