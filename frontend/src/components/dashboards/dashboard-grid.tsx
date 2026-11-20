"use client";

import * as React from "react";
import {
  cellsFromPixels,
  GRID_COLS,
  GRID_GAP,
  gridItemsFromWidgets,
  keyboardChange,
  moveItem,
  readingOrder,
  ROW_HEIGHT,
  sameLayout,
  type GridItem,
} from "@/lib/dashboards/layout";
import type { DashboardWidget } from "@/lib/types";
import { cn } from "@/lib/utils";
import { WidgetCard } from "./widget-card";

function useIsDesktop() {
  const [desktop, setDesktop] = React.useState(true);
  React.useEffect(() => {
    const mq = window.matchMedia("(min-width: 768px)");
    const on = () => setDesktop(mq.matches);
    on();
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return desktop;
}

function useWidth(ref: React.RefObject<HTMLElement | null>) {
  const [w, setW] = React.useState(0);
  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => setW(el.getBoundingClientRect().width);
    update();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

const HEADER_PX = 41;
const PAD_PX = 24;

function bodyHeight(h: number) {
  return h * ROW_HEIGHT + (h - 1) * GRID_GAP - HEADER_PX - PAD_PX;
}

type Gesture = { kind: "move" | "resize"; id: string; startX: number; startY: number; base: GridItem[]; origin: GridItem };

/**
 * 12-column dashboard grid (CSS grid, no extra dependency). In edit mode a tile is moved by dragging its title
 * bar and resized from its bottom-right corner, snapping to cells; overlapping tiles are pushed down and the grid
 * compacts upwards. Keyboard: focus a tile, arrows move it, Shift+arrows resize it. On phones tiles stack.
 */
export function DashboardGrid({
  org,
  dashboardId,
  widgets,
  editing = false,
  canEdit = false,
  onCommit,
  onEditWidget,
  onRemoveWidget,
}: {
  org: string;
  dashboardId: string;
  widgets: DashboardWidget[];
  editing?: boolean;
  canEdit?: boolean;
  onCommit?: (items: GridItem[]) => void;
  onEditWidget?: (w: DashboardWidget) => void;
  onRemoveWidget?: (w: DashboardWidget) => void;
}) {
  const ref = React.useRef<HTMLDivElement>(null);
  const width = useWidth(ref);
  const desktop = useIsDesktop();
  const [preview, setPreviewState] = React.useState<GridItem[] | null>(null);
  const previewRef = React.useRef<GridItem[] | null>(null);
  const setPreview = React.useCallback((next: GridItem[] | null) => {
    if (next && previewRef.current && sameLayout(previewRef.current, next)) return;
    previewRef.current = next;
    setPreviewState(next);
  }, []);
  const [announce, setAnnounce] = React.useState("");
  const gesture = React.useRef<Gesture | null>(null);

  const stored = React.useMemo(() => gridItemsFromWidgets(widgets), [widgets]);
  const items = preview ?? stored;
  const pos = React.useMemo(() => new Map(items.map((i) => [i.id, i])), [items]);
  const byId = React.useMemo(() => new Map(widgets.map((w) => [w.id, w])), [widgets]);

  const commit = React.useCallback(
    (next: GridItem[]) => {
      if (!sameLayout(next, stored)) onCommit?.(next);
    },
    [onCommit, stored],
  );

  React.useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const g = gesture.current;
      if (!g) return;
      const d = cellsFromPixels(e.clientX - g.startX, e.clientY - g.startY, width || 1200);
      const next =
        g.kind === "move"
          ? moveItem(g.base, g.id, { x: g.origin.x + d.dx, y: g.origin.y + d.dy })
          : moveItem(g.base, g.id, { w: g.origin.w + d.dx, h: g.origin.h + d.dy });
      setPreview(next);
    };
    const onUp = () => {
      const g = gesture.current;
      if (!g) return;
      gesture.current = null;
      document.body.style.removeProperty("user-select");
      const p = previewRef.current;
      setPreview(null);
      if (p) commit(p);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [width, commit, setPreview]);

  const start = (kind: Gesture["kind"], id: string) => (e: React.PointerEvent) => {
    if (!editing || e.button !== 0) return;
    const origin = pos.get(id);
    if (!origin) return;
    e.preventDefault();
    document.body.style.setProperty("user-select", "none");
    gesture.current = { kind, id, startX: e.clientX, startY: e.clientY, base: items, origin };
    setPreview(items);
  };

  const onKey = (id: string) => (e: React.KeyboardEvent) => {
    if (!editing || e.target !== e.currentTarget) return;
    const it = pos.get(id);
    if (!it) return;
    const change = keyboardChange(it, e.key, e.shiftKey);
    if (!change) return;
    e.preventDefault();
    const next = moveItem(items, id, change);
    const moved = next.find((n) => n.id === id)!;
    setAnnounce(`${byId.get(id)?.title ?? "Widget"}: column ${moved.x + 1}, row ${moved.y + 1}, ${moved.w} by ${moved.h}`);
    commit(next);
  };

  if (!desktop) {
    return (
      <div className="space-y-4">
        {readingOrder(items).map((it) => {
          const w = byId.get(it.id);
          if (!w) return null;
          return (
            <div key={w.id} style={{ height: w.kind === "kpi" ? 150 : w.kind === "text" ? "auto" : 340 }}>
              <WidgetCard org={org} dashboardId={dashboardId} widget={w} bodyHeight={w.kind === "kpi" ? 80 : 260} canEdit={canEdit} onEdit={onEditWidget && (() => onEditWidget(w))} onRemove={onRemoveWidget && (() => onRemoveWidget(w))} />
            </div>
          );
        })}
      </div>
    );
  }

  const rows = items.reduce((m, i) => Math.max(m, i.y + i.h), 0);
  return (
    <>
      <div
        ref={ref}
        className={cn("relative grid", editing && "rounded-card outline-dashed outline-1 outline-offset-8 outline-border")}
        style={{
          gridTemplateColumns: `repeat(${GRID_COLS}, minmax(0, 1fr))`,
          gridAutoRows: `${ROW_HEIGHT}px`,
          gap: GRID_GAP,
          minHeight: editing ? (rows + 2) * (ROW_HEIGHT + GRID_GAP) : undefined,
        }}
        data-testid="dashboard-grid"
      >
        {items.map((it) => {
          const w = byId.get(it.id);
          if (!w) return null;
          const active = gesture.current?.id === it.id;
          return (
            <div
              key={w.id}
              style={{ gridColumn: `${it.x + 1} / span ${it.w}`, gridRow: `${it.y + 1} / span ${it.h}` }}
              className={cn("relative min-w-0 transition-[grid-area] duration-100", active && "z-10 opacity-90", editing && "focus-ring rounded-card")}
              tabIndex={editing ? 0 : undefined}
              onKeyDown={onKey(w.id)}
              aria-roledescription={editing ? "movable widget" : undefined}
              aria-label={editing ? `${w.title}. Arrow keys move, Shift plus arrow keys resize.` : undefined}
              data-widget-id={w.id}
              data-layout={`${it.x},${it.y},${it.w},${it.h}`}
            >
              <WidgetCard
                org={org}
                dashboardId={dashboardId}
                widget={w}
                editing={editing}
                canEdit={canEdit}
                bodyHeight={bodyHeight(it.h)}
                onEdit={onEditWidget && (() => onEditWidget(w))}
                onRemove={onRemoveWidget && (() => onRemoveWidget(w))}
                dragHandleProps={{ onPointerDown: start("move", w.id), className: "touch-none" }}
              />
              {editing && (
                <div
                  role="presentation"
                  onPointerDown={start("resize", w.id)}
                  className="absolute bottom-0.5 right-0.5 size-4 cursor-se-resize touch-none rounded-br-card border-b-2 border-r-2 border-accent/70"
                  title="Drag to resize"
                />
              )}
            </div>
          );
        })}
      </div>
      <div className="sr-only" aria-live="polite">
        {announce}
      </div>
    </>
  );
}
