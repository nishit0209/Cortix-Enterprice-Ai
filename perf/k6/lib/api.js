// Shared k6 helpers: session auth with CSRF exactly like the SPA, org bootstrap, job polling.
import http from "k6/http";
import { check, fail, sleep } from "k6";

export const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";
export const API = `${BASE_URL}/api/v1`;

function csrfToken() {
  const jar = http.cookieJar();
  const cookies = jar.cookiesForURL(BASE_URL);
  return (cookies.csrftoken && cookies.csrftoken[0]) || "";
}

export function ensureCsrf() {
  if (!csrfToken()) {
    const res = http.get(`${API}/auth/csrf`, { tags: { name: "auth.csrf" } });
    check(res, { "csrf cookie set": (r) => r.status < 300 });
  }
  return csrfToken();
}

export function jsonHeaders(extra = {}) {
  return { "Content-Type": "application/json", "X-CSRFToken": ensureCsrf(), ...extra };
}

export function post(path, body, name) {
  return http.post(`${API}${path}`, JSON.stringify(body), { headers: jsonHeaders(), tags: { name } });
}

/** Synthetic user + org (reserved example.test domain). Returns {email, password, slug}. */
export function signup(tag) {
  const id = `${Date.now().toString(36)}${Math.floor(Math.random() * 1e6).toString(36)}`;
  const user = { email: `perf-${tag}-${id}@example.test`, password: `Nx-perf-${id}-Pa55!` };
  const res = post(
    "/auth/signup",
    { email: user.email, password: user.password, full_name: `Perf ${tag}`, org_name: `Perf ${tag} ${id}` },
    "auth.signup",
  );
  if (!check(res, { "signup 201": (r) => r.status === 201 })) {
    fail(`signup failed: ${res.status} ${res.body}`);
  }
  return { ...user, slug: res.json("org.slug") };
}

export function login(email, password) {
  const res = post("/auth/login", { email, password }, "auth.login");
  check(res, { "login 200": (r) => r.status === 200 });
  return res;
}

/** Poll GET /orgs/<org>/jobs/<id> until a terminal status. Returns {status, ms}. */
export function waitForJob(slug, jobId, timeoutS = 600) {
  const started = Date.now();
  while ((Date.now() - started) / 1000 < timeoutS) {
    const res = http.get(`${API}/orgs/${slug}/jobs/${jobId}`, { tags: { name: "jobs.detail" } });
    const status = res.status === 200 ? res.json("status") : "http_" + res.status;
    if (["succeeded", "failed", "cancelled"].includes(status) || status.startsWith("http_4")) {
      return { status, ms: Date.now() - started };
    }
    sleep(0.5);
  }
  return { status: "timeout", ms: Date.now() - started };
}

/** Upload a file (multipart) and create a dataset from it. Returns {datasetId, jobId}. */
export function uploadAndCreateDataset(slug, filename, bytes, name) {
  const up = http.post(
    `${API}/orgs/${slug}/uploads`,
    { file: http.file(bytes, filename, "text/csv") },
    { headers: { "X-CSRFToken": ensureCsrf() }, tags: { name: "uploads.create" } },
  );
  if (!check(up, { "upload 201": (r) => r.status === 201 })) {
    fail(`upload failed: ${up.status} ${up.body}`);
  }
  const ds = post(
    `/orgs/${slug}/datasets`,
    { name, upload_id: up.json("id"), classification: "synthetic" },
    "datasets.create",
  );
  if (!check(ds, { "dataset 202": (r) => r.status === 202 || r.status === 201 })) {
    fail(`dataset create failed: ${ds.status} ${ds.body}`);
  }
  return { datasetId: ds.json("dataset.id"), jobId: ds.json("job_id") };
}
