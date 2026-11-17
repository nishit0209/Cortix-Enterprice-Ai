"""Permission catalog + system role seeds (a CONTRACT shared by all agents).

New codenames: request them from the Integrator via your handoff file.
"""

PERMISSIONS: dict[str, str] = {
    # org & people
    "org.view": "View the organization",
    "org.settings.edit": "Edit organization settings, AI config and budgets",
    "org.ai_policy.consent": "Grant/revoke AI data-policy consent",
    "members.view": "View members",
    "members.manage": "Invite, remove and change roles of members",
    "roles.view": "View roles",
    "roles.manage": "Create and edit custom roles",
    "api_keys.manage": "Create and revoke API keys",
    "audit.view": "View audit logs, access history and query history of all users",
    "jobs.view_all": "View all background jobs in the org",
    # data engineering
    "sources.view": "View data sources",
    "sources.manage": "Create/edit/test data sources",
    "datasets.view": "View datasets, profiles and quality",
    "datasets.create": "Upload files and create datasets",
    "datasets.manage": "Edit/delete datasets",
    "datasets.classify": "Change dataset/document classification (synthetic/public attestation)",
    "quality.manage": "Manage quality rules",
    "pipelines.view": "View pipelines and runs",
    "pipelines.manage": "Create/edit/publish/schedule pipelines",
    "pipelines.run": "Run, retry and cancel pipelines",
    # BI
    "semantic.manage": "Edit semantic models and metrics",
    "bi.chat": "Use conversational BI",
    "bi.sql": "Run manual (guarded) SQL",
    "dashboards.view": "View dashboards",
    "dashboards.create": "Create/edit dashboards and saved queries",
    "exports.create": "Export results and reports",
    # governance
    "governance.policy.edit": "Edit dataset ACLs, column and row policies, PII tags",
    "governance.retention.edit": "Edit retention policies",
    # documents
    "documents.view": "View and search documents",
    "documents.manage": "Upload/delete documents, run extraction",
    "documents.ask": "Ask questions over documents",
    # ML
    "ml.view": "View ML projects and models",
    "ml.manage": "Run EDA, train and register models",
    "ml.promote": "Promote models to production",
    "ml.predict": "Call the prediction API",
    # Customer 360 / Product 360
    "customers.view": "View customer profiles",
    "customers.manage": "Import/resolve/segment/score customers",
    "customers.review": "Review identity matches",
    "products.view": "View product master data",
    "products.manage": "Import/normalize/enrich products",
    "products.review": "Review product matches and corrections",
    # automation
    "automation.view": "View workflows and runs",
    "automation.manage": "Create/edit workflows, triggers and agents",
    "automation.run": "Run workflows",
    "approvals.decide": "Approve or reject approval requests",
}

_ALL = set(PERMISSIONS)
_ANALYST = {
    "org.view", "members.view", "roles.view", "sources.view", "datasets.view", "pipelines.view",
    "bi.chat", "bi.sql", "dashboards.view", "dashboards.create", "exports.create",
    "documents.view", "documents.ask", "ml.view", "customers.view", "products.view", "automation.view",
}

SYSTEM_ROLES: dict[str, dict] = {
    "owner": {"name": "Owner", "permissions": _ALL},
    "admin": {"name": "Admin", "permissions": _ALL},
    "data_engineer": {
        "name": "Data Engineer",
        "permissions": _ANALYST | {
            "sources.manage", "datasets.create", "datasets.manage", "quality.manage", "pipelines.manage",
            "pipelines.run", "semantic.manage", "documents.manage", "customers.manage", "products.manage",
            "automation.run",
        },
    },
    "analyst": {"name": "Analyst", "permissions": _ANALYST},
    "data_scientist": {
        "name": "Data Scientist",
        "permissions": _ANALYST | {"ml.manage", "ml.predict", "semantic.manage", "datasets.create", "customers.manage"},
    },
    "viewer": {
        "name": "Viewer",
        "permissions": {"org.view", "datasets.view", "bi.chat", "dashboards.view", "documents.view",
                        "documents.ask", "customers.view", "products.view"},
    },
    "auditor": {
        "name": "Auditor",
        "permissions": {"org.view", "members.view", "roles.view", "audit.view", "jobs.view_all",
                        "datasets.view", "pipelines.view", "dashboards.view", "automation.view"},
    },
}
