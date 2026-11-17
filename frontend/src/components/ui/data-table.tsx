"use client";

import * as React from "react";
import {
  columnFilteringFeature,
  columnVisibilityFeature,
  createFilteredRowModel,
  createPaginatedRowModel,
  createSortedRowModel,
  filterFns,
  flexRender,
  globalFilteringFeature,
  rowPaginationFeature,
  rowSortingFeature,
  sortFns,
  tableFeatures,
  useTable,
  type ColumnDef,
  type Column,
  type SortingState,
  type ColumnVisibilityState,
  type RowData,
} from "@tanstack/react-table";
import {
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  ChevronsUpDown,
  Columns3,
  Search,
  SearchX,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "./button";
import { Input } from "./input";
import { EmptyState, ErrorState, Skeleton } from "./feedback";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "./overlays";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "./select";

export interface DataTableColumnMeta {
  /** Human label (column visibility menu, mobile cards) when `header` is not a string. */
  label?: string;
  align?: "left" | "right" | "center";
  /** Extra classes for the cells of this column. */
  className?: string;
  /** Hide this column from the mobile card list. */
  hideOnMobile?: boolean;
}

export const dataTableFeatures = tableFeatures({
  rowSortingFeature,
  columnFilteringFeature,
  globalFilteringFeature,
  rowPaginationFeature,
  columnVisibilityFeature,
  filteredRowModel: createFilteredRowModel(),
  sortedRowModel: createSortedRowModel(),
  paginatedRowModel: createPaginatedRowModel(),
  filterFns,
  sortFns,
  columnMeta: {} as DataTableColumnMeta,
});

export type DataTableFeatures = typeof dataTableFeatures;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type DataTableColumn<T extends RowData> = ColumnDef<DataTableFeatures, T, any>;

export interface DataTableFilter {
  columnId: string;
  label: string;
  options: Array<{ label: string; value: string }>;
}

export interface DataTableProps<T extends RowData> {
  columns: DataTableColumn<T>[];
  data: T[] | undefined;
  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  searchable?: boolean;
  searchPlaceholder?: string;
  filters?: DataTableFilter[];
  pageSize?: number;
  pageSizes?: number[];
  initialSorting?: SortingState;
  initialColumnVisibility?: ColumnVisibilityState;
  getRowId?: (row: T, index: number) => string;
  onRowClick?: (row: T) => void;
  emptyState?: React.ReactNode;
  toolbar?: React.ReactNode;
  footer?: React.ReactNode;
  className?: string;
  /** Accessible table caption (visually hidden). */
  caption?: string;
  dense?: boolean;
  /** Show the column visibility menu (default true). */
  columnToggle?: boolean;
}

const ALL = "__all__";

function columnLabel<T extends RowData>(column: Column<DataTableFeatures, T, unknown>): string {
  const meta = column.columnDef.meta as DataTableColumnMeta | undefined;
  if (meta?.label) return meta.label;
  const header = column.columnDef.header;
  return typeof header === "string" ? header : column.id;
}

export function DataTable<T extends RowData>({
  columns,
  data,
  loading,
  error,
  onRetry,
  searchable = true,
  searchPlaceholder = "Search…",
  filters,
  pageSize = 20,
  pageSizes = [10, 20, 50, 100],
  initialSorting,
  initialColumnVisibility,
  getRowId,
  onRowClick,
  emptyState,
  toolbar,
  footer,
  className,
  caption,
  dense,
  columnToggle = true,
}: DataTableProps<T>) {
  const rows = React.useMemo(() => data ?? [], [data]);
  const filterIds = React.useMemo(() => new Set((filters ?? []).map((f) => f.columnId)), [filters]);
  const cols = React.useMemo(
    () =>
      columns.map((c) => {
        const id = (c as { id?: string; accessorKey?: string }).id ?? (c as { accessorKey?: string }).accessorKey;
        return id && filterIds.has(id) ? ({ ...c, filterFn: "equalsString" } as DataTableColumn<T>) : c;
      }),
    [columns, filterIds],
  );

  const table = useTable({
    features: dataTableFeatures,
    columns: cols,
    data: rows,
    getRowId,
    globalFilterFn: "includesString",
    initialState: {
      sorting: initialSorting ?? [],
      columnVisibility: initialColumnVisibility ?? {},
      pagination: { pageIndex: 0, pageSize },
      globalFilter: "",
      columnFilters: [],
    },
  });

  const state = table.state;
  const globalFilter = (state.globalFilter as string) ?? "";
  const isFiltered = globalFilter !== "" || state.columnFilters.length > 0;
  const filteredCount = table.getFilteredRowModel().rows.length;
  const pageRows = table.getRowModel().rows;
  const { pageIndex, pageSize: currentSize } = state.pagination;
  const pageCount = Math.max(1, table.getPageCount());
  const from = filteredCount === 0 ? 0 : pageIndex * currentSize + 1;
  const to = Math.min(filteredCount, (pageIndex + 1) * currentSize);
  const visibleLeaf = table.getVisibleLeafColumns();
  const hideable = table.getAllLeafColumns().filter((c) => c.getCanHide());

  const showToolbar = searchable || (filters && filters.length > 0) || toolbar || columnToggle;

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      {showToolbar && (
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <div className="flex flex-1 flex-wrap items-center gap-2">
            {searchable && (
              <Input
                aria-label="Search table"
                className="w-full sm:w-64"
                placeholder={searchPlaceholder}
                value={globalFilter}
                onChange={(e) => {
                  table.setGlobalFilter(e.target.value);
                  table.setPageIndex(0);
                }}
                leading={<Search />}
                trailing={
                  globalFilter ? (
                    <button
                      type="button"
                      aria-label="Clear search"
                      className="flex size-6 items-center justify-center rounded-[4px] hover:bg-surface-2 hover:text-fg"
                      onClick={() => table.setGlobalFilter("")}
                    >
                      <X className="size-3.5" />
                    </button>
                  ) : undefined
                }
              />
            )}
            {filters?.map((f) => {
              const column = table.getColumn(f.columnId);
              if (!column) return null;
              const value = (column.getFilterValue() as string | undefined) ?? ALL;
              return (
                <Select
                  key={f.columnId}
                  value={value}
                  onValueChange={(v) => {
                    column.setFilterValue(v === ALL ? undefined : v);
                    table.setPageIndex(0);
                  }}
                >
                  <SelectTrigger className="h-9 w-auto min-w-36" aria-label={`Filter by ${f.label}`}>
                    <span className="text-fg-subtle">{f.label}:</span>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ALL}>All</SelectItem>
                    {f.options.map((o) => (
                      <SelectItem key={o.value} value={o.value}>
                        {o.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              );
            })}
            {isFiltered && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  table.setGlobalFilter("");
                  table.setColumnFilters([]);
                }}
              >
                Reset <X />
              </Button>
            )}
          </div>
          <div className="flex items-center gap-2">
            {toolbar}
            {columnToggle && hideable.length > 1 && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="secondary" size="sm" aria-label="Toggle columns">
                    <Columns3 /> <span className="hidden sm:inline">Columns</span>
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuLabel>Visible columns</DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  {hideable.map((c) => (
                    <DropdownMenuCheckboxItem
                      key={c.id}
                      checked={c.getIsVisible()}
                      onCheckedChange={(v) => c.toggleVisibility(!!v)}
                      onSelect={(e) => e.preventDefault()}
                    >
                      {columnLabel(c)}
                    </DropdownMenuCheckboxItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </div>
        </div>
      )}

      <div className="overflow-hidden rounded-card border border-border bg-surface shadow-xs">
        {error ? (
          <ErrorState error={error} onRetry={onRetry} compact />
        ) : (
          <>
            {/* Desktop / tablet table */}
            <div className="scrollbar-thin hidden overflow-x-auto md:block">
              <table className="w-full border-collapse text-sm">
                {caption && <caption className="sr-only">{caption}</caption>}
                <thead>
                  {table.getHeaderGroups().map((hg) => (
                    <tr key={hg.id} className="border-b border-border bg-surface-2/70">
                      {hg.headers.map((header) => {
                        const meta = header.column.columnDef.meta as DataTableColumnMeta | undefined;
                        const sorted = header.column.getIsSorted();
                        const canSort = header.column.getCanSort();
                        const content = header.isPlaceholder
                          ? null
                          : flexRender(header.column.columnDef.header, header.getContext());
                        return (
                          <th
                            key={header.id}
                            scope="col"
                            aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined}
                            className={cn(
                              "h-9 whitespace-nowrap px-4 text-xs font-medium text-fg-muted",
                              meta?.align === "right" ? "text-right" : meta?.align === "center" ? "text-center" : "text-left",
                            )}
                          >
                            {canSort ? (
                              <button
                                type="button"
                                onClick={header.column.getToggleSortingHandler()}
                                className={cn(
                                  "group/sort -mx-1 inline-flex items-center gap-1 rounded-[4px] px-1 py-0.5 transition-colors hover:text-fg focus-ring",
                                  sorted && "text-fg",
                                  meta?.align === "right" && "flex-row-reverse",
                                )}
                              >
                                {content}
                                {sorted === "asc" ? (
                                  <ArrowUp className="size-3" aria-hidden />
                                ) : sorted === "desc" ? (
                                  <ArrowDown className="size-3" aria-hidden />
                                ) : (
                                  <ChevronsUpDown className="size-3 opacity-0 transition-opacity group-hover/sort:opacity-60" aria-hidden />
                                )}
                              </button>
                            ) : (
                              content
                            )}
                          </th>
                        );
                      })}
                    </tr>
                  ))}
                </thead>
                <tbody>
                  {loading
                    ? Array.from({ length: Math.min(currentSize, 6) }).map((_, i) => (
                        <tr key={i} className="border-b border-border last:border-0">
                          {visibleLeaf.map((c) => (
                            <td key={c.id} className={cn("px-4", dense ? "py-2" : "py-3")}>
                              <Skeleton className="h-3.5" style={{ width: `${45 + ((i * 13 + c.id.length * 7) % 45)}%` }} />
                            </td>
                          ))}
                        </tr>
                      ))
                    : pageRows.map((row) => (
                        <tr
                          key={row.id}
                          onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                          onKeyDown={
                            onRowClick
                              ? (e) => {
                                  if (e.key === "Enter") onRowClick(row.original);
                                }
                              : undefined
                          }
                          tabIndex={onRowClick ? 0 : undefined}
                          className={cn(
                            "border-b border-border transition-colors last:border-0",
                            onRowClick && "cursor-pointer hover:bg-surface-2/60 focus-visible:bg-accent-soft/50 focus-visible:outline-none",
                          )}
                        >
                          {row.getVisibleCells().map((cell) => {
                            const meta = cell.column.columnDef.meta as DataTableColumnMeta | undefined;
                            return (
                              <td
                                key={cell.id}
                                className={cn(
                                  "px-4 align-middle text-fg",
                                  dense ? "py-2" : "py-2.5",
                                  meta?.align === "right" && "tabular text-right",
                                  meta?.align === "center" && "text-center",
                                  meta?.className,
                                )}
                              >
                                {flexRender(cell.column.columnDef.cell, cell.getContext())}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                </tbody>
              </table>
            </div>

            {/* Mobile card list */}
            <ul className="divide-y divide-border md:hidden" aria-label={caption}>
              {loading
                ? Array.from({ length: 4 }).map((_, i) => (
                    <li key={i} className="space-y-2 p-4">
                      <Skeleton className="h-4 w-1/2" />
                      <Skeleton className="h-3 w-3/4" />
                    </li>
                  ))
                : pageRows.map((row) => {
                    const cells = row.getVisibleCells().filter((c) => {
                      const m = c.column.columnDef.meta as DataTableColumnMeta | undefined;
                      return !m?.hideOnMobile;
                    });
                    const [first, ...rest] = cells;
                    return (
                      <li
                        key={row.id}
                        onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                        className={cn("space-y-2 p-4", onRowClick && "cursor-pointer active:bg-surface-2")}
                      >
                        {first && (
                          <div className="font-medium text-fg">
                            {flexRender(first.column.columnDef.cell, first.getContext())}
                          </div>
                        )}
                        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                          {rest.map((cell) => (
                            <React.Fragment key={cell.id}>
                              <dt className="text-fg-subtle">{columnLabel(cell.column)}</dt>
                              <dd className="min-w-0 truncate text-right text-fg">
                                {flexRender(cell.column.columnDef.cell, cell.getContext())}
                              </dd>
                            </React.Fragment>
                          ))}
                        </dl>
                      </li>
                    );
                  })}
            </ul>

            {!loading && pageRows.length === 0 && (
              <div className="border-t border-border first:border-t-0">
                {isFiltered ? (
                  <EmptyState
                    compact
                    icon={<SearchX />}
                    title="No matching results"
                    description="Try a different search term or clear the filters."
                    action={
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => {
                          table.setGlobalFilter("");
                          table.setColumnFilters([]);
                        }}
                      >
                        Clear filters
                      </Button>
                    }
                  />
                ) : (
                  (emptyState ?? <EmptyState compact title="No data yet" description="Nothing to show here." />)
                )}
              </div>
            )}
          </>
        )}

        {!error && !loading && filteredCount > 0 && (
          <div className="flex flex-col gap-2 border-t border-border px-4 py-2.5 text-xs text-fg-muted sm:flex-row sm:items-center sm:justify-between">
            <span className="tabular" aria-live="polite">
              {from}–{to} of {filteredCount}
              {isFiltered && rows.length !== filteredCount ? ` (filtered from ${rows.length})` : ""}
            </span>
            <div className="flex items-center gap-3">
              <label className="flex items-center gap-2">
                <span className="hidden sm:inline">Rows</span>
                <select
                  className="h-7 rounded-control border border-border bg-surface px-1.5 text-xs text-fg focus-ring"
                  value={currentSize}
                  onChange={(e) => table.setPageSize(Number(e.target.value))}
                  aria-label="Rows per page"
                >
                  {pageSizes.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </label>
              <span className="tabular">
                Page {pageIndex + 1} / {pageCount}
              </span>
              <div className="flex items-center gap-1">
                <Button
                  variant="secondary"
                  size="icon-xs"
                  onClick={() => table.previousPage()}
                  disabled={!table.getCanPreviousPage()}
                  aria-label="Previous page"
                >
                  <ChevronLeft />
                </Button>
                <Button
                  variant="secondary"
                  size="icon-xs"
                  onClick={() => table.nextPage()}
                  disabled={!table.getCanNextPage()}
                  aria-label="Next page"
                >
                  <ChevronRight />
                </Button>
              </div>
            </div>
          </div>
        )}
        {footer && <div className="border-t border-border px-4 py-2.5">{footer}</div>}
      </div>
    </div>
  );
}
