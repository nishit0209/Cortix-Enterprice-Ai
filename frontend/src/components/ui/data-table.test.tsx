import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DataTable, type DataTableColumn } from "./data-table";
import { TooltipProvider } from "./overlays";
import { ApiError } from "@/lib/api";

interface Row {
  id: string;
  name: string;
  kind: string;
  rows: number;
}

const DATA: Row[] = Array.from({ length: 25 }, (_, i) => ({
  id: `r${i}`,
  name: `Dataset ${String(i).padStart(2, "0")}`,
  kind: i % 3 === 0 ? "synthetic" : "internal",
  rows: (i * 37) % 100,
}));

const columns: DataTableColumn<Row>[] = [
  { accessorKey: "name", header: "Name" },
  { accessorKey: "kind", header: "Kind" },
  { accessorKey: "rows", header: "Rows", meta: { align: "right" } },
];

function setup(props: Partial<React.ComponentProps<typeof DataTable<Row>>> = {}) {
  const user = userEvent.setup();
  render(
    <TooltipProvider>
      <DataTable<Row> caption="Test table" columns={columns} data={DATA} pageSize={10} getRowId={(r) => r.id} {...props} />
    </TooltipProvider>,
  );
  const table = () => screen.getByRole("table");
  const bodyRows = () => within(table()).getAllByRole("row").slice(1);
  return { user, table, bodyRows };
}

describe("DataTable", () => {
  it("renders headers and paginates rows", async () => {
    const { user, bodyRows } = setup();
    expect(screen.getByRole("columnheader", { name: /name/i })).toBeInTheDocument();
    expect(bodyRows()).toHaveLength(10);
    expect(screen.getByText("1–10 of 25")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByText("11–20 of 25")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next page" }));
    expect(bodyRows()).toHaveLength(5);
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
  });

  it("sorts when a sortable header is clicked", async () => {
    const { user, bodyRows } = setup();
    await user.click(within(screen.getByRole("columnheader", { name: /rows/i })).getByRole("button"));
    const header = screen.getByRole("columnheader", { name: /rows/i });
    const dir = header.getAttribute("aria-sort");
    expect(["ascending", "descending"]).toContain(dir);
    const values = bodyRows().map((r) => Number(within(r).getAllByRole("cell")[2].textContent));
    const sorted = [...values].sort((a, b) => (dir === "ascending" ? a - b : b - a));
    expect(values).toEqual(sorted);
  });

  it("filters with the global search and offers a reset", async () => {
    const { user, bodyRows } = setup();
    await user.type(screen.getByRole("textbox", { name: "Search table" }), "Dataset 07");
    expect(bodyRows()).toHaveLength(1);
    expect(screen.getByText("1–1 of 1 (filtered from 25)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear search" }));
    expect(bodyRows()).toHaveLength(10);
  });

  it("shows a 'no matches' state when the search excludes everything", async () => {
    const { user } = setup();
    await user.type(screen.getByRole("textbox", { name: "Search table" }), "zzz-no-match");
    expect(screen.getAllByText("No matching results").length).toBeGreaterThan(0);
  });

  it("renders skeletons while loading and the empty state when there is no data", () => {
    const { unmount } = render(<DataTable<Row> columns={columns} data={[]} emptyState={<p>Nothing here yet</p>} />);
    expect(screen.getByText("Nothing here yet")).toBeInTheDocument();
    unmount();
    render(<DataTable<Row> columns={columns} data={undefined} loading />);
    expect(screen.queryByText("Nothing here yet")).not.toBeInTheDocument();
    expect(document.querySelectorAll(".skeleton").length).toBeGreaterThan(0);
  });

  it("renders the problem details for errors with a retry action", async () => {
    const onRetry = vi.fn();
    const user = userEvent.setup();
    render(
      <TooltipProvider>
        <DataTable<Row>
          columns={columns}
          data={undefined}
          error={new ApiError({ status: 500, code: "server_error", detail: "Boom", requestId: "req-9" })}
          onRetry={onRetry}
        />
      </TooltipProvider>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Boom");
    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("invokes onRowClick with the original row", async () => {
    const onRowClick = vi.fn();
    const { user, bodyRows } = setup({ onRowClick });
    await user.click(bodyRows()[0]);
    expect(onRowClick).toHaveBeenCalledWith(DATA[0]);
  });
});
