"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Command } from "cmdk";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { useTheme } from "next-themes";
import { ArrowRight, Building2, LogOut, Moon, Search, Sparkles, Sun, Upload, UserPlus, Workflow } from "lucide-react";
import { Kbd } from "@/components/ui/feedback";
import { FeatureStatusBadge } from "@/components/ui/status";
import { orgHref } from "@/lib/nav";
import { useOrgContext } from "./org-context";
import { useVisibleNav } from "./sidebar";
import { useSignOut } from "./user-menu";

const itemCls =
  "flex h-9 cursor-default select-none items-center gap-2.5 rounded-control px-2.5 text-sm text-fg outline-none data-[selected=true]:bg-surface-2 data-[disabled=true]:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-fg-subtle";
const groupCls =
  "[&_[cmdk-group-heading]]:px-2.5 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:text-[11px] [&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-[0.08em] [&_[cmdk-group-heading]]:text-fg-subtle";

export function CommandMenu({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const router = useRouter();
  const { slug, me, can } = useOrgContext();
  const sections = useVisibleNav();
  const { resolvedTheme, setTheme } = useTheme();
  const signOut = useSignOut();

  const run = React.useCallback(
    (fn: () => void) => {
      onOpenChange(false);
      // Let the dialog close before navigating so focus returns cleanly.
      requestAnimationFrame(fn);
    },
    [onOpenChange],
  );

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-overlay backdrop-blur-[2px] data-[state=open]:animate-fade" />
        <DialogPrimitive.Content
          className="fixed left-1/2 top-[12vh] z-50 w-[calc(100vw-24px)] max-w-xl -translate-x-1/2 overflow-hidden rounded-modal border border-border bg-surface shadow-pop focus:outline-none data-[state=open]:animate-pop"
          aria-describedby={undefined}
        >
          <DialogPrimitive.Title className="sr-only">Command menu</DialogPrimitive.Title>
          <Command label="Command menu" loop>
            <div className="flex items-center gap-2.5 border-b border-border px-4">
              <Search className="size-4 shrink-0 text-fg-subtle" aria-hidden />
              <Command.Input
                autoFocus
                placeholder="Search pages and actions…"
                className="h-12 w-full bg-transparent text-sm text-fg outline-none placeholder:text-fg-subtle"
              />
              <Kbd>Esc</Kbd>
            </div>
            <Command.List className="scrollbar-thin max-h-[min(420px,60vh)] overflow-y-auto p-2">
              <Command.Empty className="py-10 text-center text-sm text-fg-muted">No results found.</Command.Empty>

              <Command.Group heading="Quick actions" className={groupCls}>
                {can("datasets.create") && (
                  <Command.Item
                    className={itemCls}
                    value="upload dataset csv file"
                    onSelect={() => run(() => router.push(orgHref(slug, "/data/datasets?upload=1")))}
                  >
                    <Upload /> Upload dataset
                  </Command.Item>
                )}
                {can("bi.chat") && (
                  <Command.Item className={itemCls} value="ask ai question" onSelect={() => run(() => router.push(orgHref(slug, "/ask")))}>
                    <Sparkles /> Ask AI
                  </Command.Item>
                )}
                {can("pipelines.view") && (
                  <Command.Item
                    className={itemCls}
                    value="new pipeline"
                    onSelect={() => run(() => router.push(orgHref(slug, "/data/pipelines?new=1")))}
                  >
                    <Workflow /> New pipeline
                  </Command.Item>
                )}
                {can("members.manage") && (
                  <Command.Item
                    className={itemCls}
                    value="invite member teammate"
                    onSelect={() => run(() => router.push(orgHref(slug, "/settings/members?invite=1")))}
                  >
                    <UserPlus /> Invite a teammate
                  </Command.Item>
                )}
                <Command.Item
                  className={itemCls}
                  value="toggle theme dark light"
                  onSelect={() => run(() => setTheme(resolvedTheme === "dark" ? "light" : "dark"))}
                >
                  {resolvedTheme === "dark" ? <Sun /> : <Moon />} Switch to {resolvedTheme === "dark" ? "light" : "dark"} theme
                </Command.Item>
              </Command.Group>

              {sections.map((s) => (
                <Command.Group key={s.id} heading={s.label ?? "Navigate"} className={groupCls}>
                  {s.items.map((item) => {
                    const Icon = item.icon;
                    return (
                      <Command.Item
                        key={item.id}
                        className={itemCls}
                        value={`${item.label} ${(item.keywords ?? []).join(" ")}`}
                        onSelect={() => run(() => router.push(orgHref(slug, item.path)))}
                      >
                        <Icon /> {item.label}
                        {item.status === "planned" ? (
                          <FeatureStatusBadge status="planned" className="ml-auto" />
                        ) : (
                          <ArrowRight className="ml-auto opacity-0 [[data-selected=true]_&]:opacity-100" />
                        )}
                      </Command.Item>
                    );
                  })}
                </Command.Group>
              ))}

              {me.orgs.length > 1 && (
                <Command.Group heading="Switch organization" className={groupCls}>
                  {me.orgs
                    .filter((o) => o.slug !== slug)
                    .map((o) => (
                      <Command.Item
                        key={o.id}
                        className={itemCls}
                        value={`switch org ${o.name} ${o.slug}`}
                        onSelect={() => run(() => router.push(`/o/${o.slug}/dashboard`))}
                      >
                        <Building2 /> {o.name}
                      </Command.Item>
                    ))}
                </Command.Group>
              )}

              <Command.Group heading="Account" className={groupCls}>
                <Command.Item className={itemCls} value="sign out logout" onSelect={() => run(() => void signOut())}>
                  <LogOut /> Sign out
                </Command.Item>
              </Command.Group>
            </Command.List>
            <div className="flex items-center justify-between border-t border-border bg-surface-2/60 px-4 py-2 text-xs text-fg-subtle">
              <span className="flex items-center gap-1.5">
                <Kbd>↑</Kbd>
                <Kbd>↓</Kbd> navigate
              </span>
              <span className="flex items-center gap-1.5">
                <Kbd>↵</Kbd> open
              </span>
            </div>
          </Command>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
