import {
  BarChart3,
  Boxes,
  Bot,
  BrainCircuit,
  Cable,
  Database,
  FileText,
  GitBranch,
  LayoutDashboard,
  ListChecks,
  type LucideIcon,
  Network,
  ScrollText,
  Settings,
  ShieldCheck,
  Sparkles,
  UserRound,
  Users,
  Workflow,
} from "lucide-react";
import type { FeatureStatus } from "@/components/ui/status";

export interface NavItem {
  id: string;
  label: string;
  /** Path relative to /o/[org] */
  path: string;
  icon: LucideIcon;
  status: FeatureStatus;
  /** Permission codename(s) required to see the item. */
  perm?: string;
  keywords?: string[];
  /** Copy for the module's Planned page. */
  summary: string;
  capabilities?: string[];
  phase?: string;
}

export interface NavSection {
  id: string;
  label: string | null;
  items: NavItem[];
}

export const NAV: NavSection[] = [
  {
    id: "overview",
    label: null,
    items: [
      {
        id: "dashboard",
        label: "Overview",
        path: "/dashboard",
        icon: LayoutDashboard,
        status: "working",
        keywords: ["home", "dashboard", "kpi"],
        summary: "Organization overview: data, pipelines, quality and AI usage at a glance.",
      },
    ],
  },
  {
    id: "data",
    label: "Data",
    items: [
      {
        id: "sources",
        label: "Sources",
        path: "/data/sources",
        icon: Cable,
        status: "planned",
        perm: "sources.view",
        keywords: ["connectors", "postgres", "database"],
        summary: "Connect read-only PostgreSQL sources, test them with streamed checks and import tables as datasets.",
        capabilities: [
          "Write-only credentials, encrypted at rest",
          "SSRF-safe host checks and read-only privilege verification",
          "Introspect schemas and import tables as versioned datasets",
        ],
        phase: "Wave 2",
      },
      {
        id: "datasets",
        label: "Datasets",
        path: "/data/datasets",
        icon: Database,
        status: "working",
        perm: "datasets.view",
        keywords: ["upload", "csv", "tables"],
        summary: "Upload, profile and explore datasets.",
      },
      {
        id: "pipelines",
        label: "Pipelines",
        path: "/data/pipelines",
        icon: Workflow,
        status: "planned",
        perm: "pipelines.view",
        keywords: ["etl", "dag", "runs", "schedule"],
        summary: "Visual pipeline builder with clean, dedup, reshape, quality and join steps, schedules and streamed runs.",
        capabilities: [
          "Drag-and-drop canvas with typed node inspectors",
          "Dry runs on 1,000 rows with live output schema",
          "Run history with step timeline, streamed logs and retries",
        ],
        phase: "Wave 2",
      },
      {
        id: "quality",
        label: "Quality",
        path: "/data/quality",
        icon: ListChecks,
        status: "planned",
        perm: "datasets.view",
        keywords: ["rules", "checks", "duplicates"],
        summary: "Org-wide data quality: rule coverage, failing checks and score trends across datasets.",
        capabilities: [
          "Completeness, uniqueness, validity and consistency scoring",
          "Custom rules per column with severities",
          "Exact, key and fuzzy duplicate detection",
        ],
        phase: "Wave 2",
      },
      {
        id: "lineage",
        label: "Lineage",
        path: "/data/lineage",
        icon: GitBranch,
        status: "planned",
        perm: "datasets.view",
        keywords: ["graph", "dependencies"],
        summary: "Interactive lineage graph from sources through pipelines to datasets, dashboards and models.",
        capabilities: ["Upstream and downstream impact analysis", "Column-level lineage for pipeline steps"],
        phase: "Wave 2",
      },
    ],
  },
  {
    id: "intelligence",
    label: "Intelligence",
    items: [
      {
        id: "ask",
        label: "Ask AI",
        path: "/ask",
        icon: Sparkles,
        status: "planned",
        perm: "bi.chat",
        keywords: ["chat", "nl2sql", "question", "bi"],
        summary: "Ask questions in plain language. Every answer shows its SQL, sources and the policy that applied.",
        capabilities: [
          "Streaming answers with charts, tables and local insights",
          "SQL transparency panel and guarded manual SQL",
          "Honest AI data-policy notices and quota banners",
        ],
        phase: "Wave 3",
      },
      {
        id: "dashboards",
        label: "Dashboards",
        path: "/dashboards",
        icon: BarChart3,
        status: "planned",
        perm: "dashboards.view",
        keywords: ["charts", "widgets", "reports"],
        summary: "Grid dashboards of KPI, chart, table and text widgets built from saved queries and metrics.",
        capabilities: ["Global date-range filters and drill-down", "BI and Org Admin templates"],
        phase: "Wave 4",
      },
      {
        id: "documents",
        label: "Documents",
        path: "/documents",
        icon: FileText,
        status: "planned",
        perm: "documents.view",
        keywords: ["pdf", "rag", "search"],
        summary: "Upload documents, extract structured fields and ask questions with citations.",
        capabilities: ["Hybrid semantic + keyword search", "Answers with citation chips per passage"],
        phase: "Later",
      },
      {
        id: "ml",
        label: "ML Workspace",
        path: "/ml",
        icon: BrainCircuit,
        status: "planned",
        perm: "ml.view",
        keywords: ["models", "training", "predictions"],
        summary: "Exploratory analysis, AutoML training, model registry and drift monitoring.",
        capabilities: ["Experiment tracking and model comparison", "Promotion workflow and prediction API"],
        phase: "Later",
      },
    ],
  },
  {
    id: "business",
    label: null,
    items: [
      {
        id: "customers",
        label: "Customer 360",
        path: "/customers",
        icon: UserRound,
        status: "planned",
        perm: "customers.view",
        keywords: ["crm", "identity", "segments"],
        summary: "Unified customer profiles with identity resolution, segments and scores.",
        capabilities: ["Probabilistic identity matching with human review", "Segments and churn/LTV scores"],
        phase: "Later",
      },
      {
        id: "products",
        label: "Product 360",
        path: "/products",
        icon: Boxes,
        status: "planned",
        perm: "products.view",
        keywords: ["catalog", "master data"],
        summary: "Product master data: normalization, enrichment, matching and corrections.",
        capabilities: ["Attribute normalization and enrichment", "Match review queue"],
        phase: "Later",
      },
      {
        id: "automation",
        label: "Automation",
        path: "/automation",
        icon: Bot,
        status: "planned",
        perm: "automation.view",
        keywords: ["workflows", "agents", "approvals"],
        summary: "Workflows, triggers and governed agents with human approvals.",
        capabilities: ["Visual workflow canvas", "Approval requests with audit trail"],
        phase: "Later",
      },
    ],
  },
  {
    id: "governance",
    label: "Governance",
    items: [
      {
        id: "ai-policy",
        label: "AI data policy",
        path: "/settings/ai-policy",
        icon: ShieldCheck,
        status: "working",
        perm: "org.view",
        keywords: ["gemini", "consent", "privacy"],
        summary: "Control what data may be sent to Gemini.",
      },
      {
        id: "policies",
        label: "Access policies",
        path: "/governance",
        icon: Network,
        status: "planned",
        perm: "governance.policy.edit",
        keywords: ["acl", "masking", "row level"],
        summary: "Dataset ACLs, column masking and row policies with a Preview-as-user panel.",
        capabilities: ["Allow / deny / mask per role and column", "Structured row predicates with $user tokens"],
        phase: "Wave 3",
      },
      {
        id: "audit",
        label: "Audit log",
        path: "/audit",
        icon: ScrollText,
        status: "working",
        perm: "audit.view",
        keywords: ["events", "history", "compliance"],
        summary: "Every mutation in the organization.",
      },
    ],
  },
  {
    id: "admin",
    label: "Admin",
    items: [
      {
        id: "members",
        label: "Members",
        path: "/settings/members",
        icon: Users,
        status: "working",
        perm: "members.view",
        keywords: ["team", "invite", "roles", "users"],
        summary: "Invite teammates and manage roles.",
      },
      {
        id: "settings",
        label: "Settings",
        path: "/settings",
        icon: Settings,
        status: "working",
        perm: "org.view",
        keywords: ["organization", "general"],
        summary: "Organization profile and preferences.",
      },
    ],
  },
];

export const ALL_NAV_ITEMS: NavItem[] = NAV.flatMap((s) => s.items);

export function navItem(id: string): NavItem {
  const item = ALL_NAV_ITEMS.find((i) => i.id === id);
  if (!item) throw new Error(`Unknown nav item ${id}`);
  return item;
}

export function orgHref(org: string, path: string) {
  return `/o/${org}${path}`;
}

/** Longest-prefix match of the current pathname to a nav item. */
export function activeNavItem(pathname: string, org: string): NavItem | undefined {
  const rel = pathname.replace(`/o/${org}`, "") || "/dashboard";
  let best: NavItem | undefined;
  for (const item of ALL_NAV_ITEMS) {
    if (rel === item.path || rel.startsWith(item.path + "/")) {
      if (!best || item.path.length > best.path.length) best = item;
    }
  }
  return best;
}
