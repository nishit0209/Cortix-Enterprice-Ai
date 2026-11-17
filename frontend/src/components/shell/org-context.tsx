"use client";

import * as React from "react";
import type { MeResponse, Org, OrgSummary } from "@/lib/types";

export interface OrgContextValue {
  slug: string;
  me: MeResponse;
  /** Full org (with permissions). Undefined while loading or if GET /orgs/{org} is unavailable. */
  org: Org | undefined;
  summary: OrgSummary | undefined;
  /** Permission check. While permissions are unknown, falls back to role-based defaults for owners/admins. */
  can: (perm: string | string[]) => boolean;
}

const Ctx = React.createContext<OrgContextValue | null>(null);

export function OrgProvider({ value, children }: { value: OrgContextValue; children: React.ReactNode }) {
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useOrgContext(): OrgContextValue {
  const v = React.useContext(Ctx);
  if (!v) throw new Error("useOrgContext must be used inside an org route");
  return v;
}

/* ---------------- breadcrumb override (e.g. dataset name instead of its id) ---------------- */

const CrumbCtx = React.createContext<{
  label: string | null;
  setLabel: (label: string | null) => void;
}>({ label: null, setLabel: () => {} });

export function BreadcrumbProvider({ children }: { children: React.ReactNode }) {
  const [label, setLabel] = React.useState<string | null>(null);
  const value = React.useMemo(() => ({ label, setLabel }), [label]);
  return <CrumbCtx.Provider value={value}>{children}</CrumbCtx.Provider>;
}

export function useBreadcrumbLabel() {
  return React.useContext(CrumbCtx).label;
}

/** Pages call this to name the last breadcrumb (cleared on unmount). */
export function useSetBreadcrumb(label: string | null | undefined) {
  const { setLabel } = React.useContext(CrumbCtx);
  React.useEffect(() => {
    setLabel(label ?? null);
    return () => setLabel(null);
  }, [label, setLabel]);
}
