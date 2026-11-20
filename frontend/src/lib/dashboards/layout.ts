/**
 * Dashboard grid model (pure). Widgets sit on a 12-column grid with integer `{x, y, w, h}` positions, the same
 * shape the backend stores (`apps/bi` DashboardWidget.layout: x + w <= 12, 1 <= h <= 24). Moving or resizing a
 * widget pushes overlapping widgets down and then compacts the grid upwards, so the layout never overlaps.
 * `layoutPatch` builds the bulk PATCH body `layout: [{id, x, y, w, h}]`.
 */

import type { WidgetKind, WidgetLayout } from "@/lib/types";

export const GRID_COLS = 12;
export const MAX_H = 24;
export const MAX_Y = 1000;
export const ROW_HEIGHT = 76;
export const GRID_GAP = 16;

export interface GridItem extends WidgetLayout {
  id: string;
}

const int = (v: unknown, fallback: number) => (typeof v === "number" && Number.isFinite(v) ? Math.round(v) : fallback);

/** Clamp a position into the grid (integers, x + w <= 12, 1 <= h <= 24, y >= 0). */
export function clampItem<T extends GridItem>(it: T): T {
  const w = Math.min(GRID_COLS, Math.max(1, int(it.w, 6)));
  const h = Math.min(MAX_H, Math.max(1, int(it.h, 4)));
  const x = Math.min(GRID_COLS - w, Math.max(0, int(it.x, 0)));
  const y = Math.min(MAX_Y, Math.max(0, int(it.y, 0)));
  return { ...it, x, y, w, h };
}

export function collides(a: WidgetLayout, b: WidgetLayout): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

function byPosition(priority?: string) {
  return (a: GridItem, b: GridItem) => a.y - b.y || (priority ? Number(b.id === priority) - Number(a.id === priority) : 0) || a.x - b.x || a.id.localeCompare(b.id);
}

/**
 * Vertical compaction: every item moves up as far as it can without overlapping the items placed before it.
 * `priority` wins ties at the same row (the widget being dragged keeps its spot).
 */
export function compact<T extends GridItem>(items: T[], priority?: string): T[] {
  const placed: T[] = [];
  for (const raw of [...items].map(clampItem).sort(byPosition(priority))) {
    let it = { ...raw };
    // Resolve any overlap first (push below the lowest collider), then float up.
    for (let guard = 0; guard < 500; guard++) {
      const hit = placed.filter((p) => collides(p, it));
      if (!hit.length) break;
      it = { ...it, y: Math.max(...hit.map((p) => p.y + p.h)) };
    }
    while (it.y > 0 && !placed.some((p) => collides(p, { ...it, y: it.y - 1 }))) it = { ...it, y: it.y - 1 };
    placed.push(it);
  }
  return placed;
}

/**
 * Widgets as returned by `GET /dashboards/{id}` -> grid items: clamped to the backend's bounds (a missing or
 * malformed layout falls back to the backend default 6x4) and compacted, so stored positions never overlap.
 */
export function gridItemsFromWidgets(widgets: ReadonlyArray<{ id: string; layout?: Partial<WidgetLayout> | null }>): GridItem[] {
  return compact(widgets.map((w) => clampItem({ x: 0, y: 0, w: 6, h: 4, ...(w.layout ?? {}), id: w.id })));
}

/** Place `id` at `to` (clamped), push the others out of the way, and compact. Returns a new list in input order. */
export function moveItem<T extends GridItem>(items: T[], id: string, to: Partial<WidgetLayout>): T[] {
  const target = items.find((i) => i.id === id);
  if (!target) return items;
  const moved = clampItem({ ...target, ...to });
  const out: T[] = [moved];
  for (const o of items.filter((i) => i.id !== id).sort(byPosition())) {
    let it = clampItem(o);
    for (let guard = 0; guard < 500; guard++) {
      const hit = out.filter((p) => collides(p, it));
      if (!hit.length) break;
      it = { ...it, y: Math.max(...hit.map((p) => p.y + p.h)) };
    }
    out.push(it);
  }
  const compacted = compact(out, id);
  const byId = new Map(compacted.map((c) => [c.id, c]));
  return items.map((i) => byId.get(i.id) ?? i);
}

export function resizeItem<T extends GridItem>(items: T[], id: string, size: { w: number; h: number }): T[] {
  return moveItem(items, id, { w: size.w, h: size.h });
}

/** Keyboard editing: arrows move by one cell, Shift+arrows resize. Returns null for keys it does not handle. */
export function keyboardChange(it: WidgetLayout, key: string, shift: boolean): Partial<WidgetLayout> | null {
  const d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[key];
  if (!d) return null;
  return shift ? { w: it.w + d[0], h: it.h + d[1] } : { x: it.x + d[0], y: it.y + d[1] };
}

/** First free row below every widget. */
export function nextFreeY(items: WidgetLayout[]): number {
  return items.reduce((m, i) => Math.max(m, i.y + i.h), 0);
}

export function defaultSize(kind: WidgetKind): { w: number; h: number } {
  switch (kind) {
    case "kpi":
      return { w: 3, h: 2 };
    case "table":
      return { w: 6, h: 5 };
    case "text":
      return { w: 4, h: 3 };
    default:
      return { w: 6, h: 4 };
  }
}

/** A position for a new widget: the first gap of its size scanning rows top-down, else below everything. */
export function placeNew(items: WidgetLayout[], size: { w: number; h: number }): WidgetLayout {
  const w = Math.min(GRID_COLS, Math.max(1, size.w));
  const h = Math.min(MAX_H, Math.max(1, size.h));
  const bottom = nextFreeY(items);
  for (let y = 0; y <= bottom; y++) {
    for (let x = 0; x + w <= GRID_COLS; x++) {
      const cand = { x, y, w, h };
      if (!items.some((i) => collides(i, cand))) return cand;
    }
  }
  return { x: 0, y: bottom, w, h };
}

/** Convert a pointer delta in pixels into whole grid cells. */
export function cellsFromPixels(dx: number, dy: number, containerWidth: number, gap = GRID_GAP, rowHeight = ROW_HEIGHT): { dx: number; dy: number } {
  const colWidth = (containerWidth - gap * (GRID_COLS - 1)) / GRID_COLS;
  const r = (v: number) => (Object.is(Math.round(v), -0) ? 0 : Math.round(v));
  return { dx: r(dx / (colWidth + gap)), dy: r(dy / (rowHeight + gap)) };
}

/** Bulk PATCH body for `PATCH /dashboards/{id}`: every widget's position (`[{id, x, y, w, h}]`). */
export function layoutPatch(items: GridItem[]): { layout: GridItem[] } {
  return { layout: items.map((i) => ({ id: i.id, x: i.x, y: i.y, w: i.w, h: i.h })) };
}

/** True when two layouts put every widget at the same place. */
export function sameLayout(a: GridItem[], b: GridItem[]): boolean {
  if (a.length !== b.length) return false;
  const m = new Map(b.map((i) => [i.id, i]));
  return a.every((i) => {
    const o = m.get(i.id);
    return !!o && o.x === i.x && o.y === i.y && o.w === i.w && o.h === i.h;
  });
}

/** Reading order for small screens (row-major). */
export function readingOrder<T extends GridItem>(items: T[]): T[] {
  return [...items].sort((a, b) => a.y - b.y || a.x - b.x);
}
