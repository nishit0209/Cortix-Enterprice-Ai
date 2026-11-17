# NEXORA performance scripts (k6)

QA/Security-owned. Targets from `docs/TESTING_STRATEGY.md` §7. Not part of the per-push CI; run nightly and
before a release against the compose stack (`http://localhost:8080`). Every run signs up its own synthetic orgs
(`perf-*@example.test`) and uploads `testdata/generated/sales.csv`.

```bash
k6 run perf/k6/upload_ingest.js                                  # 4 VUs, 20 uploads
k6 run -e VUS=8 -e ITERATIONS=80 perf/k6/upload_ingest.js
k6 run perf/k6/query.js                                          # 10 VUs for 2 minutes
k6 run -e BASE_URL=http://localhost:8080 -e VUS=20 -e DURATION=5m -e TABLE=sales perf/k6/query.js
```

| Script | Scenario | Thresholds |
|---|---|---|
| `k6/upload_ingest.js` | Each VU signs up an org, then repeatedly uploads `sales.csv`, creates a dataset (`classification=synthetic`) and polls the job until it finishes | upload p95 < 3 s, dataset create p95 < 1.5 s, upload→ready p95 < 60 s, 0 failed ingests |
| `k6/query.js` | One org with the loaded dataset; VUs run guarded manual SQL (`POST /bi/queries`), dataset list and preview, plus a rejected `pg_sleep` query | guarded query p95 < 1.5 s, list p95 < 400 ms, preview p95 < 800 ms, < 1 % errors, rejections are fast 422s |

`k6/lib/api.js` holds the shared helpers: CSRF cookie + `X-CSRFToken` exactly like the SPA, signup, job polling,
multipart upload. Status: **written, not yet executed** (the upload, dataset and BI endpoints land in waves 2–3).
