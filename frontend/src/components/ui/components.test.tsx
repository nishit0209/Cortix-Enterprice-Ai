import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ScoreRing, scoreTone } from "./score-ring";
import { ClassificationBadge, FeatureStatusBadge, StatusBadge } from "./status";
import { Sparkline, StatCard } from "./stat-card";
import { ConfirmDialog } from "./confirm-dialog";
import { Dropzone, validateFile } from "./dropzone";
import { aiHealth } from "@/components/shell/ai-status-pill";
import { normalizeHistogram, normalizeTopValues } from "@/components/datasets/profile";
import { safeNext } from "@/lib/routing";
import { permissionsAllow } from "@/lib/permissions";
import { activeNavItem } from "@/lib/nav";

describe("ScoreRing", () => {
  it("labels the score accessibly and never invents a value for null", () => {
    const { rerender } = render(<ScoreRing score={87.4} label="Quality" />);
    expect(screen.getByRole("img", { name: "Quality: 87 out of 100" })).toBeInTheDocument();
    rerender(<ScoreRing score={null} label="Quality" />);
    expect(screen.getByRole("img", { name: "Quality: not available" })).toHaveTextContent("—");
  });

  it("maps scores to tones", () => {
    expect(scoreTone(90)).toBe("success");
    expect(scoreTone(70)).toBe("warning");
    expect(scoreTone(10)).toBe("danger");
    expect(scoreTone(null)).toBe("neutral");
  });
});

describe("badges", () => {
  it("renders status and feature maturity labels", () => {
    render(
      <>
        <StatusBadge status="running" />
        <StatusBadge status="succeeded" />
        <FeatureStatusBadge status="planned" />
        <FeatureStatusBadge status="working" />
        <ClassificationBadge value="synthetic" />
      </>,
    );
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("Succeeded")).toBeInTheDocument();
    expect(screen.getByText("Planned")).toBeInTheDocument();
    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.getByText("Synthetic")).toHaveAttribute("title", expect.stringMatching(/Eligible for AI/));
  });
});

describe("StatCard / Sparkline", () => {
  it("does not draw a sparkline with fewer than two points", () => {
    const { container, rerender } = render(<Sparkline data={[5]} />);
    expect(container.querySelector("svg")).toBeNull();
    rerender(<Sparkline data={[1, 4, 2]} />);
    expect(screen.getByTestId("sparkline")).toBeInTheDocument();
  });

  it("renders the value or a loading skeleton", () => {
    const { rerender, container } = render(<StatCard label="Total datasets" value="12" hint="3.4K rows" />);
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("3.4K rows")).toBeInTheDocument();
    rerender(<StatCard label="Total datasets" value="12" loading />);
    expect(screen.queryByText("12")).not.toBeInTheDocument();
    expect(container.querySelectorAll(".skeleton").length).toBeGreaterThan(0);
  });
});

describe("ConfirmDialog", () => {
  it("requires typing the confirmation text before confirming", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        open
        onOpenChange={() => {}}
        title="Delete dataset?"
        confirmLabel="Delete"
        confirmText="sales"
        onConfirm={onConfirm}
      />,
    );
    const btn = screen.getByRole("button", { name: "Delete" });
    expect(btn).toBeDisabled();
    await user.type(screen.getByRole("textbox"), "sales");
    expect(btn).toBeEnabled();
    await user.click(btn);
    expect(onConfirm).toHaveBeenCalledOnce();
  });
});

describe("Dropzone", () => {
  it("validates type and size", () => {
    const csv = new File(["a,b\n1,2"], "data.csv", { type: "text/csv" });
    const exe = new File(["MZ"], "x.exe");
    expect(validateFile(csv, [".csv"], 1024)).toBeNull();
    expect(validateFile(exe, [".csv"])).toMatch(/Unsupported/);
    expect(validateFile(csv, [".csv"], 2)).toMatch(/too large/);
  });

  it("accepts a chosen file and rejects an unsupported one", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const onFile = vi.fn();
    const onReject = vi.fn();
    render(<Dropzone accept={[".csv"]} onFile={onFile} onReject={onReject} />);
    const input = screen.getByTestId("dropzone-input") as HTMLInputElement;
    await user.upload(input, new File(["a"], "ok.csv"));
    expect(onFile).toHaveBeenCalledOnce();
    await user.upload(input, new File(["a"], "bad.pdf"));
    expect(onReject).toHaveBeenCalledWith(expect.stringMatching(/Unsupported/));
  });
});

describe("helpers", () => {
  it("computes AI health from status", () => {
    const base = { policy: "synthetic_only" as const, tier: "free", configured: true, breaker: "closed" as const, resets_at: null };
    expect(aiHealth({ ...base, org_budget: { limit: 50, used: 10 } }).tone).toBe("success");
    expect(aiHealth({ ...base, org_budget: { limit: 50, used: 45 } }).tone).toBe("warning");
    expect(aiHealth({ ...base, org_budget: { limit: 50, used: 50 } }).tone).toBe("danger");
    expect(aiHealth({ ...base, breaker: "open_day", org_budget: { limit: 50, used: 1 } }).label).toBe("Paused for today");
    expect(aiHealth(undefined).tone).toBe("neutral");
  });

  it("normalises both histogram encodings and top values", () => {
    expect(normalizeHistogram([{ start: 0, end: 10, count: 3 }])).toEqual([{ label: "0–10", count: 3 }]);
    expect(normalizeHistogram({ bins: [0, 5, 10], counts: [1, 2] })).toEqual([
      { label: "0–5", count: 1 },
      { label: "5–10", count: 2 },
    ]);
    expect(normalizeTopValues({ a: 2 })).toEqual([{ value: "a", count: 2 }]);
  });

  it("rejects open redirects", () => {
    expect(safeNext("/o/acme/dashboard")).toBe("/o/acme/dashboard");
    expect(safeNext("//evil.example")).toBeNull();
    expect(safeNext("https://evil.example")).toBeNull();
    expect(safeNext("/login")).toBeNull();
  });

  it("uses server permissions first and role defaults as fallback", () => {
    expect(permissionsAllow(["datasets.view"], "owner", "members.manage")).toBe(false);
    expect(permissionsAllow(undefined, "owner", "members.manage")).toBe(true);
    expect(permissionsAllow(undefined, "viewer", "datasets.create")).toBe(false);
  });

  it("matches the most specific nav item", () => {
    expect(activeNavItem("/o/acme/settings/members", "acme")?.id).toBe("members");
    expect(activeNavItem("/o/acme/settings", "acme")?.id).toBe("settings");
    expect(activeNavItem("/o/acme/data/datasets/123", "acme")?.id).toBe("datasets");
  });
});
