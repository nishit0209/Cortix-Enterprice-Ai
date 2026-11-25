"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { motion, useReducedMotion } from "framer-motion";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { cn } from "@/lib/utils";
import { NAV, activeNavItem, orgHref, type NavItem } from "@/lib/nav";
import { Logo } from "@/components/brand/logo";
import { Tooltip } from "@/components/ui/overlays";
import { useOrgContext } from "./org-context";

export function useVisibleNav() {
  const { can } = useOrgContext();
  return React.useMemo(
    () =>
      NAV.map((s) => ({ ...s, items: s.items.filter((i) => !i.perm || can(i.perm)) })).filter((s) => s.items.length > 0),
    [can],
  );
}

export function SidebarNav({ collapsed, onNavigate }: { collapsed: boolean; onNavigate?: () => void }) {
  const { slug } = useOrgContext();
  const pathname = usePathname();
  const active = activeNavItem(pathname, slug);
  const sections = useVisibleNav();
  const reduce = useReducedMotion();

  return (
    <nav aria-label="Main" className="scrollbar-thin flex-1 overflow-y-auto overflow-x-hidden px-2.5 py-3">
      {sections.map((section, si) => (
        <div key={section.id} className={cn(si > 0 && "mt-4")}>
          {section.label &&
            (collapsed ? (
              <div className="mx-2 mb-2 h-px bg-border" aria-hidden />
            ) : (
              <div className="mb-1 px-2.5 text-[11px] font-semibold uppercase tracking-[0.09em] text-fg-subtle">
                {section.label}
              </div>
            ))}
          {!section.label && si > 0 && collapsed && <div className="mx-2 mb-2 h-px bg-border" aria-hidden />}
          <ul className="space-y-0.5">
            {section.items.map((item) => (
              <li key={item.id}>
                <NavLink
                  item={item}
                  href={orgHref(slug, item.path)}
                  active={active?.id === item.id}
                  collapsed={collapsed}
                  onNavigate={onNavigate}
                  reduceMotion={!!reduce}
                />
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}

function NavLink({
  item,
  href,
  active,
  collapsed,
  onNavigate,
  reduceMotion,
}: {
  item: NavItem;
  href: string;
  active: boolean;
  collapsed: boolean;
  onNavigate?: () => void;
  reduceMotion: boolean;
}) {
  const Icon = item.icon;
  const link = (
    <Link
      href={href}
      prefetch={true}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={cn(
        "group relative flex h-8 items-center gap-2.5 rounded-control px-2.5 text-sm font-medium transition-colors focus-ring",
        active ? "text-fg" : "text-fg-muted hover:bg-surface-3/60 hover:text-fg",
        collapsed && "justify-center px-0",
      )}
    >
      {active && (
        <motion.span
          layoutId="sidebar-active"
          className="absolute inset-0 rounded-control border border-border bg-surface shadow-xs"
          transition={reduceMotion ? { duration: 0 } : { type: "spring", stiffness: 500, damping: 40 }}
          aria-hidden
        />
      )}
      <Icon
        className={cn("relative size-4 shrink-0 transition-colors", active ? "text-accent" : "text-fg-subtle group-hover:text-fg-muted")}
        aria-hidden
      />
      {!collapsed && <span className="relative truncate">{item.label}</span>}
      {!collapsed && item.status === "planned" && (
        <span className="relative ml-auto rounded-full border border-border px-1.5 text-[10px] font-medium leading-4 text-fg-subtle">
          Soon
        </span>
      )}
      {collapsed && <span className="sr-only">{item.label}</span>}
    </Link>
  );
  return collapsed ? (
    <Tooltip content={item.status === "planned" ? `${item.label} · Planned` : item.label} side="right">
      {link}
    </Tooltip>
  ) : (
    link
  );
}

export function Sidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const { slug } = useOrgContext();
  return (
    <aside
      className={cn(
        "sticky top-0 hidden h-dvh shrink-0 flex-col border-r border-border bg-surface-2 transition-[width] duration-200 ease-out md:flex",
        collapsed ? "w-[60px]" : "w-[248px]",
      )}
      aria-label="Sidebar"
    >
      <div className={cn("flex h-14 items-center border-b border-border", collapsed ? "justify-center px-2" : "px-4")}>
        <Link href={orgHref(slug, "/dashboard")} className="rounded-control focus-ring" aria-label="Cortix home">
          <Logo collapsed={collapsed} />
        </Link>
      </div>
      <SidebarNav collapsed={collapsed} />
      <div className={cn("border-t border-border p-2.5", collapsed && "flex justify-center")}>
        <Tooltip content={collapsed ? "Expand sidebar" : undefined} side="right">
          <button
            type="button"
            onClick={onToggle}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!collapsed}
            className={cn(
              "flex h-8 items-center gap-2.5 rounded-control px-2.5 text-sm text-fg-muted transition-colors hover:bg-surface-3/60 hover:text-fg focus-ring",
              collapsed ? "w-8 justify-center px-0" : "w-full",
            )}
          >
            {collapsed ? <PanelLeftOpen className="size-4" /> : <PanelLeftClose className="size-4" />}
            {!collapsed && <span>Collapse</span>}
            {!collapsed && <span className="ml-auto text-[11px] text-fg-subtle">[</span>}
          </button>
        </Tooltip>
      </div>
    </aside>
  );
}
