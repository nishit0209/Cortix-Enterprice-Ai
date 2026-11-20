/**
 * Client mirror of backend/apps/orgs/permissions_catalog.py SYSTEM_ROLES.
 * Used only as a fallback for UI gating while GET /orgs/{org} (authoritative `permissions`) is loading or
 * unavailable. The server always enforces permissions; this never grants access.
 */

const ANALYST = [
  "org.view", "members.view", "roles.view", "sources.view", "datasets.view", "pipelines.view",
  "bi.chat", "bi.sql", "dashboards.view", "dashboards.create", "exports.create",
  "documents.view", "documents.ask", "ml.view", "customers.view", "products.view", "automation.view",
];

export const ROLE_PERMISSIONS: Record<string, string[] | "*"> = {
  owner: "*",
  admin: "*",
  data_engineer: [
    ...ANALYST,
    "sources.manage", "datasets.create", "datasets.manage", "quality.manage", "pipelines.manage",
    "pipelines.run", "semantic.manage", "documents.manage", "customers.manage", "products.manage", "automation.run",
  ],
  analyst: ANALYST,
  data_scientist: [...ANALYST, "ml.manage", "ml.predict", "semantic.manage", "datasets.create", "customers.manage"],
  viewer: ["org.view", "datasets.view", "bi.chat", "dashboards.view", "documents.view", "documents.ask", "customers.view", "products.view"],
  auditor: ["org.view", "members.view", "roles.view", "audit.view", "jobs.view_all", "datasets.view", "pipelines.view", "dashboards.view", "automation.view"],
};

export const ROLE_LABELS: Record<string, string> = {
  owner: "Owner",
  admin: "Admin",
  data_engineer: "Data Engineer",
  analyst: "Analyst",
  data_scientist: "Data Scientist",
  viewer: "Viewer",
  auditor: "Auditor",
};

export const ROLE_DESCRIPTIONS: Record<string, string> = {
  owner: "Full control, including billing and deleting the organization.",
  admin: "Full control of members, data, AI policy and settings.",
  data_engineer: "Connects sources, uploads data and builds pipelines.",
  analyst: "Explores data, asks questions and builds dashboards.",
  data_scientist: "Analyst access plus ML training and predictions.",
  viewer: "Read-only access to datasets and dashboards.",
  auditor: "Read-only access to audit logs and activity.",
};

export function roleAllows(role: string | undefined, perms: string[]): boolean {
  if (!role) return false;
  const granted = ROLE_PERMISSIONS[role];
  if (!granted) return false;
  if (granted === "*") return true;
  return perms.every((p) => granted.includes(p));
}

export function permissionsAllow(permissions: string[] | undefined, role: string | undefined, perm: string | string[]) {
  const perms = Array.isArray(perm) ? perm : [perm];
  if (permissions) return perms.every((p) => permissions.includes(p));
  return roleAllows(role, perms);
}

/**
 * Creating/editing dashboards. The catalog codename is `dashboards.create`; the W3 contract names it
 * `dashboards.manage`. Either one grants it, so the UI works whichever the backend ships.
 */
export function canManageDashboards(can: (perm: string) => boolean): boolean {
  return can("dashboards.manage") || can("dashboards.create");
}
