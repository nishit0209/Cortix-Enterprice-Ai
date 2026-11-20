import { describe, expect, it } from "vitest";
import { isAllowedVerdict, SOURCE_LABELS, sourceTone, verdictLabel, verdictTone } from "./error-copy";

describe("query audit verdicts (apps/bi BiQueryAudit.verdict: ok | rejected | error | blocked)", () => {
  it("labels and colours the backend's verdicts", () => {
    expect([verdictLabel("ok"), verdictTone("ok")]).toEqual(["Allowed", "success"]);
    expect([verdictLabel("rejected"), verdictTone("rejected")]).toEqual(["Rejected", "danger"]);
    expect([verdictLabel("error"), verdictTone("error")]).toEqual(["Error", "danger"]);
    expect([verdictLabel("blocked"), verdictTone("blocked")]).toEqual(["Blocked by policy", "warning"]);
    expect([verdictLabel("some_new_state"), verdictTone("some_new_state")]).toEqual(["Some new state", "neutral"]);
  });

  it("treats only ok (and the legacy 'allowed') as let through", () => {
    expect(isAllowedVerdict("ok")).toBe(true);
    expect(isAllowedVerdict("allowed")).toBe(true);
    expect(isAllowedVerdict("blocked")).toBe(false);
    expect(isAllowedVerdict(null)).toBe(false);
  });

  it("names the W3 dashboard source of re-run widget queries", () => {
    expect(SOURCE_LABELS.dashboard).toBe("Dashboard");
    expect(sourceTone("dashboard")).toBe("info");
  });
});
