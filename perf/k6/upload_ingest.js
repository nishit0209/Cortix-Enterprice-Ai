// Upload + ingest throughput (TESTING_STRATEGY §7). Not run in CI per push; nightly / before release.
//   k6 run perf/k6/upload_ingest.js                     # default: 4 VUs x 5 iterations each
//   k6 run -e BASE_URL=http://localhost:8080 -e VUS=8 -e ITERATIONS=40 perf/k6/upload_ingest.js
// Each VU signs up its own synthetic org (setup is per-VU so orgs never share tenants).
import exec from "k6/execution";
import { check } from "k6";
import { Counter, Trend } from "k6/metrics";

import { signup, uploadAndCreateDataset, waitForJob } from "./lib/api.js";

const SALES = open("../../testdata/generated/sales.csv", "b");
const ingestMs = new Trend("ingest_end_to_end_ms", true);
const ingestFailed = new Counter("ingest_failed");

export const options = {
  scenarios: {
    ingest: {
      executor: "shared-iterations",
      vus: Number(__ENV.VUS || 4),
      iterations: Number(__ENV.ITERATIONS || 20),
      maxDuration: "30m",
    },
  },
  thresholds: {
    "http_req_duration{name:uploads.create}": ["p(95)<3000"],
    "http_req_duration{name:datasets.create}": ["p(95)<1500"],
    ingest_end_to_end_ms: ["p(95)<60000"], // 5k-row CSV: upload -> profile -> quality -> warehouse
    ingest_failed: ["count==0"],
    checks: ["rate>0.99"],
  },
};

const orgs = {};

export default function () {
  const vu = exec.vu.idInTest;
  if (!orgs[vu]) orgs[vu] = signup(`ingest${vu}`); // cookie jar is per VU => session stays with its org
  const { slug } = orgs[vu];
  const started = Date.now();
  const { jobId } = uploadAndCreateDataset(slug, "sales.csv", SALES, `perf-sales-${exec.scenario.iterationInTest}`);
  const job = waitForJob(slug, jobId);
  ingestMs.add(Date.now() - started);
  if (!check(job, { "ingest succeeded": (j) => j.status === "succeeded" })) ingestFailed.add(1);
}
