"use client";

import * as React from "react";
import { usePathname } from "next/navigation";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Menu, Search, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { activeNavItem, NAV, orgHref } from "@/lib/nav";
import { Breadcrumbs, type Crumb } from "@/components/ui/page-header";
import { Kbd } from "@/components/ui/feedback";
import { Logo } from "@/components/brand/logo";
import { Sidebar, SidebarNav } from "./sidebar";
import { OrgSwitcher } from "./org-switcher";
import { AiStatusPill } from "./ai-status-pill";
import { NotificationCenter } from "./notification-center";
import { UserMenu } from "./user-menu";
import { CommandMenu } from "./command-menu";
import { useBreadcrumbLabel, useOrgContext } from "./org-context";

const SIDEBAR_KEY = "nexora:sidebar-collapsed";

function useCollapsedSidebar() {
  const [collapsed, setCollapsed] = React.useState(false);
  React.useEffect(() => {
    try {
      const stored = window.localStorage.getItem(SIDEBAR_KEY);
      if (stored !== null) setCollapsed(stored === "1");
      else if (window.matchMedia("(max-width: 1279px)").matches) setCollapsed(true);
    } catch {
      /* storage unavailable */
    }
  }, []);
  const toggle = React.useCallback(() => {
    setCollapsed((c) => {
      try {
        window.localStorage.setItem(SIDEBAR_KEY, c ? "0" : "1");
      } catch {
        /* ignore */
      }
      return !c;
    });
  }, []);
  return [collapsed, toggle] as const;
}

function isTypingTarget(t: EventTarget | null) {
  const el = t as HTMLElement | null;
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
}

function useCrumbs(): Crumb[] {
  const pathname = usePathname();
  const { slug } = useOrgContext();
  const override = useBreadcrumbLabel();
  const item = activeNavItem(pathname, slug);
  if (!item) return override ? [{ label: override }] : [];
  const section = NAV.find((s) => s.items.some((i) => i.id === item.id));
  const crumbs: Crumb[] = [];
  if (section?.label) crumbs.push({ label: section.label });
  crumbs.push({ label: item.label, href: orgHref(slug, item.path) });
  const rest = pathname.replace(`/o/${slug}${item.path}`, "");
  if (rest && rest !== "/") crumbs.push({ label: override ?? decodeURIComponent(rest.split("/").filter(Boolean).pop() ?? "") });
  return crumbs;
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const [collapsed, toggleCollapsed] = useCollapsedSidebar();
  const [commandOpen, setCommandOpen] = React.useState(false);
  const [mobileOpen, setMobileOpen] = React.useState(false);
  const crumbs = useCrumbs();
  const pathname = usePathname();

  React.useEffect(() => setMobileOpen(false), [pathname]);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCommandOpen((o) => !o);
        return;
      }
      if (e.key === "[" && !e.metaKey && !e.ctrlKey && !e.altKey && !isTypingTarget(e.target)) {
        toggleCollapsed();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleCollapsed]);

  return (
    <div className="flex min-h-dvh bg-bg">
      <a
        href="#main"
        className="sr-only z-50 rounded-control bg-surface px-3 py-2 text-sm focus:not-sr-only focus:fixed focus:left-3 focus:top-3"
      >
        Skip to content
      </a>
      <Sidebar collapsed={collapsed} onToggle={toggleCollapsed} />

      {/* Mobile drawer */}
      <DialogPrimitive.Root open={mobileOpen} onOpenChange={setMobileOpen}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-overlay data-[state=open]:animate-fade md:hidden" />
          <DialogPrimitive.Content
            className="fixed inset-y-0 left-0 z-50 flex w-[280px] max-w-[85vw] flex-col border-r border-border bg-surface-2 shadow-pop focus:outline-none md:hidden"
            aria-describedby={undefined}
          >
            <DialogPrimitive.Title className="sr-only">Navigation</DialogPrimitive.Title>
            <div className="flex h-14 items-center justify-between border-b border-border px-4">
              <Logo />
              <DialogPrimitive.Close
                className="flex size-8 items-center justify-center rounded-control text-fg-muted hover:bg-surface-3 focus-ring"
                aria-label="Close navigation"
              >
                <X className="size-4" />
              </DialogPrimitive.Close>
            </div>
            <SidebarNav collapsed={false} onNavigate={() => setMobileOpen(false)} />
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-border bg-bg/80 px-3 backdrop-blur-md supports-[backdrop-filter]:bg-bg/70 sm:px-4">
          <button
            type="button"
            className="flex size-8 items-center justify-center rounded-control text-fg-muted hover:bg-surface-2 focus-ring md:hidden"
            onClick={() => setMobileOpen(true)}
            aria-label="Open navigation"
          >
            <Menu className="size-4" />
          </button>
          <OrgSwitcher />
          {crumbs.length > 0 && (
            <>
              <span className="hidden text-border-strong sm:inline" aria-hidden>
                /
              </span>
              <Breadcrumbs items={crumbs} className="hidden sm:block" />
            </>
          )}
          <div className="ml-auto flex items-center gap-1.5 sm:gap-2">
            <button
              type="button"
              onClick={() => setCommandOpen(true)}
              className={cn(
                "flex h-8 items-center gap-2 rounded-control border border-border bg-surface px-2.5 text-sm text-fg-subtle shadow-xs transition-colors",
                "hover:border-border-strong hover:text-fg-muted focus-ring lg:w-64",
              )}
              aria-label="Search and commands"
              aria-keyshortcuts="Control+K Meta+K"
            >
              <Search className="size-4" aria-hidden />
              <span className="hidden lg:inline">Search or jump to…</span>
              <span className="ml-auto hidden items-center gap-0.5 lg:flex">
                <Kbd>Ctrl</Kbd>
                <Kbd>K</Kbd>
              </span>
            </button>
            <AiStatusPill />
            <NotificationCenter />
            <UserMenu onOpenCommand={() => setCommandOpen(true)} />
          </div>
        </header>
        <main id="main" className="flex-1">
          <div className="mx-auto w-full max-w-[1400px] px-4 py-6 sm:px-6 lg:px-8 lg:py-8">{children}</div>
        </main>
        <footer className="border-t border-border px-4 py-3 text-xs text-fg-subtle sm:px-8">
          <div className="mx-auto flex max-w-[1400px] items-center justify-between gap-2">
            <span>NEXORA · Enterprise AI data platform</span>
            <span className="hidden sm:inline">
              Press <Kbd>Ctrl</Kbd> <Kbd>K</Kbd> for commands · <Kbd>[</Kbd> to toggle the sidebar
            </span>
          </div>
        </footer>
      </div>
      <CommandMenu open={commandOpen} onOpenChange={setCommandOpen} />
    </div>
  );
}
