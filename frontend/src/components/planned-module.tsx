"use client";

import * as React from "react";
import Link from "next/link";
import { motion, useReducedMotion } from "framer-motion";
import { ArrowRight, CalendarClock, Check } from "lucide-react";
import { useOrgContext } from "@/components/shell/org-context";
import { PageHeader } from "@/components/ui/page-header";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FeatureStatusBadge } from "@/components/ui/status";
import { ALL_NAV_ITEMS, navItem, orgHref } from "@/lib/nav";

/**
 * Honest placeholder for modules that aren't built yet (UI/UX principle 5): no sample data,
 * just what the module will do and where to go meanwhile.
 */
export function PlannedModule({ id }: { id: string }) {
  const item = navItem(id);
  const { slug, can } = useOrgContext();
  const reduce = useReducedMotion();
  const Icon = item.icon;
  const working = ALL_NAV_ITEMS.filter((i) => i.status === "working" && i.id !== "settings" && (!i.perm || can(i.perm))).slice(0, 4);

  return (
    <div className="space-y-6">
      <PageHeader title={item.label} badge={<FeatureStatusBadge status="planned" />} description={item.summary} />
      <Card className="relative overflow-hidden">
        <div className="bg-grid pointer-events-none absolute inset-0 opacity-40 [mask-image:radial-gradient(ellipse_at_top,black_20%,transparent_70%)]" aria-hidden />
        <div className="relative grid gap-8 p-6 sm:p-10 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] lg:items-center">
          <div className="space-y-5">
            <motion.span
              initial={reduce ? false : { opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ duration: 0.22 }}
              className="relative flex size-14 items-center justify-center rounded-modal border border-border bg-surface shadow-soft"
            >
              <span className="absolute inset-0 -z-10 scale-150 rounded-full bg-accent/15 blur-2xl" aria-hidden />
              <Icon className="size-6 text-accent" aria-hidden />
            </motion.span>
            <div className="space-y-2">
              <h2 className="text-lg font-semibold tracking-[-0.01em] text-fg">{item.label} is on the roadmap</h2>
              <p className="max-w-md text-sm text-fg-muted">
                This module isn&apos;t available yet, so nothing here is simulated. It will appear in this space, wired to
                real data, when it ships.
              </p>
            </div>
            {item.phase && (
              <Badge tone="outline" size="md">
                <CalendarClock /> Planned for {item.phase}
              </Badge>
            )}
          </div>
          {item.capabilities && item.capabilities.length > 0 && (
            <div className="rounded-card border border-border bg-surface/80 p-5 backdrop-blur-sm">
              <div className="mb-3 text-xs font-semibold uppercase tracking-[0.08em] text-fg-subtle">What it will do</div>
              <ul className="space-y-3">
                {item.capabilities.map((c, i) => (
                  <motion.li
                    key={c}
                    initial={reduce ? false : { opacity: 0, x: -4 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ duration: 0.18, delay: 0.05 * i }}
                    className="flex gap-2.5 text-sm text-fg"
                  >
                    <span className="mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full bg-accent-soft text-accent-soft-fg">
                      <Check className="size-3" aria-hidden />
                    </span>
                    {c}
                  </motion.li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </Card>
      {working.length > 0 && (
        <section aria-labelledby="available-now" className="space-y-3">
          <h2 id="available-now" className="text-sm font-semibold text-fg">
            Available now
          </h2>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {working.map((w) => {
              const WIcon = w.icon;
              return (
                <Button key={w.id} variant="secondary" className="h-auto justify-start py-3" asChild>
                  <Link href={orgHref(slug, w.path)}>
                    <WIcon className="text-fg-subtle" />
                    <span className="truncate">{w.label}</span>
                    <ArrowRight className="ml-auto text-fg-subtle" />
                  </Link>
                </Button>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}
