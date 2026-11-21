/**
 * No-mock guard. Every number, list and chart in the UI must come from the API (which reads the database);
 * demo content exists only through `manage.py seed_demo`. This test fails when application code under `src/`
 * (tests and test helpers excluded) contains a known demo-data marker, when a navigation entry is a "mock", or
 * when a "Planned" placeholder appears for anything but the modules that are not built yet.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import { describe, expect, it } from "vitest";
import { ALL_NAV_ITEMS } from "@/lib/nav";

// vite-node provides __dirname (import.meta.url is not a file: URL under jsdom); fall back to <cwd>/src.
const SRC = typeof __dirname === "string" ? resolve(__dirname, "..") : resolve(process.cwd(), "src");

/** Modules that are not built yet. Since wave 5 every module is built, so nothing may be "Planned". */
const PLANNED_ALLOWED = new Set<string>();

/** Demo-data markers: [what it catches, pattern]. Matched per line of application code. */
const DEMO_DATA_MARKERS: Array<[string, RegExp]> = [
  ["lorem ipsum filler", /lorem\s+ipsum/i],
  ["an identifier holding invented data", /\b(?:mock|fake|dummy|sample|demo|seed)(?:Data|Rows|Users|Members|Customers|Orders|Products|Kpis?|Stats|Metrics|Revenue|Sales|Series|Items|Records|Dashboards?|Widgets?|Notifications?|Insights?|Datasets?|Pipelines?|Documents?|Events|Activity|Results?|Values|Points|Charts?)\b/i],
  ["random numbers standing in for data", /\bMath\.random\s*\(/],
  ["a fake person", /\b(?:John|Jane)\s+(?:Doe|Smith)\b/],
  ["a timer that resolves with literal data (fake latency)", /setTimeout\([^;\n]*\b(?:resolve|res)\(\s*[[{'"`\d]/],
  ["a hard-coded money figure", /["'`>]\s*[$₹€£]\s?\d[\d,.]*\s*[KkMmBbLl]?\b/],
  ["a hard-coded percentage delta", /["'`>]\s*[+−-]\d+(?:\.\d+)?%\s*(?:["'`<]|vs\b)/],
  ["a fake-data library", /from\s+["'](?:@faker-js\/faker|faker|chance|casual)["']/],
  ["a TODO stub standing in for a feature", /\bTODO\b.*\b(?:mock|fake|stub|hard-?coded|placeholder data)\b/i],
];

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      if (name === "node_modules" || name === "test") continue;
      out.push(...sourceFiles(full));
    } else if (/\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name) && !name.endsWith(".d.ts")) {
      out.push(full);
    }
  }
  return out;
}

function findMarkers(text: string): Array<{ line: number; marker: string; excerpt: string }> {
  const hits: Array<{ line: number; marker: string; excerpt: string }> = [];
  text.split(/\r?\n/).forEach((raw, i) => {
    for (const [marker, re] of DEMO_DATA_MARKERS) {
      if (re.test(raw)) hits.push({ line: i + 1, marker, excerpt: raw.trim().slice(0, 120) });
    }
  });
  return hits;
}

describe("no-mock guard", () => {
  const files = sourceFiles(SRC);

  it("scans the application sources", () => {
    expect(files.length).toBeGreaterThan(100);
    expect(files.some((f) => f.endsWith(`app${sep}o${sep}[org]${sep}dashboard${sep}page.tsx`))).toBe(true);
  });

  it("finds no demo-data markers in application code", () => {
    const offenders = files.flatMap((f) =>
      findMarkers(readFileSync(f, "utf8")).map((h) => `${relative(SRC, f)}:${h.line} ${h.marker}: ${h.excerpt}`),
    );
    expect(offenders).toEqual([]);
  });

  it("recognizes every marker it is meant to catch (and leaves normal UI code alone)", () => {
    const bad = [
      "const text = 'Lorem ipsum dolor sit amet';",
      "const mockData = [{ month: 'Jan', value: 10 }];",
      "export const demoCustomers = rows;",
      "const sampleRows = [[1, 2]];",
      "value={Math.round(Math.random() * 100)}",
      "owner: 'John Doe',",
      "await new Promise((resolve) => setTimeout(() => resolve([{ id: 1 }]), 400));",
      "<StatCard value=\"$1.2M\" />",
      "<span>₹12,40,000</span>",
      "delta: '+12.5%' ,",
      "<Badge>+4% vs last week</Badge>",
      "import { faker } from '@faker-js/faker';",
      "// TODO: replace hardcoded stats with the API",
    ];
    for (const line of bad) expect(findMarkers(line), line).not.toEqual([]);
    const fine = [
      "const t = setTimeout(() => setCopied(false), 1500);",
      "placeholderData: (prev) => prev,",
      '<Input placeholder="Acme Analytics" />',
      "Sample of {panel.sourceRows ?? \"?\"} source rows",
      "const sample = values.filter((v) => v !== null).slice(0, 50);",
      "label: `${formatNumber(budget.used)} / ${formatNumber(budget.limit)}`,",
      "style={{ top: \"32%\" }}",
      '<stop offset="100%" stopColor={color} />',
      "description: \"Under the synthetic_only policy, only synthetic data is sent.\",",
    ];
    for (const line of fine) expect(findMarkers(line), line).toEqual([]);
  });

  it("has no navigation entry presented as a mock, and 'Planned' only for modules not built yet", () => {
    expect(ALL_NAV_ITEMS.filter((i) => i.status === "mock").map((i) => i.id)).toEqual([]);
    const planned = ALL_NAV_ITEMS.filter((i) => i.status === "planned");
    for (const item of planned) {
      expect(PLANNED_ALLOWED.has(item.id), `${item.id} is Planned but its module exists`).toBe(true);
      expect(item.phase ?? "", `${item.id} needs a roadmap phase`).toMatch(/^Wave \d$/);
    }
  });

  it("renders the Planned placeholder only on routes of planned modules", () => {
    const pages = files.filter((f) => f.includes(`${sep}app${sep}`) && f.endsWith("page.tsx"));
    for (const page of pages) {
      const m = /<PlannedModule\s+id="([^"]+)"/.exec(readFileSync(page, "utf8"));
      if (!m) continue;
      const item = ALL_NAV_ITEMS.find((i) => i.id === m[1]);
      expect(item?.status, `${relative(SRC, page)} renders PlannedModule for "${m[1]}"`).toBe("planned");
    }
  });

  it("ships no mock or fake-data packages to the browser", () => {
    const pkg = JSON.parse(readFileSync(join(SRC, "..", "package.json"), "utf8")) as { dependencies?: Record<string, string> };
    const deps = Object.keys(pkg.dependencies ?? {});
    expect(deps.filter((d) => /faker|chance|casual|msw|mock|json-server|miragejs/i.test(d))).toEqual([]);
  });
});
