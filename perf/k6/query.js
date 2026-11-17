// Guarded query latency (TESTING_STRATEGY §7): manual SQL through the guard, dataset reads, job status.
//   k6 run perf/k6/query.js
//   k6 run -e BASE_URL=http://localhost:8080 -e VUS=20 -e DURATION=5m perf/k6/query.js
// setup() creates one synthetic org, loads testdata sales.csv and shares the session cookie with all VUs.
import http from "k6/http";
import { check, fail, sleep } from "k6";
import { Trend } from "k6/metrics";

import { API, BASE_URL, ensureCsrf, signup, uploadAndCreateDataset, waitForJob } from "./lib/api.js";

const SALES = open("../../testdata/generated/sales.csv", "b");
const guardedMs = new Trend("guarded_query_ms", true);

// Logical table name the guard exposes for the dataset (override if the semantic layer names it differently).
const T = __ENV.TABLE || "sales";
const QUERIES = [
  `SELECT region, sum(revenue) AS revenue FROM ${T} GROUP BY region ORDER BY revenue DESC`,
  `SELECT date_trunc('month', order_date) AS month, count(*) AS orders FROM ${T} GROUP BY 1 ORDER BY 1`,
  `SELECT category, avg(discount) AS avg_discount FROM ${T} GROUP BY category`,
  `SELECT product, sum(quantity) AS units FROM ${T} GROUP BY product ORDER BY units DESC LIMIT 10`,
  `SELECT channel, count(DISTINCT customer_id) AS customers FROM ${T} GROUP BY channel`,
];

export const options = {
  setupTimeout: "10m",
  scenarios: {
    queries: {
      executor: "constant-vus",
      vus: Number(__ENV.VUS || 10),
      duration: __ENV.DURATION || "2m",
    },
  },
  thresholds: {
    guarded_query_ms: ["p(95)<1500"], // aggregate over the dataset, excluding AI
    "http_req_duration{name:datasets.list}": ["p(95)<400"],
    "http_req_duration{name:datasets.preview}": ["p(95)<800"],
    "http_req_failed{name:bi.queries}": ["rate<0.01"],
    checks: ["rate>0.99"],
  },
};

export function setup() {
  const owner = signup("query");
  const { datasetId, jobId } = uploadAndCreateDataset(owner.slug, "sales.csv", SALES, "perf-sales");
  const job = waitForJob(owner.slug, jobId);
  if (job.status !== "succeeded") fail(`ingest did not succeed: ${job.status}`);
  const cookies = http.cookieJar().cookiesForURL(BASE_URL);
  return { slug: owner.slug, datasetId, cookies };
}

export default function (data) {
  const jar = http.cookieJar();
  for (const [name, values] of Object.entries(data.cookies)) jar.set(BASE_URL, name, values[0]);
  const { slug, datasetId } = data;

  const sql = QUERIES[Math.floor(Math.random() * QUERIES.length)];
  const res = http.post(`${API}/orgs/${slug}/bi/queries`, JSON.stringify({ dataset_id: datasetId, sql }), {
    headers: { "Content-Type": "application/json", "X-CSRFToken": ensureCsrf() },
    tags: { name: "bi.queries" },
  });
  guardedMs.add(res.timings.duration);
  check(res, {
    "query 200": (r) => r.status === 200,
    "has rows": (r) => r.status === 200 && Array.isArray(r.json("rows")),
    "never truncated silently": (r) => r.status !== 200 || typeof r.json("truncated") === "boolean",
  });

  const list = http.get(`${API}/orgs/${slug}/datasets`, { tags: { name: "datasets.list" } });
  check(list, { "list 200": (r) => r.status === 200 });
  const preview = http.get(`${API}/orgs/${slug}/datasets/${datasetId}/preview?limit=100`, {
    tags: { name: "datasets.preview" },
  });
  check(preview, { "preview 200": (r) => r.status === 200 });

  // a rejected query must stay cheap and never 5xx
  const bad = http.post(
    `${API}/orgs/${slug}/bi/queries`,
    JSON.stringify({ dataset_id: datasetId, sql: "SELECT pg_sleep(10)" }),
    { headers: { "Content-Type": "application/json", "X-CSRFToken": ensureCsrf() }, tags: { name: "bi.queries.rejected" } },
  );
  check(bad, { "guard rejects fast": (r) => r.status === 422 && r.timings.duration < 500 });
  sleep(Math.random());
}
